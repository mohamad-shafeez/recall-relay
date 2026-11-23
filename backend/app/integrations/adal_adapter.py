"""AdaL CLI adapter (SylphAI-Inc/adal-cli, docs.sylph.ai).

AdaL is a terminal CLI / MCP agent harness with a Free tier and no documented public
REST API. RecallRelay therefore never invents an AdaL REST endpoint; it integrates
strictly via safe, shell-free bounded subprocess execution when ADAL_CLI_PATH is installed.
"""

from __future__ import annotations

import shutil
import subprocess

from app.integrations.base import (
    BaseSponsorAdapter,
    ConnectionState,
    ImplementationStatus,
    SponsorStatusReport,
    record_integration_usage,
)

MAX_CLI_OUTPUT_BYTES = 8192


class AdalCliAdapter(BaseSponsorAdapter):
    """Safe shell-free AdaL CLI integration adapter (no fabricated REST endpoints)."""

    sponsor_name = "AdaL"
    capability = "Terminal CLI / MCP agent harness for bounded recall response report generation (when installed)"
    official_docs_url = "https://docs.sylph.ai/"
    free_access_summary = "Free tier available via AdaL Terminal CLI (docs.sylph.ai); Pro plans are paid. No public REST API."
    card_or_wallet_required = False

    def __init__(
        self,
        *,
        db_path: str | None = None,
        cli_path: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, max_retries=0)
        self.db_path = db_path
        self._cli_path = cli_path.strip() if cli_path and cli_path.strip() else None

    def _resolved_binary(self) -> str | None:
        if not self._cli_path:
            return None
        return shutil.which(self._cli_path)

    def is_configured(self) -> bool:
        return self._resolved_binary() is not None

    def status_report(self) -> SponsorStatusReport:
        if not self.is_configured():
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.CONFIGURATION_MISSING,
                configured=False,
                endpoint_host="local-cli",
                message="AdaL CLI binary (ADAL_CLI_PATH) is not installed in this runtime environment. No fabricated REST endpoint is used.",
            )
        if self._last_report is not None:
            return self._last_report
        return self._build_report(
            implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
            connection_state=ConnectionState.NOT_CONNECTED,
            configured=True,
            endpoint_host="local-cli",
            message="AdaL CLI binary detected; run a check to verify version and responsiveness.",
        )

    def health_check(
        self, db_path: str | None = None, actor_id: str = "admin"
    ) -> SponsorStatusReport:
        target_db = db_path or self.db_path
        binary = self._resolved_binary()
        if not binary:
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.CONFIGURATION_MISSING,
                configured=False,
                endpoint_host="local-cli",
                message="AdaL CLI binary is not installed in this environment.",
                checked=True,
            )
        try:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            out = (proc.stdout or proc.stderr or "")[:MAX_CLI_OUTPUT_BYTES].strip()
            if proc.returncode == 0:
                if target_db:
                    record_integration_usage(
                        target_db,
                        sponsor_name=self.sponsor_name,
                        operation="cli_version_check",
                        actor_id=actor_id,
                        status="LIVE_VERIFIED",
                    )
                return self._build_report(
                    implementation_status=ImplementationStatus.LIVE_VERIFIED,
                    connection_state=ConnectionState.CONNECTED,
                    configured=True,
                    endpoint_host="local-cli",
                    message=f"AdaL CLI verified ({out[:120] or 'ok'}).",
                    checked=True,
                )
            return self._build_report(
                implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
                connection_state=ConnectionState.ERROR,
                configured=True,
                endpoint_host="local-cli",
                message=f"AdaL CLI exited with code {proc.returncode}.",
                checked=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return self._build_report(
                implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
                connection_state=ConnectionState.ERROR,
                configured=True,
                endpoint_host="local-cli",
                message=f"AdaL CLI check failed: {type(exc).__name__}.",
                checked=True,
            )
