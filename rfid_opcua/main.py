"""
rfid_opcua.main
───────────────
Main async loop: connects to the OPC UA server, sets up subscriptions,
and runs the DI trigger mode.

Usage:  python -m rfid_opcua          (or via rfid_opcua_logger.py)
Stop:   Ctrl+C
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

try:
    from asyncua import Client, ua
except ImportError:
    raise SystemExit("Missing dependency.  Run:  pip install asyncua")

from .config import (
    DEBUG_BROWSE,
    DI_CHANNEL,
    DI_SAMPLE_MS,
    EVENT_PUBLISH_INTERVAL,
    OPCUA_PASS,
    OPCUA_URL,
    OPCUA_USER,
    OUTPUT_DIR,
    READ_POINT,
    RETRY_DELAY,
    WATCHDOG_TIMEOUT,
)
from .handlers import DIHandler
from .opcua_helpers import (
    browse_tree,
    find_nodes,
    setup_scan_event_subscription,
    start_scanning,
    stop_scanning,
)
from .session import Session, ensure_csv

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Edge callbacks (scheduled from subscription handlers)
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_di_edge(
    di_val: bool, session: Session, rp,
    scan_start_node, scan_active_node, scan_stop_node,
    evt_handler,
):
    """Called from the DI data-change callback when an edge is detected."""
    if di_val:
        session.start(trigger="DI")
        if evt_handler is not None:
            evt_handler.reset_watchdog()
        await start_scanning(rp, scan_start_node, scan_active_node)
    else:
        await stop_scanning(rp, scan_stop_node, scan_active_node)
        session.stop()


# ═══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ═══════════════════════════════════════════════════════════════════════════════

async def _run():
    from . import setup_logging
    setup_logging()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = Session()

    log.info("Siemens RF695R — OPC UA RFID Logger")
    log.info("  Server : %s", OPCUA_URL)
    csv_path = ensure_csv()
    log.info("  CSV    : %s", os.path.relpath(csv_path))
    log.info("  Ctrl+C to stop")

    while True:
        client = Client(OPCUA_URL)
        if OPCUA_USER:
            client.set_user(OPCUA_USER)
            client.set_password(OPCUA_PASS)

        # Track resources so _cleanup can tear them down
        scan_sub   = None
        di_sub     = None
        rp         = None
        scan_stop_node   = None
        scan_active_node = None

        async def _cleanup(session: Session, reason: str = ""):
            """
            Graceful resource teardown — called before the OPC UA client
            disconnects.  Safe to call multiple times.
            """
            nonlocal scan_sub, di_sub
            tag = f"Cleanup ({reason}): " if reason else "Cleanup: "

            # 1. Stop scanning on the reader
            if rp is not None and scan_stop_node is not None:
                try:
                    await stop_scanning(rp, scan_stop_node, scan_active_node)
                except Exception as e:
                    log.debug("%sScanStop failed: %s", tag, e)

            # 2. Delete event subscription
            if scan_sub is not None:
                try:
                    await scan_sub.delete()
                    log.debug("%sScan subscription deleted", tag)
                except Exception as e:
                    log.debug("%sScan sub delete failed: %s", tag, e)
                scan_sub = None

            # 3. Delete DI subscription
            if di_sub is not None:
                try:
                    await di_sub.delete()
                    log.debug("%sDI subscription deleted", tag)
                except Exception as e:
                    log.debug("%sDI sub delete failed: %s", tag, e)
                di_sub = None

            # 4. Flush session
            if session.active:
                session.stop()

        try:
            log.info("Connected to %s", OPCUA_URL)
            async with client:
                try:
                    await client.load_data_type_definitions()
                    log.debug("Custom data types loaded")
                except Exception as e:
                    log.debug("Could not load data types: %s", e)

                if DEBUG_BROWSE:
                    await browse_tree(client)

                (rp, scan_active_node,
                 scan_start_node, scan_stop_node,
                 di_node, scan_nodes) = await find_nodes(client, READ_POINT)

                log.debug("Resetting scan state...")
                await stop_scanning(rp, scan_stop_node, scan_active_node)
                await asyncio.sleep(0.5)

                # ── Event subscription for tag reads ──────────────────────
                scan_sub, evt_handler = await setup_scan_event_subscription(
                    client, rp, session, EVENT_PUBLISH_INTERVAL,
                    scan_nodes=scan_nodes,
                )

                # ── DI / IO-Link photoelectric sensor trigger ─────────────
                if di_node is None:
                    raise RuntimeError(
                        f"DI{DI_CHANNEL} node not found \u2014 cannot start DI trigger. "
                        "Set DEBUG_BROWSE=True and restart to inspect the OPC UA tree."
                    )

                try:
                    raw     = await di_node.read_value()
                    prev_di = bool((int(raw) >> DI_CHANNEL) & 1)
                except Exception:
                    prev_di = False

                log.info("Ready — waiting for sensor on DI%d ...", DI_CHANNEL)

                di_handler = DIHandler(
                    initial_val=prev_di,
                    on_edge=lambda di_val: _handle_di_edge(
                        di_val, session, rp,
                        scan_start_node, scan_active_node,
                        scan_stop_node, evt_handler,
                    ),
                )
                di_sub = await client.create_subscription(DI_SAMPLE_MS, di_handler)
                await di_sub.subscribe_data_change(di_node)
                log.debug("Subscribed to DI%d data changes  (sample: %d ms)", DI_CHANNEL, DI_SAMPLE_MS)

                # ── Idle loop: watchdog + keepalive ───────────────────────
                try:
                    while True:
                        if (WATCHDOG_TIMEOUT > 0
                                and evt_handler is not None
                                and session.active
                                and evt_handler.watchdog_armed):
                            silence = time.monotonic() - evt_handler.last_event_time
                            if silence > WATCHDOG_TIMEOUT:
                                raise RuntimeError(
                                    f"Watchdog: no scan events for {silence:.0f}s "
                                    f"\u2014 subscription may be dead"
                                )

                        await asyncio.sleep(EVENT_PUBLISH_INTERVAL / 1000)

                except KeyboardInterrupt:
                    log.info("Shutting down...")
                    await _cleanup(session, "user interrupt")
                    log.info("Records saved to: %s", OUTPUT_DIR)
                    return

                except Exception as e:
                    log.error("%s", e)
                    await _cleanup(session, "error recovery")
                    # Fall through to outer retry logic

        except KeyboardInterrupt:
            log.info("Shutting down...")
            if session.active:
                session.stop()
            log.info("Records saved to: %s", OUTPUT_DIR)
            return

        except Exception as e:
            if session.active:
                session.stop()
            log.error("%s", e)

        log.info("Reconnecting in %ds...", RETRY_DELAY)
        await asyncio.sleep(RETRY_DELAY)


def main():
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
