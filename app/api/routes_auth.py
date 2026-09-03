"""Login, logout and user management."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.auth import (
    User,
    UserStore,
    create_session_token,
    current_user,
    get_user_store,
    require_admin,
)
from app.config import Settings, get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class CreateUser(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    role: Literal["admin", "listener"] = "listener"


class UpdateUser(BaseModel):
    role: Literal["admin", "listener"] | None = None
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=8)


def _set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/login")
async def login(
    payload: LoginRequest,
    response: Response,
    users: Annotated[UserStore, Depends(get_user_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    user = users.authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    token = create_session_token(user, settings)
    _set_cookie(response, token, settings)
    return {"user": user.public(), "token": token}


@router.post("/logout")
async def logout(response: Response, settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
    response.delete_cookie(settings.cookie_name, path="/")
    return {"status": "logged out"}


@router.get("/me")
async def me(user: Annotated[User, Depends(current_user)]) -> dict[str, object]:
    return user.public()


@router.post("/password")
async def change_password(
    payload: PasswordChange,
    user: Annotated[User, Depends(current_user)],
    users: Annotated[UserStore, Depends(get_user_store)],
) -> dict[str, str]:
    if users.authenticate(user.username, payload.current_password) is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "current password is wrong")
    users.set_password(user.username, payload.new_password)
    return {"status": "password changed"}


@router.get("/users", dependencies=[Depends(require_admin)])
async def list_users(users: Annotated[UserStore, Depends(get_user_store)]) -> list[dict[str, object]]:
    return [u.public() for u in users.list()]


@router.post("/users", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def create_user(
    payload: CreateUser, users: Annotated[UserStore, Depends(get_user_store)]
) -> dict[str, object]:
    try:
        user = users.create(payload.username, payload.password, payload.role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return user.public()


@router.patch("/users/{username}", dependencies=[Depends(require_admin)])
async def update_user(
    username: str,
    payload: UpdateUser,
    users: Annotated[UserStore, Depends(get_user_store)],
) -> dict[str, object]:
    if users.get(username) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user '{username}'")
    if payload.password:
        users.set_password(username, payload.password)
    user = users.update(username, role=payload.role, disabled=payload.disabled)
    return user.public()


@router.delete("/users/{username}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_user(
    username: str,
    admin: Annotated[User, Depends(require_admin)],
    users: Annotated[UserStore, Depends(get_user_store)],
) -> Response:
    if username.strip().lower() == admin.username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot delete your own account")
    remaining_admins = [u for u in users.list() if u.role == "admin" and u.username != username]
    if not remaining_admins:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the last admin cannot be deleted")
    users.delete(username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
