"""SQLite connection management and idempotent versioned schema migrations."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.core.config import get_settings

SCHEMA_VERSION_PHASE2 = "002_phase2_multi_org_schema"
SCHEMA_VERSION_PHASE3 = "003_phase3_boundary_auth_and_evidence_stage"
SCHEMA_VERSION_PHASE4 = "004_phase4_security_and_agent_audit"
SCHEMA_VERSION_PHASE5 = "005_phase5_a2a_coordination"
SCHEMA_VERSION_PHASE7 = "006_phase7_security_hardening"
SCHEMA_VERSION_PHASE8 = "007_phase8_bff_sessions_and_integrations"
SCHEMA_VERSION_PHASE9 = "008_phase9_audit_hardening"
SCHEMA_VERSION_PHASE9_AUDIT2 = "009_phase9_second_audit"
SCHEMA_VERSION_PHASE9_AUDIT3 = "010_phase9_third_audit"
SCHEMA_VERSION = SCHEMA_VERSION_PHASE9_AUDIT3

_SCHEMA_SQL_PATH = Path(__file__).resolve().parent / "schema.sql"


def _ensure_parent_directory(db_path: str) -> None:
    if db_path == ":memory:" or db_path.startswith("file:"):
        return
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with PRAGMA foreign_keys = ON and busy timeout."""
    resolved_path = db_path or get_settings().database_path
    _ensure_parent_directory(resolved_path)
    connection = sqlite3.connect(resolved_path, timeout=10.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    if resolved_path != ":memory:":
        connection.execute("PRAGMA journal_mode = WAL;")
    return connection


@contextmanager
def get_db_connection(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager yielding an initialized SQLite connection."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _apply_phase3_migration(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS organization_credentials (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_org_credentials_org
        ON organization_credentials(organization_id)
        """
    )

    if not _column_exists(conn, "evidence_records", "evidence_stage"):
        conn.execute(
            """
            ALTER TABLE evidence_records
            ADD COLUMN evidence_stage TEXT NOT NULL DEFAULT 'SOURCE_MATERIAL'
            CHECK (
                evidence_stage IN (
                    'SOURCE_MATERIAL',
                    'AI_EXTRACTED',
                    'INDEPENDENTLY_VERIFIED'
                )
            )
            """
        )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION_PHASE3, datetime.now(timezone.utc).isoformat()),
    )


def _apply_phase4_migration(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "organizations", "identity_verification_status"):
        conn.execute(
            """
            ALTER TABLE organizations
            ADD COLUMN identity_verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED'
            CHECK (
                identity_verification_status IN (
                    'UNVERIFIED',
                    'PENDING_VERIFICATION',
                    'VERIFIED',
                    'REJECTED'
                )
            )
            """
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_executions (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL CHECK (length(trim(agent_id)) > 0),
            agent_role TEXT NOT NULL CHECK (length(trim(agent_role)) > 0),
            authorized_boundary_id TEXT,
            idempotency_key TEXT,
            input_references_json TEXT NOT NULL,
            output_references_json TEXT NOT NULL,
            evidence_references_json TEXT NOT NULL,
            execution_status TEXT NOT NULL CHECK (
                execution_status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'TIMED_OUT')
            ),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_executions_role_status
        ON agent_executions(agent_role, execution_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_executions_boundary
        ON agent_executions(authorized_boundary_id)
        """
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION_PHASE4, datetime.now(timezone.utc).isoformat()),
    )


