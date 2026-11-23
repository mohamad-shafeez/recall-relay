"""Agent37 sponsor adapter — explicitly BLOCKED under RecallRelay's zero-payment/no-card policy.

Verified against https://www.agent37.com/pricing:
"New eligible accounts get $5 starter credit after adding a card."
"Add a card to unlock the starter credit. No charge until you top up."

Per RecallRelay's mandatory zero-payment / no-card policy, Agent37 sandbox execution
is NOT activated and NEVER simulated.
"""

from __future__ import annotations

from app.integrations.base import (
    BaseSponsorAdapter,
    ConnectionState,
    ImplementationStatus,
    SponsorStatusReport,
)


class Agent37Adapter(BaseSponsorAdapter):
    """Honest BLOCKED status adapter for Agent37 under zero-payment/no-card policy."""

    sponsor_name = "Agent37"
    capability = "Isolated Linux cloud sandboxes for independent organization agents"
    official_docs_url = "https://www.agent37.com/pricing"
    free_access_summary = (
        "No card-free tier: $5 starter credit requires adding a payment card "
        "(verified at agent37.com/pricing). Blocked by zero-payment policy."
    )
    card_or_wallet_required = True

    def __init__(self, *, api_key: str | None = None) -> None:
        super().__init__(timeout_seconds=10.0, max_retries=0)
        self._api_key = api_key.strip() if api_key and api_key.strip() else None

    def is_configured(self) -> bool:
        return False

    def status_report(self) -> SponsorStatusReport:
        return self._build_report(
            implementation_status=ImplementationStatus.BLOCKED,
            connection_state=ConnectionState.BLOCKED,
            configured=False,
            endpoint_host="www.agent37.com",
            message=(
                "BLOCKED by zero-payment/no-card policy: Agent37 requires adding a payment card "
                "to unlock starter credit (agent37.com/pricing). No simulated sandbox is used."
            ),
        )

    def health_check(
        self, db_path: str | None = None, actor_id: str = "admin"
    ) -> SponsorStatusReport:
        _ = db_path, actor_id
        return self._build_report(
            implementation_status=ImplementationStatus.BLOCKED,
            connection_state=ConnectionState.BLOCKED,
            configured=False,
            endpoint_host="www.agent37.com",
            message=(
                "BLOCKED: No card-free Agent37 sandbox access is available under the zero-payment policy."
            ),
            checked=True,
        )
