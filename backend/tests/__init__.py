"""Backend test suite for RecallRelay."""

import os
from app.core.config import get_settings

# Isolate unit test suites from local .env live credentials unless explicitly enabled
if os.environ.get("RECALLRELAY_LIVE_SMOKE") != "1":
    for _key in (
        "BAND_SENDER_API_KEY",
        "BAND_RECEIVER_API_KEY",
        "BAND_RECEIVER_AGENT_ID",
        "LYZR_API_KEY",
        "LYZR_AGENT_ID",
    ):
        os.environ.pop(_key, None)
    get_settings.cache_clear()