def _apply_phase5_migration(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "recall_notices", "public_disclosure_approved"):
        conn.execute(
            """
            ALTER TABLE recall_notices
            ADD COLUMN public_disclosure_approved INTEGER NOT NULL DEFAULT 0
            CHECK (public_disclosure_approved IN (0, 1))
            """
        )

    if not _column_exists(conn, "agent_executions", "idempotency_scope_hash"):
        conn.execute(
            "ALTER TABLE agent_executions ADD COLUMN idempotency_scope_hash TEXT"
        )
    if not _column_exists(conn, "agent_executions", "input_payload_hash"):
        conn.execute(
            "ALTER TABLE agent_executions ADD COLUMN input_payload_hash TEXT"
        )
    if not _column_exists(conn, "agent_executions", "result_json"):
        conn.execute("ALTER TABLE agent_executions ADD COLUMN result_json TEXT")
    if not _column_exists(conn, "human_approvals", "proposing_agent_id"):
        conn.execute("ALTER TABLE human_approvals ADD COLUMN proposing_agent_id TEXT")
    if not _column_exists(conn, "organizations", "identity_verified_by"):
        conn.execute("ALTER TABLE organizations ADD COLUMN identity_verified_by TEXT")
    if not _column_exists(conn, "organizations", "identity_verified_at"):
        conn.execute("ALTER TABLE organizations ADD COLUMN identity_verified_at TEXT")
    if not _column_exists(conn, "organizations", "identity_verification_evidence_id"):
        conn.execute("ALTER TABLE organizations ADD COLUMN identity_verification_evidence_id TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            parent_task_id TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
            recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL CHECK (length(trim(conversation_id)) > 0),
            assigned_agent TEXT NOT NULL CHECK (length(trim(assigned_agent)) > 0),
            requested_action TEXT NOT NULL CHECK (length(trim(requested_action)) > 0),
            input_evidence_references_json TEXT NOT NULL,
            required_output_schema TEXT NOT NULL CHECK (length(trim(required_output_schema)) > 0),
            status TEXT NOT NULL CHECK (
                status IN (
                    'PENDING',
                    'RUNNING',
                    'COMPLETED',
                    'FAILED',
                    'EXPIRED',
                    'BLOCKED_MISSING_EVIDENCE',
                    'BLOCKED_MISSING_AUTH'
                )
            ),
            authorized_boundary_id TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
            max_retries INTEGER NOT NULL DEFAULT 2 CHECK (max_retries >= 0),
            deadline_at TEXT,
            result_reference TEXT,
            failure_reason TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_dependencies (
            task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
            depends_on_task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
            PRIMARY KEY (task_id, depends_on_task_id),
            CHECK (task_id <> depends_on_task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS a2a_messages (
            id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL DEFAULT '1.0',
            conversation_id TEXT NOT NULL CHECK (length(trim(conversation_id)) > 0),
            task_id TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
            recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
            sender_agent TEXT NOT NULL CHECK (length(trim(sender_agent)) > 0),
            recipient_agent TEXT NOT NULL CHECK (length(trim(recipient_agent)) > 0),
            sender_boundary_id TEXT,
            recipient_boundary_id TEXT,
            message_type TEXT NOT NULL CHECK (
                message_type IN (
                    'TASK_HANDOFF',
                    'EVIDENCE_SHARE_NOTICE',
                    'RESULT_REPORT',
                    'ACK',
                    'ERROR_REPORT'
                )
            ),
            payload_json TEXT NOT NULL,
            evidence_references_json TEXT NOT NULL,
            idempotency_key TEXT,
            status TEXT NOT NULL CHECK (
                status IN (
                    'PENDING',
                    'DELIVERED',
                    'PROCESSING',
                    'PROCESSED',
                    'FAILED',
                    'EXPIRED'
                )
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1),
            claimed_by TEXT,
            claimed_at TEXT,
            delivered_at TEXT,
            acknowledged_at TEXT,
            processed_at TEXT,
            expires_at TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_a2a_messages_recipient_status
        ON a2a_messages(recipient_agent, status, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS information_sharing_grants (
            id TEXT PRIMARY KEY,
            recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
            owner_org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            owner_boundary_id TEXT NOT NULL CHECK (length(trim(owner_boundary_id)) > 0),
            recipient_org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            recipient_boundary_id TEXT NOT NULL CHECK (length(trim(recipient_boundary_id)) > 0),
            allowed_fields_json TEXT NOT NULL,
            allowed_evidence_ids_json TEXT NOT NULL,
            purpose TEXT NOT NULL CHECK (length(trim(purpose)) > 0),
            authorized_by TEXT NOT NULL CHECK (length(trim(authorized_by)) > 0),
            status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'REVOKED', 'EXPIRED')),
            expires_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
            actor_id TEXT NOT NULL CHECK (length(trim(actor_id)) > 0),
            boundary_id TEXT,
            entity_type TEXT NOT NULL CHECK (length(trim(entity_type)) > 0),
            entity_id TEXT NOT NULL CHECK (length(trim(entity_id)) > 0),
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL UNIQUE CHECK (length(event_hash) = 64)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS org_verification_reviews (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            previous_status TEXT NOT NULL,
            new_status TEXT NOT NULL CHECK (
                new_status IN ('UNVERIFIED', 'PENDING_VERIFICATION', 'VERIFIED', 'REJECTED')
            ),
            reviewed_by TEXT NOT NULL CHECK (length(trim(reviewed_by)) > 0),
            evidence_id TEXT REFERENCES evidence_records(id) ON DELETE RESTRICT,
            rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION_PHASE5, datetime.now(timezone.utc).isoformat()),
    )


def _apply_phase7_migration(conn: sqlite3.Connection) -> None:
    """Apply Phase 7 security hardening: boundary-scoped A2A message idempotency."""
    if not _column_exists(conn, "a2a_messages", "idempotency_scope_hash"):
        conn.execute(
            "ALTER TABLE a2a_messages ADD COLUMN idempotency_scope_hash TEXT"
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_a2a_messages_scope_hash
        ON a2a_messages(idempotency_scope_hash)
        WHERE idempotency_scope_hash IS NOT NULL
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION_PHASE7, datetime.now(timezone.utc).isoformat()),
    )


def _apply_phase8_migration(conn: sqlite3.Connection) -> None:
    """Apply Phase 8 additive migration: BFF HttpOnly sessions and sanitized external integration logs."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bff_sessions (
            id TEXT PRIMARY KEY,
            session_token_hash TEXT NOT NULL UNIQUE CHECK (length(session_token_hash) = 64),
            csrf_token TEXT NOT NULL CHECK (length(trim(csrf_token)) >= 16),
            principal_type TEXT NOT NULL CHECK (principal_type IN ('ADMIN', 'ORGANIZATION')),
            organization_id TEXT REFERENCES organizations(id) ON DELETE CASCADE,
            organization_role TEXT,
            data_boundary_id TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bff_sessions_org
        ON bff_sessions(organization_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS external_integration_logs (
            id TEXT PRIMARY KEY,
            sponsor_name TEXT NOT NULL CHECK (length(trim(sponsor_name)) > 0),
            operation TEXT NOT NULL CHECK (length(trim(operation)) > 0),
            actor_id TEXT NOT NULL CHECK (length(trim(actor_id)) > 0),
            boundary_id TEXT,
            external_reference_id TEXT,
            status TEXT NOT NULL CHECK (length(trim(status)) > 0),
            error_summary TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION_PHASE8, datetime.now(timezone.utc).isoformat()),
    )


def _apply_phase9_migration(conn: sqlite3.Connection) -> None:
    """Apply Phase 9 audit hardening: external transport metadata on A2A messages & login throttling table."""
    if not _column_exists(conn, "a2a_messages", "external_message_id"):
        conn.execute(
            "ALTER TABLE a2a_messages ADD COLUMN external_message_id TEXT"
        )
    if not _column_exists(conn, "a2a_messages", "transport_provider"):
        conn.execute(
            "ALTER TABLE a2a_messages ADD COLUMN transport_provider TEXT NOT NULL DEFAULT 'internal_sqlite'"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL CHECK (length(trim(scope_key)) > 0),
            attempted_at TEXT NOT NULL,
            success INTEGER NOT NULL CHECK (success IN (0, 1))
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_login_attempts_scope_time
        ON login_attempts(scope_key, attempted_at)
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION_PHASE9, datetime.now(timezone.utc).isoformat()),
    )




def _apply_phase9_audit2_migration(conn: sqlite3.Connection) -> None:
    """Apply migration 009: two-way BAND reply IDs on a2a_messages & persisted orchestration timeline tables."""
    if not _column_exists(conn, "a2a_messages", "external_reply_message_id"):
        conn.execute(
            "ALTER TABLE a2a_messages ADD COLUMN external_reply_message_id TEXT"
        )
    if not _column_exists(conn, "a2a_messages", "external_receiver_agent_id"):
        conn.execute(
            "ALTER TABLE a2a_messages ADD COLUMN external_receiver_agent_id TEXT"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_runs (
            id TEXT PRIMARY KEY,
            recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
            idempotency_key TEXT UNIQUE,
            status TEXT NOT NULL CHECK (
                status IN ('RUNNING', 'COMPLETED', 'COMPLETED_WITH_GAPS', 'PARTIAL_FAILURE', 'FAILED')
            ),
            active_transport TEXT NOT NULL,
            lyzr_used INTEGER NOT NULL DEFAULT 0 CHECK (lyzr_used IN (0, 1)),
            band_used INTEGER NOT NULL DEFAULT 0 CHECK (band_used IN (0, 1)),
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_steps (
            id TEXT PRIMARY KEY,
            orchestration_id TEXT NOT NULL REFERENCES orchestration_runs(id) ON DELETE CASCADE,
            step_order INTEGER NOT NULL,
            step_key TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('PENDING', 'RUNNING', 'COMPLETED', 'SKIPPED', 'BLOCKED', 'AWAITING_EVIDENCE', 'NOT_CONNECTED', 'FAILED')
            ),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            evidence_references_json TEXT NOT NULL,
            details_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_steps_run
        ON orchestration_steps(orchestration_id, step_order)
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION_PHASE9_AUDIT2, datetime.now(timezone.utc).isoformat()),
    )




def _apply_phase9_audit3_migration(conn: sqlite3.Connection) -> None:
    """Apply migration 010: persistent sponsor verification evidence & async orchestration recovery."""
    import hashlib

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sponsor_verification_evidence (
            id TEXT PRIMARY KEY,
            sponsor_name TEXT NOT NULL CHECK (length(trim(sponsor_name)) > 0),
            operation_type TEXT NOT NULL CHECK (length(trim(operation_type)) > 0),
            external_reference_id TEXT,
            sender_identity TEXT,
            receiver_identity TEXT,
            summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
            verified_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sponsor_verification_evidence_name
        ON sponsor_verification_evidence(sponsor_name, verified_at DESC)
        """
    )

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orchestration_runs'"
    ).fetchone()
    table_sql = row["sql"] if row and row["sql"] else ""
    if "INTERRUPTED" not in table_sql or "idempotency_scope_hash" not in table_sql:
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute(
            """
            CREATE TABLE orchestration_runs_v10 (
                id TEXT PRIMARY KEY,
                recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
                requester_scope TEXT NOT NULL DEFAULT 'admin',
                idempotency_key TEXT,
                idempotency_scope_hash TEXT,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'PENDING',
                        'RUNNING',
                        'COMPLETED',
                        'COMPLETED_WITH_GAPS',
                        'PARTIAL_FAILURE',
                        'FAILED',
                        'INTERRUPTED'
                    )
                ),
                active_transport TEXT NOT NULL,
                lyzr_used INTEGER NOT NULL DEFAULT 0 CHECK (lyzr_used IN (0, 1)),
                band_used INTEGER NOT NULL DEFAULT 0 CHECK (band_used IN (0, 1)),
                summary_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        existing_rows = conn.execute("SELECT * FROM orchestration_runs").fetchall()
        for existing in existing_rows:
            ikey = existing["idempotency_key"]
            req_scope = "admin"
            scope_hash = (
                hashlib.sha256(f"{existing['recall_id']}|{req_scope}|{ikey}".encode("utf-8")).hexdigest()
                if ikey
                else None
            )
            conn.execute(
                """
                INSERT INTO orchestration_runs_v10 (
                    id, recall_id, requester_scope, idempotency_key, idempotency_scope_hash,
                    status, active_transport, lyzr_used, band_used, summary_json,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    existing["id"],
                    existing["recall_id"],
                    req_scope,
                    ikey,
                    scope_hash,
                    existing["status"],
                    existing["active_transport"],
                    existing["lyzr_used"],
                    existing["band_used"],
                    existing["summary_json"],
                    existing["created_at"],
                    existing["completed_at"],
                ),
            )
        conn.execute("DROP TABLE orchestration_runs")
        conn.execute("ALTER TABLE orchestration_runs_v10 RENAME TO orchestration_runs")
        conn.execute("PRAGMA foreign_keys = ON;")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_orchestration_runs_scope_hash
        ON orchestration_runs(idempotency_scope_hash)
        WHERE idempotency_scope_hash IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_runs_recall_status
        ON orchestration_runs(recall_id, status)
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION_PHASE9_AUDIT3, datetime.now(timezone.utc).isoformat()),
    )


