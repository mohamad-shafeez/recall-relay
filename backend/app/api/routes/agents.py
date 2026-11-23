"""Versioned API routes for inspecting and invoking RecallRelay agents."""

from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.agents.audit import AgentExecutionAudit, list_execution_audits
from app.agents.base import AgentAuthContext, AgentExecutionEnvelope, AgentRole
from app.agents.specialists import AGENT_CLASSES, build_agent
from app.agents.tools import ToolRegistry
from app.core.auth import (
    require_admin_auth,
    require_admin_or_org_auth,
    require_org_boundary,
)
from app.core.config import get_settings
from app.schemas.domain import OrganizationBoundaryContext

router = APIRouter(prefix="/agents", tags=["agents"])

_TOOL_REGISTRY = ToolRegistry()

_ORG_BOUNDARY_AGENT_ROLES = {
    AgentRole.MANUFACTURER_OPERATIONS_AGENT,
    AgentRole.DISTRIBUTOR_RECONCILIATION_AGENT,
    AgentRole.RETAILER_INVENTORY_AGENT,
}


class AgentDescriptor(BaseModel):
    agent_id: str
    role: AgentRole
    execution_mode: str
    allowed_tools: List[str]
    requires_org_boundary: bool


class AgentRunRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


@router.get(
    "",
    response_model=List[AgentDescriptor],
    summary="List registered specialized agents and their backend-enforced tool permissions",
)
def list_agents(
    _auth=Depends(require_admin_or_org_auth),
) -> List[AgentDescriptor]:
    descriptors: List[AgentDescriptor] = []
    for role, cls in AGENT_CLASSES.items():
        descriptors.append(
            AgentDescriptor(
                agent_id=cls.agent_id,
                role=role,
                execution_mode=cls.execution_mode.value,
                allowed_tools=_TOOL_REGISTRY.allowed_tools_for(role),
                requires_org_boundary=role in _ORG_BOUNDARY_AGENT_ROLES,
            )
        )
    return descriptors


@router.post(
    "/{agent_role}/run",
    response_model=AgentExecutionEnvelope,
    summary="Execute a specialized agent within its authorized context",
)
def run_agent(
    agent_role: AgentRole,
    body: AgentRunRequest,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="X-Idempotency-Key"),
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(default=None),
) -> AgentExecutionEnvelope:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    timeout = get_settings().agent_default_timeout_seconds

    if agent_role in _ORG_BOUNDARY_AGENT_ROLES:
        boundary: OrganizationBoundaryContext = require_org_boundary(
            request, authorization=authorization
        )
        auth_ctx = AgentAuthContext(is_admin=False, boundary=boundary)
    else:
        require_admin_auth(request, x_admin_key=x_admin_key)
        auth_ctx = AgentAuthContext(is_admin=True, boundary=None)

    registry = getattr(request.app.state, "integration_registry", None)
    lyzr_adapter = registry.get_adapter("lyzr") if registry is not None else None

    agent = build_agent(
        agent_role,
        db_path=db_path,
        tool_registry=_TOOL_REGISTRY,
        timeout_seconds=timeout,
        lyzr_adapter=lyzr_adapter,
    )
    envelope = agent.run(body.payload, auth_ctx, idempotency_key=idempotency_key)
    if envelope.execution_status == "FAILED" and envelope.error_message:
        if "AgentBoundaryError" in envelope.error_message or "ToolPermissionError" in envelope.error_message:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=envelope.error_message,
            )
    return envelope


@router.get(
    "/executions",
    response_model=List[AgentExecutionAudit],
    summary="List sanitized agent execution audit logs",
)
def get_agent_executions(
    request: Request,
    agent_role: Optional[AgentRole] = Query(default=None),
    auth_tuple=Depends(require_admin_or_org_auth),
) -> List[AgentExecutionAudit]:
    is_admin, boundary = auth_tuple
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return list_execution_audits(
        db_path,
        agent_role=agent_role.value if agent_role else None,
        authorized_boundary_id=None if is_admin else (boundary.data_boundary_id if boundary else None),
    )
