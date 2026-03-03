#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Simple mock gNB WebSocket server used by the O1 adapter harness.

The server accepts commands published by the adapter and responds with basic
acknowledgements so the adapter can progress through runtime updates and
restart workflows during testing.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

import websockets

EVENT_LOG_PATH = (
    Path(os.getenv("MOCK_GNB_EVENT_LOG", "")).expanduser()
    if os.getenv("MOCK_GNB_EVENT_LOG")
    else None
)
EVENT_LOG_LOCK = asyncio.Lock()


async def _record_event(event: Dict[str, Any]):
    """Persist the incoming event to the shared log file if configured."""
    if not EVENT_LOG_PATH:
        return

    def _append_line(line: str):
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    serialized = json.dumps(event)
    async with EVENT_LOG_LOCK:
        await asyncio.to_thread(_append_line, serialized)


def _build_response(request: Dict[str, Any]) -> Dict[str, Any]:
    """Create a generic acknowledgement payload for the incoming command."""
    cmd = request.get("cmd", "unknown")
    response: Dict[str, Any] = {"ok": True, "cmd": cmd}

    if cmd == "metrics_subscribe":
        response["subscription"] = "metrics"
    elif cmd == "quit":
        response["status"] = "shutdown-requested"
    elif cmd == "ssb_set":
        response["status"] = "ssb-updated"
    elif cmd == "rrm_policy_ratio_set":
        response["status"] = "rrm-policy-updated"
    else:
        response["status"] = "ack"
    return response


async def client_handler(websocket: websockets.WebSocketServerProtocol):
    """Handle a single client connection."""
    peer = websocket.remote_address
    logging.info("Client connected: %s", peer)

    try:
        async for message in websocket:
            logging.info("RX from %s: %s", peer, message)
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                logging.warning("Dropping non-JSON WS message: %s", message)
                continue

            await _record_event(payload)

            response = _build_response(payload)
            await websocket.send(json.dumps(response))
            logging.info("TX to %s: %s", peer, response)

            if payload.get("cmd") == "quit":
                logging.info("Quit command received, closing connection to %s", peer)
                await websocket.close()
                break

    except websockets.ConnectionClosed:
        logging.info("Client disconnected: %s", peer)


async def main():
    host = os.getenv("MOCK_GNB_HOST", "0.0.0.0")
    port = int(os.getenv("MOCK_GNB_PORT", "8001"))

    logging.basicConfig(level=os.getenv("MOCK_GNB_LOGLEVEL", "INFO"))

    if EVENT_LOG_PATH and EVENT_LOG_PATH.exists():
        EVENT_LOG_PATH.unlink()

    stop_event = asyncio.Event()

    async def handler_with_shutdown(websocket: websockets.WebSocketServerProtocol):
        await client_handler(websocket)
        if websocket.close_code is not None:
            # If the client requested quit, client_handler closed the socket.
            # Signal shutdown after servicing the quit command.
            stop_event.set()

    async with websockets.serve(handler_with_shutdown, host, port):
        logging.info("Mock gNB listening on %s:%d", host, port)
        await stop_event.wait()


if __name__ == "__main__":
    asyncio.run(main())
