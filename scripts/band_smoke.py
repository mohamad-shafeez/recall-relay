#!/usr/bin/env python3
"""Standalone live BAND verification smoke test (run locally with real .env credentials).

Usage:
    python scripts/band_smoke.py

Verifies:
1. Required BAND env vars are present (`BAND_SENDER_AGENT_ID`, `BAND_SENDER_API_KEY`,
   `BAND_RECEIVER_AGENT_ID`, `BAND_RECEIVER_API_KEY`, `BAND_ROOM_ID`).
2. Authenticated `GET /api/v1/agent/me` succeeds for BOTH sender and receiver (`X-API-Key`).
3. Two-way shared-room message round-trip (`POST /api/v1/agent/chats/{room_id}/messages` +
   `GET /api/v1/agent/chats/{room_id}/context`) delivers a message from sender to receiver and
   returns a reply from receiver to sender.

Security: NEVER prints API keys or secrets; exits non-zero on any failure.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.integrations.band_adapter import (  # noqa: E402
    DEFAULT_BAND_BASE_URL,
    _default_band_rest_list_messages,
    _default_band_rest_send,
    _extract_band_message_id,
    is_band_sdk_installed,
)
from app.integrations.base import redact_secrets  # noqa: E402

import httpx  # noqa: E402


def main() -> int:
    settings = get_settings()
    base_url = (settings.band_api_base_url or DEFAULT_BAND_BASE_URL).rstrip("/")
    sender_id = settings.band_sender_agent_id
    sender_key = settings.band_sender_api_key
    receiver_id = settings.band_receiver_agent_id
    receiver_key = settings.band_receiver_api_key
    room_id = settings.band_room_id
    secrets = [sender_key, receiver_key]

    missing = [
        name
        for name, val in [
            ("BAND_SENDER_AGENT_ID", sender_id),
            ("BAND_SENDER_API_KEY", sender_key),
            ("BAND_RECEIVER_AGENT_ID", receiver_id),
            ("BAND_RECEIVER_API_KEY", receiver_key),
            ("BAND_ROOM_ID", room_id),
        ]
        if not val
    ]
    if missing:
        print(f"FAIL: Missing required environment variables: {', '.join(missing)}")
        return 1
    if sender_id == receiver_id:
        print("FAIL: BAND_SENDER_AGENT_ID and BAND_RECEIVER_AGENT_ID must be distinct agent UUIDs.")
        return 1

    receiver_handle = (
        getattr(settings, "band_receiver_handle", None)
        or (receiver_id.split("/")[-1] if "/" in receiver_id else receiver_id)
    )
    sender_handle = (
        getattr(settings, "band_sender_handle", None)
        or (sender_id.split("/")[-1] if "/" in sender_id else sender_id)
    )

    print(f"BAND Smoke Test | base_url={base_url} | Transport: Band Agent REST API")
    print(f"Sender UUID: {sender_id} (handle: {sender_handle}) | Receiver UUID: {receiver_id} (handle: {receiver_handle}) | Room: {room_id}")

    try:
        for label, key in [("sender", sender_key), ("receiver", receiver_key)]:
            r = httpx.get(
                f"{base_url}/api/v1/agent/me",
                headers={"X-API-Key": key or "", "Accept": "application/json"},
                timeout=settings.external_http_timeout_seconds,
                follow_redirects=False,
            )
            if r.status_code < 200 or r.status_code >= 300:
                print(f"FAIL: GET /api/v1/agent/me for {label} returned HTTP {r.status_code}")
                return 2
            print(f"OK: {label} authenticated via GET /api/v1/agent/me (HTTP {r.status_code})")

        marker = f"recallrelay-smoke-{int(time.time())}"
        ping_content = json.dumps(
            {"protocol": "recallrelay.smoke.v1", "kind": "PING", "marker": marker},
            separators=(",", ":"),
        )
        outbound_resp = _default_band_rest_send(
            api_key=sender_key or "",
            agent_id=sender_id or "",
            room_id=room_id or "",
            content=ping_content,
            mention_agent_id=receiver_id,
            mention_handle=receiver_handle,
            rest_url=base_url,
            timeout_seconds=settings.external_http_timeout_seconds,
        )
        ext_msg_id = _extract_band_message_id(outbound_resp)
        if not ext_msg_id:
            print("FAIL: Outbound BAND send succeeded but returned no external message ID.")
            return 3
        print(f"OK: Outbound message delivered (external_message_id={ext_msg_id})")

        observed = False
        for _ in range(settings.band_poll_attempts):
            msgs = _default_band_rest_list_messages(
                api_key=receiver_key or "",
                agent_id=receiver_id or "",
                room_id=room_id or "",
                rest_url=base_url,
                timeout_seconds=settings.external_http_timeout_seconds,
            )
            if any(
                _extract_band_message_id(m) == ext_msg_id or marker in str(m.get("content") or "")
                for m in msgs
            ):
                observed = True
                break
            time.sleep(settings.band_poll_interval_seconds)
        if not observed:
            print("FAIL: Receiver did not observe outbound message in shared room within poll window.")
            return 4
        print("OK: Receiver observed outbound message in shared room")

        pong_content = json.dumps(
            {"protocol": "recallrelay.smoke.v1", "kind": "PONG", "in_reply_to": ext_msg_id},
            separators=(",", ":"),
        )
        reply_resp = _default_band_rest_send(
            api_key=receiver_key or "",
            agent_id=receiver_id or "",
            room_id=room_id or "",
            content=pong_content,
            mention_agent_id=sender_id,
            mention_handle=sender_handle,
            rest_url=base_url,
            timeout_seconds=settings.external_http_timeout_seconds,
        )
        ext_reply_id = _extract_band_message_id(reply_resp)
        if not ext_reply_id:
            print("FAIL: Receiver reply returned no external message ID.")
            return 5
        print(f"OK: Receiver reply delivered (external_reply_message_id={ext_reply_id})")
        print("PASS: BAND two-way smoke verification succeeded.")
        return 0
    except Exception as exc:
        print(f"FAIL: {redact_secrets(f'{type(exc).__name__}: {exc}', secrets)}")
        return 10


if __name__ == "__main__":
    raise SystemExit(main())
