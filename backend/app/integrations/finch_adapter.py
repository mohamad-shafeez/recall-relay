"""Finch sponsor adapter targeting Finch Remote MCP (https://www.finchtech.ai/mcp) and finchtech-cli.

Never invents local skills and claims they came from Finch.
"""

from __future__ import annotations

import shutil
import subprocess

import httpx

from app.integrations.base import (
    BaseSponsorAdapter,
    ConnectionState,
    ImplementationStatus,
    SponsorStatusReport,
    execute_with_bounded_retries,
    record_integration_usage,
    redact_secrets,
    validate_sponsor_https_url,
)


class FinchMcpAdapter(BaseSponsorAdapter):
    """Real JSON-RPC 2.0 MCP client + optional finchtech-cli check for Finch."""

    sponsor_name = "Finch"
    capability = "Remote MCP skill discovery and execution (https://www.finchtech.ai/mcp) / finchtech-cli"
    official_docs_url = "https://www.finchtech.ai/en/story"
    free_access_summary = "Public Finch Remote MCP & finchtech-cli; protected skills may be free but require explicit acquisition."
    card_or_wallet_required = False

    def __init__(
        self,
        *,
        db_path: str | None = None,
        mcp_url: str = "https://www.finchtech.ai/mcp",
        api_key: str | None = None,
        cli_path: str | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        allow_dev_localhost: bool = False,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, max_retries=max_retries)
        self.db_path = db_path
        self._api_key = api_key.strip() if api_key and api_key.strip() else None
        self._cli_path = cli_path.strip() if cli_path and cli_path.strip() else None
        self._config_error: str | None = None
        self._mcp_url: str | None = None
        self._host: str | None = None
        try:
            self._mcp_url, self._host = validate_sponsor_https_url(
                mcp_url, allow_dev_localhost=allow_dev_localhost
            )
        except ValueError as exc:
            self._config_error = str(exc)

    def _has_cli(self) -> bool:
        return bool(self._cli_path and shutil.which(self._cli_path))

    def is_configured(self) -> bool:
        # Requires explicit FINCH_API_KEY or installed finchtech-cli for authenticated skill usage
        return bool((self._mcp_url and self._api_key and not self._config_error) or self._has_cli())

    def status_report(self) -> SponsorStatusReport:
        if self._config_error:
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.ERROR,
                configured=False,
                endpoint_host=None,
                message=f"Invalid Finch MCP URL: {self._config_error}",
            )
        if not self.is_configured():
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.CONFIGURATION_MISSING,
                configured=False,
                endpoint_host=self._host,
                message="Configure FINCH_API_KEY (or FINCH_CLI_PATH for finchtech-cli) to connect to Finch Remote MCP; no local fake skills are used.",
            )
        if self._last_report is not None:
            return self._last_report
        return self._build_report(
            implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
            connection_state=ConnectionState.NOT_CONNECTED,
            configured=True,
            endpoint_host=self._host or "local-cli",
            message="Finch MCP/CLI configured; run a live check to verify tool discovery.",
        )

    def health_check(
        self, db_path: str | None = None, actor_id: str = "admin"
    ) -> SponsorStatusReport:
        target_db = db_path or self.db_path
        if self._config_error:
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.ERROR,
                configured=False,
                endpoint_host=None,
                message=f"Invalid Finch MCP URL: {self._config_error}",
                checked=True,
            )
        if self._has_cli():
            resolved = shutil.which(self._cli_path or "")
            try:
                proc = subprocess.run(
                    [resolved or "", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
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
                        message="finchtech-cli binary verified.",
                        checked=True,
                    )
            except (OSError, subprocess.SubprocessError):
                pass

        if not (self._mcp_url and self._api_key):
            return self._build_report(
                implementation_status=ImplementationStatus.IMPLEMENTED_NOT_CONNECTED,
                connection_state=ConnectionState.CONFIGURATION_MISSING,
                configured=False,
                endpoint_host=self._host,
                message="Finch MCP credentials are not configured.",
                checked=True,
            )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        mcp_init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "RecallRelay", "version": "0.8.0"},
            },
        }
        try:
            resp = execute_with_bounded_retries(
                lambda: httpx.post(
                    self._mcp_url or "",
                    headers=headers,
                    json=mcp_init,
                    timeout=self.timeout_seconds,
                ),
                max_retries=self.max_retries,
            )
            if 200 <= resp.status_code < 300:
                if target_db:
                    record_integration_usage(
                        target_db,
                        sponsor_name=self.sponsor_name,
                        operation="mcp_initialize",
                        actor_id=actor_id,
                        status="LIVE_VERIFIED",
                    )
                return self._build_report(
                    implementation_status=ImplementationStatus.LIVE_VERIFIED,
                    connection_state=ConnectionState.CONNECTED,
                    configured=True,
                    endpoint_host=self._host,
                    message=f"Finch Remote MCP handshake verified (HTTP {resp.status_code}).",
                    checked=True,
                )
            msg = f"Finch Remote MCP returned HTTP {resp.status_code}."
            return self._build_report(
                implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
                connection_state=ConnectionState.ERROR,
                configured=True,
                endpoint_host=self._host,
                message=msg,
                checked=True,
            )
        except httpx.HTTPError as exc:
            err = redact_secrets(f"Finch MCP unreachable: {type(exc).__name__}", [self._api_key])
            return self._build_report(
                implementation_status=ImplementationStatus.CONNECTED_UNVERIFIED,
                connection_state=ConnectionState.ERROR,
                configured=True,
                endpoint_host=self._host,
                message=err,
                checked=True,
            )
