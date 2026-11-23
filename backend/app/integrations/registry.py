"""Centralized sponsor integration registry built from server environment settings."""

from __future__ import annotations

from typing import Dict

from fastapi import HTTPException, status

from app.communication.transport import InternalSQLiteTransport, MessageTransport
from app.core.config import Settings, get_settings
from app.integrations.adal_adapter import AdalCliAdapter
from app.integrations.agent37_adapter import Agent37Adapter
from app.integrations.band_adapter import BandMessageTransport
from app.integrations.base import (
    BaseSponsorAdapter,
    IntegrationOverviewResponse,
    SponsorStatusReport,
)
from app.integrations.finch_adapter import FinchMcpAdapter
from app.integrations.lyzr_adapter import LyzrReasoningAdapter


class IntegrationRegistry:
    """Centralized registry for all five RecallRelay sponsor adapters and explicit A2A transport selection."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        settings: Settings | None = None,
        a2a_transport_override: str | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self.db_path = db_path
        timeout = cfg.external_http_timeout_seconds
        retries = cfg.external_max_retries
        raw_transport = (a2a_transport_override or cfg.a2a_transport or "internal_sqlite").strip().lower()
        self.configured_a2a_transport = raw_transport if raw_transport in {"internal_sqlite", "band"} else "internal_sqlite"

        self._adapters: Dict[str, BaseSponsorAdapter] = {
            "band": BandMessageTransport(
                db_path=db_path,
                api_base_url=cfg.band_api_base_url,
                api_key=cfg.band_sender_api_key,
                agent_id=cfg.band_sender_agent_id,
                receiver_api_key=cfg.band_receiver_api_key,
                receiver_agent_id=cfg.band_receiver_agent_id,
                sender_handle=cfg.band_sender_handle,
                receiver_handle=cfg.band_receiver_handle,
                room_id=cfg.band_room_id,
                poll_attempts=cfg.band_poll_attempts,
                poll_interval_seconds=cfg.band_poll_interval_seconds,
                timeout_seconds=timeout,
                max_retries=retries,
            ),
            "lyzr": LyzrReasoningAdapter(
                db_path=db_path,
                api_base_url=cfg.lyzr_api_base_url,
                api_key=cfg.lyzr_api_key,
                agent_id=cfg.lyzr_agent_id,
                timeout_seconds=timeout,
                max_retries=retries,
            ),
            "finch": FinchMcpAdapter(
                db_path=db_path,
                mcp_url=cfg.finch_mcp_url,
                api_key=cfg.finch_api_key,
                cli_path=cfg.finch_cli_path,
                timeout_seconds=timeout,
                max_retries=retries,
            ),
            "adal": AdalCliAdapter(
                db_path=db_path,
                cli_path=cfg.adal_cli_path,
                timeout_seconds=timeout,
            ),
            "agent37": Agent37Adapter(
                api_key=cfg.agent37_api_key,
            ),
        }

    def get_adapter(self, sponsor_key: str) -> BaseSponsorAdapter:
        adapter = self._adapters.get(sponsor_key.strip().lower())
        if adapter is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown sponsor integration '{sponsor_key}'.",
            )
        return adapter

    def resolve_a2a_transport(self, db_path: str | None = None) -> MessageTransport:
        """Return BandMessageTransport ONLY when explicitly selected via A2A_TRANSPORT=band AND configured."""
        resolved_db = db_path or self.db_path
        if self.configured_a2a_transport == "band":
            band_adapter = self._adapters["band"]
            assert isinstance(band_adapter, BandMessageTransport)
            if band_adapter.is_configured():
                return band_adapter
        return InternalSQLiteTransport(resolved_db)

    def overview(self) -> IntegrationOverviewResponse:
        reports = [adapter.status_report() for adapter in self._adapters.values()]
        band_adapter = self._adapters["band"]
        lyzr_adapter = self._adapters["lyzr"]
        assert isinstance(band_adapter, BandMessageTransport)
        assert isinstance(lyzr_adapter, LyzrReasoningAdapter)

        active_transport = (
            "band_transport"
            if (self.configured_a2a_transport == "band" and band_adapter.is_configured())
            else "internal_sqlite_transport"
        )
        exec_mode = (
            "DETERMINISTIC + LYZR_ASSISTED"
            if lyzr_adapter.real_invocation_succeeded
            else "DETERMINISTIC"
        )
        return IntegrationOverviewResponse(
            configured_a2a_transport=self.configured_a2a_transport,
            active_a2a_transport=active_transport,
            band_real_message_succeeded=band_adapter.real_message_succeeded,
            lyzr_real_invocation_succeeded=lyzr_adapter.real_invocation_succeeded,
            agent_execution_mode=exec_mode,
            sponsors=reports,
        )

    def check_sponsor(
        self, sponsor_key: str, *, db_path: str | None = None, actor_id: str = "admin"
    ) -> SponsorStatusReport:
        adapter = self.get_adapter(sponsor_key)
        return adapter.health_check(db_path=db_path, actor_id=actor_id)
