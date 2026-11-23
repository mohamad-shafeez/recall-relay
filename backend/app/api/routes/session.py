"""BFF Session API routes: login (admin/org) with Origin checks & throttling, session introspection, and logout."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.auth import (
    _validate_cookie_csrf_and_origin,
    resolve_bearer_token,
    resolve_client_ip_for_rate_limit,
    validate_request_origin,
    verify_admin_key_value,
)
from app.core.config import get_settings
from app.core.sessions import (
    SESSION_COOKIE_NAME,
    check_login_rate_limit,
    create_bff_session,
    record_login_attempt,
    resolve_bff_session,
    revoke_bff_session_by_token,
)

router = APIRouter(prefix="/session", tags=["session"])


class AdminLoginRequest(BaseModel):
    admin_key: str


class OrgLoginRequest(BaseModel):
    bearer_token: str


class SessionStatusResponse(BaseModel):
    authenticated: bool
    mode: str  # "public" | "admin" | "organization"
    organization_id: str | None = None
    organization_role: str | None = None
    data_boundary_id: str | None = None
    csrf_token: str | None = None
    expires_at: str | None = None


def _set_session_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        max_age=settings.session_ttl_minutes * 60,
        path="/",
    )


@router.post(
    "/login/admin",
    response_model=SessionStatusResponse,
    summary="Verify admin key once (throttled & origin-checked) and issue an HttpOnly session cookie",
)
def login_admin(
    payload: AdminLoginRequest,
    request: Request,
    response: Response,
    rr_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> SessionStatusResponse:
    validate_request_origin(request)
    db_path: str | None = getattr(request.app.state, "database_path", None)
    client_ip = resolve_client_ip_for_rate_limit(request)
    scope_key = f"admin_login:{client_ip}"

    check_login_rate_limit(db_path, scope_key=scope_key)
    try:
        verify_admin_key_value(request, payload.admin_key)
    except HTTPException as exc:
        if exc.status_code == 401:
            record_login_attempt(db_path, scope_key=scope_key, success=False)
        raise

    record_login_attempt(db_path, scope_key=scope_key, success=True)
    # Rotate any prior session to prevent session fixation
    revoke_bff_session_by_token(db_path, rr_session)
    session, raw_token = create_bff_session(db_path, principal_type="ADMIN")
    _set_session_cookie(response, raw_token)
    return SessionStatusResponse(
        authenticated=True,
        mode="admin",
        csrf_token=session.csrf_token,
        expires_at=session.expires_at,
    )


@router.post(
    "/login/organization",
    response_model=SessionStatusResponse,
    summary="Verify organization bearer token once (throttled & origin-checked) and issue an HttpOnly session cookie",
)
def login_organization(
    payload: OrgLoginRequest,
    request: Request,
    response: Response,
    rr_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> SessionStatusResponse:
    validate_request_origin(request)
    db_path: str | None = getattr(request.app.state, "database_path", None)
    client_ip = resolve_client_ip_for_rate_limit(request)
    scope_key = f"org_login:{client_ip}"

    check_login_rate_limit(db_path, scope_key=scope_key)
    try:
        boundary = resolve_bearer_token(db_path, f"Bearer {payload.bearer_token.strip()}")
    except HTTPException as exc:
        if exc.status_code == 401:
            record_login_attempt(db_path, scope_key=scope_key, success=False)
        raise

    record_login_attempt(db_path, scope_key=scope_key, success=True)
    revoke_bff_session_by_token(db_path, rr_session)
    session, raw_token = create_bff_session(
        db_path,
        principal_type="ORGANIZATION",
        organization_id=boundary.organization_id,
        organization_role=boundary.role,
        data_boundary_id=boundary.data_boundary_id,
    )
    _set_session_cookie(response, raw_token)
    return SessionStatusResponse(
        authenticated=True,
        mode="organization",
        organization_id=boundary.organization_id,
        organization_role=boundary.role.value,
        data_boundary_id=boundary.data_boundary_id,
        csrf_token=session.csrf_token,
        expires_at=session.expires_at,
    )


@router.get(
    "/me",
    response_model=SessionStatusResponse,
    summary="Inspect active HttpOnly BFF session status (returns public mode if unauthenticated)",
)
def get_current_session(
    request: Request,
    rr_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> SessionStatusResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    session = resolve_bff_session(db_path, rr_session)
    if session is None:
        return SessionStatusResponse(authenticated=False, mode="public")
    return SessionStatusResponse(
        authenticated=True,
        mode="admin" if session.principal_type == "ADMIN" else "organization",
        organization_id=session.organization_id,
        organization_role=session.organization_role.value if session.organization_role else None,
        data_boundary_id=session.data_boundary_id,
        csrf_token=session.csrf_token,
        expires_at=session.expires_at,
    )


@router.post(
    "/logout",
    response_model=SessionStatusResponse,
    summary="Revoke active BFF session and clear HttpOnly cookie",
)
def logout_session(
    request: Request,
    response: Response,
    x_csrf_token: Optional[str] = Header(default=None, alias="X-CSRF-Token"),
    rr_session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> SessionStatusResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    session = resolve_bff_session(db_path, rr_session)
    if session is not None:
        _validate_cookie_csrf_and_origin(request, session, x_csrf_token)
        revoke_bff_session_by_token(db_path, rr_session)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return SessionStatusResponse(authenticated=False, mode="public")
