"""The hub: HTML pages for browsing and playing the stations."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import User, optional_user
from app.config import Settings, get_settings

router = APIRouter(tags=["hub"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "web" / "templates"))

OptionalUser = Annotated[User | None, Depends(optional_user)]


@router.get("/", response_class=HTMLResponse)
async def hub(request: Request, user: OptionalUser, settings: Annotated[Settings, Depends(get_settings)]):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "hub.html", {"user": user.public(), "app_name": settings.app_name}
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: OptionalUser, settings: Annotated[Settings, Depends(get_settings)]):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"app_name": settings.app_name})


@router.get("/station/{station_id}", response_class=HTMLResponse)
async def station_page(
    request: Request,
    station_id: str,
    user: OptionalUser,
    settings: Annotated[Settings, Depends(get_settings)],
):
    if user is None:
        return RedirectResponse(f"/login?next=/station/{station_id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "station.html",
        {"user": user.public(), "station_id": station_id, "app_name": settings.app_name},
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user: OptionalUser, settings: Annotated[Settings, Depends(get_settings)]):
    if user is None:
        return RedirectResponse("/login?next=/admin", status_code=303)
    return templates.TemplateResponse(
        request, "admin.html", {"user": user.public(), "app_name": settings.app_name}
    )
