"""Health check response schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthChecks(BaseModel):
    """Individual subsystem health indicators."""

    api: Literal["healthy", "degraded"] = "healthy"
    database: Literal["healthy", "degraded"] = "healthy"


class HealthResponse(BaseModel):
    """Structured response returned by GET /api/health."""

    status: Literal["ok", "degraded"] = Field(default="ok")
    service: str
    version: str
    environment: str
    demo_mode: bool
    timestamp: str
    uptime_seconds: float = Field(ge=0.0)
    checks: HealthChecks
