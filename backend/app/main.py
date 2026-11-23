"""RecallRelay FastAPI application entrypoint."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes.a2a import router as a2a_router
from app.api.routes.agents import router as agents_router
from app.api.routes.evidence import router as evidence_router
from app.api.routes.governance import router as governance_router
from app.api.routes.health import router as health_router
from app.api.routes.integrations import router as integrations_router
from app.api.routes.inventory import router as inventory_router
from app.api.routes.orchestrations import router as orchestrations_router
from app.api.routes.organizations import router as organizations_router
from app.api.routes.recalls import router as recalls_router
from app.api.routes.session import router as session_router
from app.api.routes.sharing import router as sharing_router
from app.api.routes.shipments import router as shipments_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.db.database import init_db, mark_stale_running_orchestrations_interrupted
from app.integrations.registry import IntegrationRegistry


_UNSET = object()


def create_app(
    database_path: str | None = None,
    admin_api_key: str | None | object = _UNSET,
    trusted_origins: List[str] | None = None,
    a2a_transport: str | None = None,
) -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()
    resolved_db_path = database_path or settings.database_path
    resolved_trusted_origins = (
        [o.strip().rstrip("/") for o in trusted_origins if o and o.strip()]
        if trusted_origins is not None
        else settings.trusted_origins
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_db(resolved_db_path)
        mark_stale_running_orchestrations_interrupted(resolved_db_path)
        yield

    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "RecallRelay multi-agent product recall coordination network "
            "(v1.0.0 — BFF Sessions, Explicit A2A Transport, Lyzr Source Analysis & openFDA Importer)."
        ),
        lifespan=lifespan,
    )

    application.state.started_at = time.monotonic()
    application.state.database_path = resolved_db_path
    application.state.admin_api_key = (
        settings.admin_api_key if admin_api_key is _UNSET else admin_api_key
    )
    application.state.trusted_origins = resolved_trusted_origins
    application.state.integration_registry = IntegrationRegistry(
        db_path=resolved_db_path,
        settings=settings,
        a2a_transport_override=a2a_transport,
    )

    init_db(resolved_db_path)
    mark_stale_running_orchestrations_interrupted(resolved_db_path)

    # allow_credentials=True with explicit trusted_origins (never '*') enables HttpOnly BFF cookies.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_trusted_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    register_exception_handlers(application)
    application.include_router(health_router, prefix="/api")
    application.include_router(session_router, prefix="/api/v1")
    application.include_router(evidence_router, prefix="/api/v1")
    application.include_router(organizations_router, prefix="/api/v1")
    application.include_router(recalls_router, prefix="/api/v1")
    application.include_router(shipments_router, prefix="/api/v1")
    application.include_router(inventory_router, prefix="/api/v1")
    application.include_router(agents_router, prefix="/api/v1")
    application.include_router(a2a_router, prefix="/api/v1")
    application.include_router(sharing_router, prefix="/api/v1")
    application.include_router(governance_router, prefix="/api/v1")
    application.include_router(integrations_router, prefix="/api/v1")
    application.include_router(orchestrations_router, prefix="/api/v1")

    # Serve built React SPA on the same origin when frontend/dist exists (single-origin production).
    dist_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist_dir.is_dir():
        application.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="spa")

    return application


app = create_app()
