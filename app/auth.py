"""Users, sessions and stream authorisation.

Three ways to authenticate, because radio players are not browsers:

    1. session cookie  - the hub UI, set by POST /api/auth/login
    2. Bearer token    - API clients
    3. stream token    - ``?token=...`` on /stream/*, plus HTTP Basic as a
                         fallback for players that cannot send headers
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.security import TokenError, hash_password, sign_token, verify_password, verify_token

log = logging.getLogger(__name__)

Role = Literal["admin", "listener"]


class User(BaseModel):
    username: str
    role: Role = "listener"
    password_hash: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    disabled: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "role": self.role,
            "disabled": self.disabled,
            "created_at": self.created_at.isoformat(),
        }


class UserStore:
    """Users persisted as JSON on the data volume."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._users: dict[str, User] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
            self._users = {u["username"]: User(**u) for u in raw.get("users", [])}
        except (OSError, ValueError, TypeError) as exc:
            log.error("could not read %s: %s", self._path, exc)

    def _save(self) -> None:
        payload = {"users": [u.model_dump(mode="json") for u in self._users.values()]}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        tmp.replace(self._path)

    def list(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: u.username)

    def get(self, username: str) -> User | None:
        return self._users.get(username.strip().lower())

    def create(self, username: str, password: str, role: Role = "listener") -> User:
        username = username.strip().lower()
        if not username:
            raise ValueError("username must not be empty")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        with self._lock:
            if username in self._users:
                raise ValueError(f"user '{username}' already exists")
            user = User(username=username, role=role, password_hash=hash_password(password))
            self._users[username] = user
            self._save()
        return user

    def set_password(self, username: str, password: str) -> None:
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        with self._lock:
            user = self._users[username.strip().lower()]
            user.password_hash = hash_password(password)
            self._save()

    def update(self, username: str, *, role: Role | None = None, disabled: bool | None = None) -> User:
        with self._lock:
            user = self._users[username.strip().lower()]
            if role is not None:
                user.role = role
            if disabled is not None:
                user.disabled = disabled
            self._save()
        return user

    def delete(self, username: str) -> None:
        with self._lock:
            self._users.pop(username.strip().lower(), None)
            self._save()

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.get(username)
        if user is None:
            # Spend roughly the same time as a real check so the endpoint does
            # not leak which usernames exist.
            verify_password(password, hash_password("dummy-password"))
            return None
        if user.disabled or not verify_password(password, user.password_hash):
            return None
        return user

    def bootstrap_admin(self, username: str, password: str) -> None:
        if self._users:
            return
        if not password or password in {"admin", "change-me"}:
            log.warning(
                "bootstrap admin '%s' is using a weak default password - "
                "set RADIO_ADMIN_PASSWORD and change it after first login",
                username,
            )
        try:
            self.create(username, password or "changeme123", role="admin")
            log.info("created bootstrap admin user '%s'", username)
        except ValueError as exc:
            log.error("could not create bootstrap admin: %s", exc)


class RevocationStore:
    """Revoked stream tokens, by jti."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._revoked: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                self._revoked = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                self._revoked = {}

    def revoke(self, jti: str, meta: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._revoked[jti] = meta or {}
            self._prune()
            self._path.write_text(json.dumps(self._revoked, indent=2), "utf-8")

    def is_revoked(self, jti: str | None) -> bool:
        return bool(jti) and jti in self._revoked

    def _prune(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        self._revoked = {
            jti: meta
            for jti, meta in self._revoked.items()
            if not meta.get("exp") or float(meta["exp"]) > now
        }


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def create_session_token(user: User, settings: Settings) -> str:
    return sign_token(
        {"sub": user.username, "role": user.role, "typ": "session"},
        settings.secret_key,
        ttl_seconds=settings.session_ttl_hours * 3600,
    )


def create_stream_token(
    user: User, settings: Settings, station_id: str | None = None, ttl_hours: int | None = None
) -> str:
    """A token that only unlocks streams.

    ``station_id=None`` mints an all-station key (handy for one player that
    hops between stations); otherwise the token is bound to one station.
    """
    payload: dict[str, Any] = {"sub": user.username, "role": user.role, "typ": "stream"}
    if station_id:
        payload["sid"] = station_id
    return sign_token(
        payload,
        settings.secret_key,
        ttl_seconds=(ttl_hours if ttl_hours is not None else settings.stream_token_ttl_hours) * 3600,
    )


def _decode_basic(header: str) -> tuple[str, str] | None:
    if not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return username, password


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_user_store(request: Request) -> UserStore:
    return request.app.state.users


def get_revocations(request: Request) -> RevocationStore:
    return request.app.state.revocations


def _unauthorized(detail: str, basic: bool = False) -> HTTPException:
    headers = {"WWW-Authenticate": 'Basic realm="radio", charset="UTF-8"'} if basic else None
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers=headers)


def current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    users: Annotated[UserStore, Depends(get_user_store)],
) -> User:
    """Authenticate a hub/API request via session cookie or bearer token."""
    token = request.cookies.get(settings.cookie_name)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header.split(" ", 1)[1].strip()

    if not token:
        raise _unauthorized("not authenticated")
    try:
        payload = verify_token(token, settings.secret_key)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc
    if payload.get("typ") != "session":
        raise _unauthorized("wrong token type")

    user = users.get(str(payload.get("sub", "")))
    if user is None or user.disabled:
        raise _unauthorized("unknown user")
    return user


def optional_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    users: Annotated[UserStore, Depends(get_user_store)],
) -> User | None:
    try:
        return current_user(request, settings, users)
    except HTTPException:
        return None


def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return user


def authenticate_stream_request(
    request: Request,
    station_id: str,
    settings: Settings,
    users: UserStore,
    revocations: RevocationStore,
) -> User:
    """Authorise a listener for one station.

    Order: stream token (query or bearer) -> session cookie -> HTTP Basic.
    """
    token = request.query_params.get("token")
    header = request.headers.get("authorization", "")
    if not token and header.lower().startswith("bearer "):
        token = header.split(" ", 1)[1].strip()

    if token:
        try:
            payload = verify_token(token, settings.secret_key)
        except TokenError as exc:
            raise _unauthorized(str(exc), basic=settings.allow_basic_auth) from exc
        if payload.get("typ") not in {"stream", "session"}:
            raise _unauthorized("wrong token type", basic=settings.allow_basic_auth)
        if revocations.is_revoked(payload.get("jti")):
            raise _unauthorized("token revoked", basic=settings.allow_basic_auth)
        bound = payload.get("sid")
        if bound and bound != station_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "token is not valid for this station")
        user = users.get(str(payload.get("sub", "")))
        if user is None or user.disabled:
            raise _unauthorized("unknown user", basic=settings.allow_basic_auth)
        return user

    cookie = request.cookies.get(settings.cookie_name)
    if cookie:
        try:
            payload = verify_token(cookie, settings.secret_key)
            user = users.get(str(payload.get("sub", "")))
            if user and not user.disabled and payload.get("typ") == "session":
                return user
        except TokenError:
            pass

    if settings.allow_basic_auth and header:
        credentials = _decode_basic(header)
        if credentials:
            user = users.authenticate(*credentials)
            if user:
                return user

    raise _unauthorized("authentication required", basic=settings.allow_basic_auth)


def user_may_access(user: User, station: Any) -> bool:
    """Check a station's AccessSpec against a user."""
    access = station.access
    if user.role == "admin":
        return True
    if user.role not in access.allowed_roles:
        return False
    if access.allowed_users and user.username not in access.allowed_users:
        return False
    return True