def mark_stale_running_orchestrations_interrupted(db_path: str | None = None) -> int:
    """Startup recovery: mark any stale PENDING/RUNNING orchestrations as INTERRUPTED."""
    import json

    now = datetime.now(timezone.utc).isoformat()
    with get_db_connection(db_path) as conn:
        stale_rows = conn.execute(
            "SELECT id, summary_json FROM orchestration_runs WHERE status IN ('PENDING', 'RUNNING')"
        ).fetchall()
        for row in stale_rows:
            try:
                summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
            except ValueError:
                summary = {}
            summary["interrupted_reason"] = (
                "Server restarted while orchestration was in progress; marked INTERRUPTED."
            )
            conn.execute(
                """
                UPDATE orchestration_runs
                SET status = 'INTERRUPTED', summary_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (json.dumps(summary), now, row["id"]),
            )
            conn.execute(
                """
                UPDATE orchestration_steps
                SET status = 'FAILED', completed_at = ?
                WHERE orchestration_id = ? AND status IN ('PENDING', 'RUNNING')
                """,
                (now, row["id"]),
            )
    return len(stale_rows)


def init_db(db_path: str | None = None) -> str:
    """Apply Phase 2–9 SQLite migrations idempotently without data loss."""
    schema_sql = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    with get_db_connection(db_path) as conn:
        conn.executescript(schema_sql)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION_PHASE2, datetime.now(timezone.utc).isoformat()),
        )
        _apply_phase3_migration(conn)
        _apply_phase4_migration(conn)
        _apply_phase5_migration(conn)
        _apply_phase7_migration(conn)
        _apply_phase8_migration(conn)
        _apply_phase9_migration(conn)
        _apply_phase9_audit2_migration(conn)
        _apply_phase9_audit3_migration(conn)
    return SCHEMA_VERSION


def check_db_health(db_path: str | None = None) -> bool:
    """Verify SQLite is reachable, foreign_keys is ON, and latest migration is applied."""
    try:
        with get_db_connection(db_path) as conn:
            fk_row = conn.execute("PRAGMA foreign_keys").fetchone()
            if not fk_row or fk_row[0] != 1:
                return False
            row = conn.execute(
                "SELECT version FROM schema_migrations WHERE version = ?",
                (SCHEMA_VERSION,),
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False
