"""Parent login for the web panel: session cookie + CSRF token for every form."""

from __future__ import annotations

import hmac
import secrets
from typing import Annotated

from fastapi import Depends, Form, Request

from pc_server.core.exceptions import PcServerError
from pc_shared.passwords import AttemptLimiter

login_limiter = AttemptLimiter(max_failures=5, lock_seconds=300)


class LoginRequired(PcServerError):
    status_code = 303
    detail = "Нужно войти."


class CsrfError(PcServerError):
    status_code = 400
    detail = "Форма устарела. Обновите страницу и повторите."


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def require_parent(request: Request) -> None:
    if not request.session.get("parent"):
        raise LoginRequired()


async def check_csrf(request: Request, csrf: Annotated[str, Form()] = "") -> None:
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(csrf, expected):
        raise CsrfError()


def log_in(request: Request) -> None:
    request.session.clear()
    request.session["parent"] = True
    request.session["csrf"] = secrets.token_urlsafe(24)


Parent = Annotated[None, Depends(require_parent)]
Csrf = Annotated[None, Depends(check_csrf)]
