#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Mock SMO HTTP receiver used by the O1 adapter PM streaming tests.

Behaviour:
  - Accepts JSON metric envelopes via POST /json and appends each to an event log.
  - After MOCK_SMO_TRIGGER_AFTER envelopes have arrived, closes the loop by
    sending a NETCONF edit-config (payload XML read from the currently-configured
    payload file) against the configured netconf server. The trigger is
    once-latched per (re)configuration.
  - POST /configure {payload_file, clear_log} swaps the payload, re-arms the
    trigger, and optionally truncates the event log so a new test starts clean.
  - The triggered edit is recorded in the event log so tests can assert it ran.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web
from ncclient import manager

EVENT_LOG_PATH = Path(os.getenv("MOCK_SMO_EVENT_LOG", "/tmp/mock_smo_events.jsonl"))
PORT = int(os.getenv("MOCK_SMO_PORT", "9560"))
TRIGGER_AFTER = int(os.getenv("MOCK_SMO_TRIGGER_AFTER", "3"))
NETCONF_HOST = os.getenv("NETCONF_HOST", "ocudu-netconf")
NETCONF_PORT = int(os.getenv("NETCONF_PORT", "830"))
NETCONF_USERNAME = os.getenv("NETCONF_USERNAME", "root")
NETCONF_PASSWORD = os.getenv("NETCONF_PASSWORD", "root")

EVENT_LOG_LOCK = asyncio.Lock()


async def _append_event(event: dict):
    EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(event)

    def _write_line():
        with EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")

    async with EVENT_LOG_LOCK:
        await asyncio.to_thread(_write_line)


def _send_netconf_edit(payload_file: str):
    payload = Path(payload_file).read_text(encoding="utf-8")
    with manager.connect(
        host=NETCONF_HOST,
        port=NETCONF_PORT,
        username=NETCONF_USERNAME,
        password=NETCONF_PASSWORD,
        hostkey_verify=False,
        allow_agent=False,
        look_for_keys=False,
        timeout=10,
    ) as ncm:
        ncm.edit_config(target="running", config=payload)


async def handle_json(request: web.Request) -> web.Response:
    state = request.app["state"]
    try:
        envelope = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    await _append_event(envelope)

    payload_to_fire = ""
    async with state["lock"]:
        state["envelope_count"] += 1
        if state["envelope_count"] >= TRIGGER_AFTER and state["edit_payload_file"]:
            # Consume the armed payload; /configure re-arms by setting it again.
            payload_to_fire = state["edit_payload_file"]
            state["edit_payload_file"] = ""

    if payload_to_fire:
        try:
            await asyncio.to_thread(_send_netconf_edit, payload_to_fire)
            await _append_event(
                {
                    "event": "netconf_edit_sent",
                    "payload_file": payload_to_fire,
                    "ts": time.time(),
                }
            )
            logging.info("NETCONF edit sent from %s", payload_to_fire)
        except Exception as exc:  # noqa: BLE001
            await _append_event(
                {
                    "event": "netconf_edit_failed",
                    "payload_file": payload_to_fire,
                    "error": str(exc),
                    "ts": time.time(),
                }
            )
            logging.exception("NETCONF edit failed")

    return web.json_response({"ok": True})


async def handle_configure(request: web.Request) -> web.Response:
    """Re-arm the trigger with a new payload; optionally truncate the event log."""
    state = request.app["state"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    payload_file = body.get("payload_file", "")
    clear_log = bool(body.get("clear_log", False))

    async with state["lock"]:
        state["edit_payload_file"] = payload_file
        state["envelope_count"] = 0
        if clear_log:
            async with EVENT_LOG_LOCK:
                await asyncio.to_thread(EVENT_LOG_PATH.unlink, True)

    logging.info("Reconfigured: payload=%s clear_log=%s", payload_file or "<none>", clear_log)
    return web.json_response({"ok": True, "payload_file": payload_file, "cleared": clear_log})


def main():
    logging.basicConfig(level=os.getenv("MOCK_SMO_LOGLEVEL", "INFO"))

    EVENT_LOG_PATH.unlink(missing_ok=True)

    app = web.Application()
    app["state"] = {
        "envelope_count": 0,
        "edit_payload_file": os.getenv("MOCK_SMO_EDIT_PAYLOAD_FILE", ""),
        "lock": asyncio.Lock(),
    }
    app.router.add_post("/json", handle_json)
    app.router.add_post("/configure", handle_configure)
    logging.info(
        "Mock SMO listening on :%d (trigger_after=%d, payload=%s, netconf=%s:%d)",
        PORT,
        TRIGGER_AFTER,
        app["state"]["edit_payload_file"] or "<none>",
        NETCONF_HOST,
        NETCONF_PORT,
    )
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
