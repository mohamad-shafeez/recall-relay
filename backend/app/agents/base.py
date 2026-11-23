"""Common base architecture for RecallRelay specialized agents.

Provides cooperative cancellation, deadline enforcement, step-limit enforcement,
scoped idempotency (agent + boundary + canonical input), structured validation,
backend-enforced tool dispatch, and execution auditing.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from enum import Enum
from typing import Any, Generic, List, Type, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.agents.audit import (
    AgentExecutionAudit,
    compute_idempotency_scope_hash,
    find_execution_by_scope_hash,
    finish_execution_audit,
    start_execution_audit,
)
from app.schemas.domain import OrganizationBoundaryContext

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class AgentRole(str, Enum):
    RECALL_SOURCE_ANALYST = "RECALL_SOURCE_ANALYST"
    RECALL_SCOPE_AGENT = "RECALL_SCOPE_AGENT"
    MANUFACTURER_OPERATIONS_AGENT = "MANUFACTURER_OPERATIONS_AGENT"
    DISTRIBUTOR_RECONCILIATION_AGENT = "DISTRIBUTOR_RECONCILIATION_AGENT"
    RETAILER_INVENTORY_AGENT = "RETAILER_INVENTORY_AGENT"
    INDEPENDENT_VERIFICATION_AGENT = "INDEPENDENT_VERIFICATION_AGENT"
    RECALL_RESPONSE_COORDINATOR = "RECALL_RESPONSE_COORDINATOR"


class ExecutionMode(str, Enum):
    """Explicitly identifies whether execution is deterministic rule-based or model-assisted."""

    DETERMINISTIC = "DETERMINISTIC"


class ToolPermissionError(PermissionError):
    """Raised when an agent attempts to invoke a tool outside its backend-enforced permissions."""


class AgentBoundaryError(PermissionError):
    """Raised when an agent's authorization context does not match its required org role/boundary."""


class AgentTimeoutError(TimeoutError):
    """Raised when an agent exceeds its bounded execution deadline."""


class AgentCancelledError(RuntimeError):
    """Raised cooperatively when a timed-out or cancelled agent attempts further actions."""


class AgentStepLimitError(RuntimeError):
    """Raised when an agent exceeds its declared max_steps limit."""


class AgentAuthContext(BaseModel):
    """Server-constructed authorization context passed into every agent execution."""

    model_config = ConfigDict(frozen=True)

    is_admin: bool = False
    boundary: OrganizationBoundaryContext | None = None


class ExecutionGuard:
    """Thread-local cooperative cancellation, deadline, and step-budget guard."""

    def __init__(self, *, cancel_event: threading.Event, deadline: float, max_steps: int) -> None:
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.max_steps = max_steps
        self.steps_taken = 0

    def check_and_consume_step(self) -> None:
        if self.cancel_event.is_set() or time.monotonic() > self.deadline:
            self.cancel_event.set()
            raise AgentCancelledError("Execution cancelled or deadline exceeded; side effect blocked.")
        if self.steps_taken >= self.max_steps:
            raise AgentStepLimitError(f"Agent exceeded max_steps limit ({self.max_steps}).")
        self.steps_taken += 1


class AgentExecutionEnvelope(BaseModel):
    """Structured execution result returned by BaseAgent.run()."""

    execution_id: str
    agent_id: str
    agent_role: AgentRole
    execution_mode: ExecutionMode = ExecutionMode.DETERMINISTIC
    execution_status: str
    reused_idempotent_result: bool = False
    authorized_boundary_id: str | None = None
    output: dict[str, Any] | None = None
    evidence_references: List[str] = Field(default_factory=list)
    error_message: str | None = None
    audit: AgentExecutionAudit


