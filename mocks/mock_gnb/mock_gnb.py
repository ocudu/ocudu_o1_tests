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
import contextlib
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
_EMIT_PERIOD_S = float(os.getenv("MOCK_GNB_EMIT_PERIOD_S", "1.0"))


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


_DUMMY_METRIC_FRAMES_JSONL = """\
{"rlc_metrics": {"drb_id": 2, "du_id": 0, "rx": {"num_lost_pdus": 0, "num_malformed_pdus": 0, "num_pdu_bytes": 932, "num_pdus": 30, "num_sdu_bytes": 842, "num_sdus": 5}, "tx": {"max_pdu_latency_ns": 32676, "num_discard_failures": 0, "num_discarded_sdus": 0, "num_dropped_sdus": 0, "num_sdu_bytes": 1337391, "num_sdus": 891, "pull_latency_histogram": [{"pull_latency_bin_count": 78, "pull_latency_bin_start_usec": 0}, {"pull_latency_bin_count": 172, "pull_latency_bin_start_usec": 1}, {"pull_latency_bin_count": 171, "pull_latency_bin_start_usec": 2}, {"pull_latency_bin_count": 124, "pull_latency_bin_start_usec": 3}, {"pull_latency_bin_count": 94, "pull_latency_bin_start_usec": 4}, {"pull_latency_bin_count": 71, "pull_latency_bin_start_usec": 5}, {"pull_latency_bin_count": 55, "pull_latency_bin_start_usec": 6}, {"pull_latency_bin_count": 130, "pull_latency_bin_start_usec": 7}], "sum_pdu_latency_ns": 3700227, "sum_sdu_latency_us": 2538037}, "ue_id": 0}, "timestamp": "2026-05-15T14:23:28.129"}
{"app_resource_usage": {"cpu_usage_percent": 208.73291492462158, "memory_usage_mb": 2669.375, "power_consumption_watts": 23.964622964622965}, "timestamp": "2026-05-15T14:23:28.331"}
{"buffer_pool": {"central_cache_size": 1047200}, "timestamp": "2026-05-15T14:23:28.331"}
{"du": {"du_high": {"mac": {"dl": [{"average_latency_us": 70.715, "cpu_usage_percent": 0.0070715, "max_latency_us": 351.928, "min_latency_us": 6.554, "pci": 1}]}}}, "timestamp": "2026-05-15T14:23:28.476"}
{"cells": [{"cell_metrics": {"average_latency": 29, "avg_prach_delay": 0.0, "error_indication_count": 0, "late_dl_harqs": 0, "late_ul_harqs": 0, "latency_histogram": [1662, 317, 21, 0, 0, 0, 0, 0, 0, 0], "max_latency": 143, "msg3_nof_nok": 0, "msg3_nof_ok": 0, "nof_failed_pdcch_allocs": 0, "nof_failed_uci_allocs": 28, "pci": 1, "pdsch_prbs_used_per_tdd_slot_idx": [12, 4, 3, 15, 16, 7, 6, 0, 0, 0], "pucch_tot_rb_usage_avg": 0.699999988079071, "pusch_prbs_used_per_tdd_slot_idx": [0, 0, 0, 0, 0, 0, 0, 1, 0, 0]}, "ue_list": [{"avg_ce_delay": 4.059999942779541, "avg_crc_delay": 4.019999980926514, "avg_pucch_harq_delay": 3.884012460708618, "avg_pusch_harq_delay": 4.0, "avg_sr_to_pusch_delay": 10.0, "bsr": 0, "cqi": 14, "dl_brate": 11312392.0, "dl_bs": 3008, "dl_mcs": 27, "dl_nof_nok": 0, "dl_nof_ok": 627, "dl_ri": 1.0, "last_phr": -4, "max_ce_delay": 4.5, "max_crc_delay": 4.5, "max_pdsch_distance": 10, "max_pucch_harq_delay": 4.0, "max_pusch_distance": 40, "max_pusch_harq_delay": 4.0, "max_sr_to_pusch_delay": 10.0, "nof_pucch_f0f1_invalid_harqs": 0, "nof_pucch_f2f3f4_invalid_csis": 25, "nof_pucch_f2f3f4_invalid_harqs": 0, "nof_pusch_invalid_csis": 0, "nof_pusch_invalid_harqs": 0, "pci": 1, "pucch_snr_db": 9.14433479309082, "pucch_ta_ns": 284.74056534832926, "pusch_rsrp_db": -21.668001174926758, "pusch_snr_db": 27.128480911254883, "pusch_ta_ns": 357.9305314360681, "rnti": 17921, "srs_ta_ns": 0.0, "ta_ns": 297.0207333419239, "ue": 0, "ul_brate": 169600.0, "ul_mcs": 27, "ul_nof_nok": 0, "ul_nof_ok": 25, "ul_ri": 1.0}]}], "timestamp": "2026-05-15T14:23:28.476"}
{"du_low": {"dl": {"average_latency_us": 71.73273474178404, "cpu_usage_percent": 5.997806675795515, "fec": {"average_throughput_mbps": 1229.761704918625, "cpu_usage_percent": 1.520248839472634}, "ldpc_encoder": {"average_latency_us": 5.982647022713321, "average_throughput_mbps": 1229.761704918625, "avg_cb_size_bits": 7357.230202578269, "cpu_usage_percent": 0.9748061786767037, "max_latency_us": 40.452, "min_latency_us": 2.811}, "ldpc_rate_matcher": {"average_latency_us": 2.0883057090239414, "average_throughput_mbps": 3613.8454076458397, "cpu_usage_percent": 0.3402663236513527, "max_latency_us": 15.277, "min_latency_us": 0.684}, "max_latency_slot": "104.13", "max_latency_us": 266.232, "modulation_mapper": {"cpu_usage_percent": 0.20065125565010036, "qam16_mod_throughput_mbps": 0.0, "qam256_mod_throughput_mbps": 6151.526718704446, "qam64_mod_throughput_mbps": 0.0, "qpsk_mod_throughput_mbps": 1411.908895924829}, "precoding_layer_mapping": {"average_latency_us": 79.53614173228347, "cpu_usage_percent": 0.5051752368816147, "throughput_per_nof_layers_mresps": [305.294577119895, 0.0, 0.0, 0.0]}, "scrambling": {"cpu_usage_percent": 0.34417125693040634}}, "ul": {"algo_efficiency": {"bler": 0.0, "evm": 0.0, "sinr_db": 26.820281982421875}, "average_latency_us": 345.06736, "average_throughput_mbps": 19.659929585922004, "channel_estimation": {"average_latency_us": 11.132040000000002, "average_throughput_mbps": 0.6288155630055228, "cpu_usage_percent": 0.027836752983963167, "max_latency_us": 19.526, "min_latency_us": 4.085}, "cpu_usage_percent": 0.3380639972953536, "demodulation_mapper": {"cpu_usage_percent": 0.06178116569860197, "qam16_mod_throughput_mbps": 0.0, "qam256_mod_throughput_mbps": 299.1917935965185, "qam64_mod_throughput_mbps": 0.0, "qpsk_mod_throughput_mbps": 0.0}, "descrambling": {"cpu_usage_percent": 0.007947799524086257}, "fec": {"average_throughput_mbps": 71.2859416832393, "cpu_usage_percent": 0.28745800246258857}, "ldpc_decoder": {"average_cb_size_bits": 7040.0, "average_latency_us": 98.7572, "average_throughput_mbps": 71.2859416832393, "cpu_usage_percent": 0.24695202153314638, "max_latency_us": 140.483, "min_latency_us": 67.977}, "ldpc_rate_dematcher": {"average_latency_us": 15.19128, "average_throughput_mbps": 484.48846970103904, "cpu_usage_percent": 0.037987278959671356, "max_latency_us": 40.628, "min_latency_us": 5.166}, "max_latency_slot": "33.7", "max_latency_us": 489.008, "transform_precoder": {"average_latency_us": 0.0, "average_throughput_mreps": 0.0, "cpu_usage_percent": 0.0}}}, "timestamp": "2026-05-15T14:23:28.476"}
{"cu-up": {"pdcp": {"dl": {"average_latency_us": 9.498644219977553, "average_throughput_mbps": 10.699127999999998, "cpu_usage_percent": 0.42023730000000004, "max_latency_us": 107.132, "min_latency_us": 2.442}, "ul": {"average_latency_us": 23.0394, "average_throughput_mbps": 0.006736, "cpu_usage_percent": 0.0141259, "max_latency_us": 26.627}}}, "timestamp": "2026-05-15T14:23:28.972"}
"""

