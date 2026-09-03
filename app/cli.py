"""Command line helpers.

    docker compose exec radio python -m app.cli stations
    docker compose exec radio python -m app.cli add-genre "Deep House Nights" "Deep House,House" --template club
    docker compose exec radio python -m app.cli add-mood "Sunday Morning" "calm,warm"
    docker compose exec radio python -m app.cli add-artist "Bowie Radio" "David Bowie" --similar
    docker compose exec radio python -m app.cli user add lisa --role listener
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app.auth import UserStore
from app.config import get_settings
from app.presets import TEMPLATES, quick_artist, quick_genre, quick_library, quick_mood
from app.store import StationExists, StationStore


def _split(values: str) -> list[str]:
    return [v.strip() for v in values.split(",") if v.strip()]


def _add(store: StationStore, spec) -> int:
    try:
        store.create(spec)
    except StationExists as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"created '{spec.id}' -> {spec.mount()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="app.cli", description="Jellyfin Radio admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stations", help="list stations")
    sub.add_parser("templates", help="list mixing templates")

    for name, help_text in (
        ("add-genre", "new station from genres"),
        ("add-mood", "new station from AudioMuse moods"),
        ("add-artist", "new artist station"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("name")
        cmd.add_argument("values", help="comma separated")
        cmd.add_argument("--template", default="radio", choices=sorted(TEMPLATES))
        cmd.add_argument("--id", dest="station_id", default=None)
        if name == "add-artist":
            cmd.add_argument("--similar", action="store_true", help="also play similar artists")

    cmd = sub.add_parser("add-library", help="new station from the whole library")
    cmd.add_argument("name")
    cmd.add_argument("--search", default=None)
    cmd.add_argument("--template", default="radio", choices=sorted(TEMPLATES))
    cmd.add_argument("--id", dest="station_id", default=None)

    cmd = sub.add_parser("remove", help="delete a station")
    cmd.add_argument("station_id")

    user = sub.add_parser("user", help="manage users")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    user_sub.add_parser("list")
    add_user = user_sub.add_parser("add")
    add_user.add_argument("username")
    add_user.add_argument("--role", default="listener", choices=["admin", "listener"])
    passwd = user_sub.add_parser("passwd")
    passwd.add_argument("username")

    args = parser.parse_args(argv)
    store = StationStore(settings.stations_file)

    if args.command == "stations":
        for spec in store.list():
            state = "on" if spec.enabled else "off"
            kinds = ", ".join(s.kind for s in spec.sources)
            print(f"{spec.id:24} {state:3} {spec.mount():34} [{kinds}] {spec.name}")
        return 0

    if args.command == "templates":
        for template in TEMPLATES.values():
            print(f"{template.key:10} {template.label:18} {template.description}")
        return 0

    if args.command == "add-genre":
        return _add(store, quick_genre(args.name, _split(args.values), template=args.template, station_id=args.station_id))

    if args.command == "add-mood":
        return _add(store, quick_mood(args.name, _split(args.values), template=args.template, station_id=args.station_id))

    if args.command == "add-artist":
        return _add(
            store,
            quick_artist(
                args.name,
                _split(args.values),
                template=args.template,
                include_similar=args.similar,
                station_id=args.station_id,
            ),
        )

    if args.command == "add-library":
        return _add(store, quick_library(args.name, search=args.search, template=args.template, station_id=args.station_id))

    if args.command == "remove":
        try:
            store.delete(args.station_id)
        except KeyError:
            print(f"error: no station '{args.station_id}'", file=sys.stderr)
            return 1
        print(f"deleted '{args.station_id}'")
        return 0

    if args.command == "user":
        users = UserStore(settings.users_file)
        if args.user_command == "list":
            for entry in users.list():
                flag = " (disabled)" if entry.disabled else ""
                print(f"{entry.username:20} {entry.role}{flag}")
            return 0
        password = getpass.getpass("password: ")
        try:
            if args.user_command == "add":
                users.create(args.username, password, args.role)
                print(f"created user '{args.username}'")
            else:
                if users.get(args.username) is None:
                    print(f"error: no user '{args.username}'", file=sys.stderr)
                    return 1
                users.set_password(args.username, password)
                print(f"password updated for '{args.username}'")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
