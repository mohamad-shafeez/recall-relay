"""Health check endpoint."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.core.config import get_settings
from app.db.database import check_db_health
from app.schemas.health import HealthChecks, HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service health check")
async def get_health(request: Request) -> HealthResponse:
    """Return structured health metadata for the RecallRelay backend."""
    settings = get_settings()
    started_at: float = getattr(request.app.state, "started_at", time.monotonic())
    uptime_seconds = round(max(0.0, time.monotonic() - started_at), 3)
    db_path: str | None = getattr(request.app.state, "database_path", None)
    db_ok = check_db_health(db_path)

    return HealthResponse(
        status="ok" if db_ok else "degraded",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        demo_mode=settings.demo_mode,
        timestamp=datetime.now(timezone.utc).isoformat(),
        uptime_seconds=uptime_seconds,
        checks=HealthChecks(
            api="healthy",
            database="healthy" if db_ok else "degraded",
        ),
    )
