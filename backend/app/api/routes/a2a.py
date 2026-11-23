"""Versioned API routes for durable A2A messaging, task coordination, handoffs, and audit trail."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.agents.base import AgentAuthContext, AgentRole
from app.communication.audit_trail import list_audit_events, verify_audit_chain
from app.communication.schemas import (
    A2AMessage,
    A2AMessageCreate,
    AgentTask,
    AgentTaskCreate,
    AuditChainVerification,
    AuditEvent,
    HandoffDispatchRequest,
    HandoffDispatchResponse,
)
from app.communication.task_service import (
    create_task,
    dispatch_agent_handoff,
    get_task,
    list_tasks,
)
from app.communication.transport import InternalSQLiteTransport, MessageTransport
from app.core.auth import (
    require_admin_auth,
    require_admin_or_org_auth,
    require_org_boundary,
)
from app.core.config import get_settings
from app.schemas.domain import OrganizationRole

router = APIRouter(prefix="/a2a", tags=["a2a"])

_ORG_AGENT_REQUIRED_ROLE = {
    AgentRole.MANUFACTURER_OPERATIONS_AGENT: OrganizationRole.MANUFACTURER,
    AgentRole.DISTRIBUTOR_RECONCILIATION_AGENT: OrganizationRole.DISTRIBUTOR,
    AgentRole.RETAILER_INVENTORY_AGENT: OrganizationRole.RETAILER,
}


def _get_active_transport(request: Request) -> MessageTransport:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    registry = getattr(request.app.state, "integration_registry", None)
    if registry is not None:
        return registry.resolve_a2a_transport(db_path)
    return InternalSQLiteTransport(db_path)


@router.post(
    "/messages",
    response_model=A2AMessage,
    status_code=status.HTTP_201_CREATED,
    summary="Create and enqueue a versioned A2A message via the active configured transport",
)
def create_a2a_message(
    payload: A2AMessageCreate,
    request: Request,
    auth_tuple=Depends(require_admin_or_org_auth),
) -> A2AMessage:
    is_admin, boundary = auth_tuple
    transport = _get_active_transport(request)
    return transport.send_message(
        payload,
        sender_boundary_id=boundary.data_boundary_id if boundary else None,
        actor_id="admin" if is_admin else boundary.organization_id,
    )


@router.get(
    "/messages",
    response_model=List[A2AMessage],
    summary="List A2A messages visible to the authenticated caller",
)
def list_a2a_messages(
    request: Request,
    recipient_agent: Optional[AgentRole] = Query(default=None),
    recall_id: Optional[str] = Query(default=None),
    auth_tuple=Depends(require_admin_or_org_auth),
) -> List[A2AMessage]:
    is_admin, boundary = auth_tuple
    db_path: str | None = getattr(request.app.state, "database_path", None)
    transport = InternalSQLiteTransport(db_path)
    return transport.list_messages(
        is_admin=is_admin,
        caller_boundary_id=boundary.data_boundary_id if boundary else None,
        recipient_agent=recipient_agent,
        recall_id=recall_id,
    )


@router.get(
    "/messages/{message_id}",
    response_model=A2AMessage,
    summary="Get an A2A message by ID (boundary-enforced)",
)
def get_a2a_message(
    message_id: str,
    request: Request,
    auth_tuple=Depends(require_admin_or_org_auth),
) -> A2AMessage:
    is_admin, boundary = auth_tuple
    db_path: str | None = getattr(request.app.state, "database_path", None)
    transport = InternalSQLiteTransport(db_path)
    return transport.get_message(
        message_id,
        is_admin=is_admin,
        caller_boundary_id=boundary.data_boundary_id if boundary else None,
    )


@router.post(
    "/messages/{message_id}/deliver",
    response_model=A2AMessage,
    summary="Deliver a PENDING A2A message (admin required)",
)
def deliver_a2a_message(
    message_id: str,
    request: Request,
    _admin: str = Depends(require_admin_auth),
) -> A2AMessage:
    transport = _get_active_transport(request)
    return transport.deliver_message(message_id, actor_id="admin")


@router.post(
    "/messages/{message_id}/acknowledge",
    response_model=A2AMessage,
    summary="Acknowledge receipt of a delivered A2A message",
)
def acknowledge_a2a_message(
    message_id: str,
    request: Request,
    auth_tuple=Depends(require_admin_or_org_auth),
) -> A2AMessage:
    is_admin, boundary = auth_tuple
    transport = _get_active_transport(request)
    return transport.acknowledge_message(
        message_id,
        actor_id="admin" if is_admin else boundary.organization_id,
        caller_boundary_id=boundary.data_boundary_id if boundary else None,
    )


@router.post(
    "/inbox/{recipient_agent}/process",
    summary="Atomically claim and process the next DELIVERED message for a specialist agent",
)
def process_agent_inbox(
    recipient_agent: AgentRole,
    request: Request,
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    authorization: Optional[str] = Header(default=None),
):
    db_path: str | None = getattr(request.app.state, "database_path", None)
    timeout = get_settings().agent_default_timeout_seconds

    required_role = _ORG_AGENT_REQUIRED_ROLE.get(recipient_agent)
    if required_role is not None:
        boundary = require_org_boundary(request, authorization=authorization)
        if boundary.role != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Inbox access denied: '{recipient_agent.value}' requires "
                    f"'{required_role.value}' organization role."
                ),
            )
        auth_ctx = AgentAuthContext(is_admin=False, boundary=boundary)
        worker_id = f"worker_{boundary.organization_id}"
    else:
        require_admin_auth(request, x_admin_key=x_admin_key)
        auth_ctx = AgentAuthContext(is_admin=True, boundary=None)
        worker_id = f"worker_{recipient_agent.value}"

    result = InternalSQLiteTransport(db_path).process_recipient_inbox(
        recipient_agent,
        auth=auth_ctx,
        worker_id=worker_id,
        timeout_seconds=timeout,
    )
    if result is None:
        return {"processed": False, "message": "No deliverable messages in inbox."}
    return {"processed": True, **result}


@router.post(
    "/tasks",
    response_model=AgentTask,
    status_code=status.HTTP_201_CREATED,
    summary="Create a durable agent task (admin required)",
)
def create_agent_task(
    payload: AgentTaskCreate,
    request: Request,
    _admin: str = Depends(require_admin_auth),
) -> AgentTask:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return create_task(
        db_path, payload, authorized_boundary_id=None, actor_id="admin"
    )


@router.get(
    "/tasks",
    response_model=List[AgentTask],
    summary="List durable agent tasks (boundary-scoped for organization callers)",
)
def list_agent_tasks(
    request: Request,
    recall_id: Optional[str] = Query(default=None),
    auth_tuple=Depends(require_admin_or_org_auth),
) -> List[AgentTask]:
    is_admin, boundary = auth_tuple
    db_path: str | None = getattr(request.app.state, "database_path", None)
    all_tasks = list_tasks(db_path, recall_id=recall_id)
    if is_admin:
        return all_tasks
    caller_bnd = boundary.data_boundary_id if boundary else None
    return [t for t in all_tasks if t.authorized_boundary_id == caller_bnd]


@router.get(
    "/tasks/{task_id}",
    response_model=AgentTask,
    summary="Get durable task status by ID (boundary-enforced for organization callers)",
)
def get_agent_task(
    task_id: str,
    request: Request,
    auth_tuple=Depends(require_admin_or_org_auth),
) -> AgentTask:
    is_admin, boundary = auth_tuple
    db_path: str | None = getattr(request.app.state, "database_path", None)
    task = get_task(db_path, task_id)
    if not is_admin:
        caller_bnd = boundary.data_boundary_id if boundary else None
        if task.authorized_boundary_id != caller_bnd:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: task belongs to another organization boundary.",
            )
    return task


@router.post(
    "/handoffs",
    response_model=HandoffDispatchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a durable task and dispatch a TASK_HANDOFF message via the active transport (admin required)",
)
def create_agent_handoff(
    payload: HandoffDispatchRequest,
    request: Request,
    recipient_boundary_id: Optional[str] = Query(default=None),
    _admin: str = Depends(require_admin_auth),
) -> HandoffDispatchResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    transport = _get_active_transport(request)
    return dispatch_agent_handoff(
        db_path,
        payload,
        sender_boundary_id=None,
        recipient_boundary_id=recipient_boundary_id,
        actor_id="admin",
        transport=transport,
    )


@router.get(
    "/audit-events",
    response_model=List[AuditEvent],
    summary="List hash-chained coordination audit events (admin required)",
)
def get_audit_events(
    request: Request,
    event_type: Optional[str] = Query(default=None),
    _admin: str = Depends(require_admin_auth),
) -> List[AuditEvent]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return list_audit_events(db_path, event_type=event_type)


@router.get(
    "/audit-events/verify",
    response_model=AuditChainVerification,
    summary="Verify integrity of the SHA-256 audit hash chain (admin required)",
)
def verify_audit_events(
    request: Request,
    _admin: str = Depends(require_admin_auth),
) -> AuditChainVerification:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return verify_audit_chain(db_path)