_DUMMY_METRIC_FRAMES: list[str] = [
    line for line in _DUMMY_METRIC_FRAMES_JSONL.splitlines() if line.strip()
]


async def _metric_emitter(websocket: websockets.WebSocketServerProtocol):
    """Stream the dummy frame snapshot on a fixed cadence until the connection closes."""
    while True:
        for frame in _DUMMY_METRIC_FRAMES:
            try:
                await websocket.send(frame)
            except websockets.ConnectionClosed:
                return
        try:
            await asyncio.sleep(_EMIT_PERIOD_S)
        except asyncio.CancelledError:
            return


async def client_handler(websocket: websockets.WebSocketServerProtocol):
    """Handle a single client connection."""
    peer = websocket.remote_address
    logging.info("Client connected: %s", peer)

    emitter_task: asyncio.Task | None = None

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

            if payload.get("cmd") == "metrics_subscribe" and (emitter_task is None or emitter_task.done()):
                emitter_task = asyncio.create_task(_metric_emitter(websocket))
                logging.info("Started metric emitter for %s", peer)

            if payload.get("cmd") == "quit":
                logging.info("Quit command received, closing connection to %s", peer)
                await websocket.close()
                break

    except websockets.ConnectionClosed:
        logging.info("Client disconnected: %s", peer)
    finally:
        if emitter_task is not None:
            emitter_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await emitter_task


async def main():
    host = os.getenv("MOCK_GNB_HOST", "0.0.0.0")
    port = int(os.getenv("MOCK_GNB_PORT", "8001"))

    logging.basicConfig(level=os.getenv("MOCK_GNB_LOGLEVEL", "INFO"))

    # NB: do NOT unlink EVENT_LOG_PATH here. mock_gnb shuts down on `quit` and is
    # restarted by docker; clearing the log here would wipe the `quit` entry that
    # tests assert on. Tests own the log lifecycle and unlink it themselves.

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
