"""Standardized JSON error handling for the RecallRelay API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorDetail(BaseModel):
    """Structured error body returned on failed API requests."""

    code: str
    message: str
    status_code: int
    timestamp: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    """Top-level error response envelope."""

    error: ErrorDetail


def _build_error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            status_code=status_code,
            timestamp=datetime.now(timezone.utc).isoformat(),
            details=jsonable_encoder(details) if details is not None else None,
        )
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(), headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach consistent JSON exception handlers to the FastAPI instance."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return _build_error_response(
            code="http_error",
            message=str(exc.detail),
            status_code=exc.status_code,
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _build_error_response(
            code="validation_error",
            message="Request validation failed.",
            status_code=422,
            details=exc.errors(),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        return _build_error_response(
            code="internal_server_error",
            message="An unexpected internal server error occurred.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