class BaseAgent(ABC, Generic[InputT, OutputT]):
    """Abstract base class enforcing lifecycle, cooperative cancellation, bounds, and auditing."""

    agent_id: str
    role: AgentRole
    input_schema: Type[InputT]
    output_schema: Type[OutputT]
    execution_mode: ExecutionMode = ExecutionMode.DETERMINISTIC
    max_steps: int = 10

    def __init__(
        self,
        db_path: str | None,
        tool_registry: Any,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.db_path = db_path
        self.tool_registry = tool_registry
        self.timeout_seconds = timeout_seconds
        self._guard_local = threading.local()

    def _current_guard(self) -> ExecutionGuard | None:
        return getattr(self._guard_local, "guard", None)

    def check_cancelled(self) -> None:
        """Cooperative checkpoint callable by agents before any work or side effect."""
        guard = self._current_guard()
        if guard is not None:
            if guard.cancel_event.is_set() or time.monotonic() > guard.deadline:
                guard.cancel_event.set()
                raise AgentCancelledError("Execution cancelled or deadline exceeded.")

    def invoke_tool(
        self, auth: AgentAuthContext, tool_name: str, **kwargs: Any
    ) -> Any:
        """Invoke a tool strictly through the backend-enforced ToolRegistry with cancellation/step guard."""
        guard = self._current_guard()
        if guard is not None:
            guard.check_and_consume_step()
        return self.tool_registry.invoke(
            agent_role=self.role,
            auth=auth,
            tool_name=tool_name,
            db_path=self.db_path,
            **kwargs,
        )

    @abstractmethod
    def extract_input_references(self, validated_input: InputT) -> List[str]:
        """Return sanitized record IDs referenced by the input (never secrets)."""

    @abstractmethod
    def extract_output_and_evidence_references(
        self, validated_output: OutputT
    ) -> tuple[List[str], List[str]]:
        """Return (output_record_ids, evidence_ids) for execution auditing."""

    @abstractmethod
    def execute(self, validated_input: InputT, auth: AgentAuthContext) -> OutputT:
        """Run the agent's specialized domain logic using only permitted tools."""

    def _run_guarded(
        self,
        validated_input: InputT,
        auth: AgentAuthContext,
        guard: ExecutionGuard,
    ) -> OutputT:
        self._guard_local.guard = guard
        try:
            self.check_cancelled()
            return self.execute(validated_input, auth)
        finally:
            self._guard_local.guard = None

    def run(
        self,
        raw_input: InputT | dict[str, Any],
        auth: AgentAuthContext,
        *,
        idempotency_key: str | None = None,
    ) -> AgentExecutionEnvelope:
        """Execute the full audited, cooperatively cancellable, scoped-idempotent lifecycle."""
        validated_input = (
            raw_input
            if isinstance(raw_input, self.input_schema)
            else self.input_schema.model_validate(raw_input)
        )
        boundary_id = auth.boundary.data_boundary_id if auth.boundary else None

        scope_hash: str | None = None
        if idempotency_key:
            scope_hash = compute_idempotency_scope_hash(
                agent_id=self.agent_id,
                authorized_boundary_id=boundary_id,
                canonical_input=validated_input.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
            existing = find_execution_by_scope_hash(self.db_path, scope_hash)
            if existing is not None:
                return AgentExecutionEnvelope(
                    execution_id=existing.id,
                    agent_id=existing.agent_id,
                    agent_role=AgentRole(existing.agent_role),
                    execution_mode=self.execution_mode,
                    execution_status=existing.execution_status,
                    reused_idempotent_result=True,
                    authorized_boundary_id=existing.authorized_boundary_id,
                    output=None,
                    evidence_references=existing.evidence_references,
                    error_message=existing.error_message,
                    audit=existing,
                )

        input_refs = self.extract_input_references(validated_input)
        audit = start_execution_audit(
            self.db_path,
            agent_id=self.agent_id,
            agent_role=self.role.value,
            authorized_boundary_id=boundary_id,
            input_references=input_refs,
            idempotency_key=idempotency_key,
            idempotency_scope_hash=scope_hash,
        )

        cancel_event = threading.Event()
        guard = ExecutionGuard(
            cancel_event=cancel_event,
            deadline=time.monotonic() + self.timeout_seconds,
            max_steps=self.max_steps,
        )

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._run_guarded, validated_input, auth, guard)
        try:
            raw_output = future.result(timeout=self.timeout_seconds)
            validated_output = (
                raw_output
                if isinstance(raw_output, self.output_schema)
                else self.output_schema.model_validate(raw_output)
            )
            out_refs, ev_refs = self.extract_output_and_evidence_references(
                validated_output
            )
            completed_audit = finish_execution_audit(
                self.db_path,
                execution_id=audit.id,
                execution_status="COMPLETED",
                output_references=out_refs,
                evidence_references=ev_refs,
            )
            return AgentExecutionEnvelope(
                execution_id=completed_audit.id,
                agent_id=self.agent_id,
                agent_role=self.role,
                execution_mode=self.execution_mode,
                execution_status="COMPLETED",
                reused_idempotent_result=False,
                authorized_boundary_id=boundary_id,
                output=validated_output.model_dump(),
                evidence_references=completed_audit.evidence_references,
                audit=completed_audit,
            )
        except FuturesTimeoutError:
            cancel_event.set()
            future.cancel()
            msg = (
                f"Agent '{self.agent_id}' exceeded bounded execution timeout "
                f"of {self.timeout_seconds}s; cooperative cancellation signaled."
            )
            failed_audit = finish_execution_audit(
                self.db_path,
                execution_id=audit.id,
                execution_status="TIMED_OUT",
                output_references=[],
                evidence_references=[],
                error_message=msg,
            )
            return AgentExecutionEnvelope(
                execution_id=failed_audit.id,
                agent_id=self.agent_id,
                agent_role=self.role,
                execution_mode=self.execution_mode,
                execution_status="TIMED_OUT",
                reused_idempotent_result=False,
                authorized_boundary_id=boundary_id,
                output=None,
                evidence_references=[],
                error_message=msg,
                audit=failed_audit,
            )
        except Exception as exc:
            cancel_event.set()
            msg = f"{type(exc).__name__}: {exc}"
            failed_audit = finish_execution_audit(
                self.db_path,
                execution_id=audit.id,
                execution_status="FAILED",
                output_references=[],
                evidence_references=[],
                error_message=msg,
            )
            return AgentExecutionEnvelope(
                execution_id=failed_audit.id,
                agent_id=self.agent_id,
                agent_role=self.role,
                execution_mode=self.execution_mode,
                execution_status="FAILED",
                reused_idempotent_result=False,
                authorized_boundary_id=boundary_id,
                output=None,
                evidence_references=[],
                error_message=msg,
                audit=failed_audit,
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
