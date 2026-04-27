"""
rfid_opcua.main
───────────────
Main async loop: connects to the OPC UA server, sets up subscriptions,
and runs the appropriate trigger mode (DI or Presence).

Usage:  python -m rfid_opcua          (or via rfid_opcua_logger.py)
Stop:   Ctrl+C
"""

from __future__ import annotations

import asyncio
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
    POLL_INTERVAL,
    PREFER_EVENTS,
    READ_POINT,
    RETRY_DELAY,
)
from .handlers import DIHandler
from .opcua_helpers import (
    browse_tree,
    find_nodes,
    poll_last_scan,
    setup_scan_event_subscription,
    start_scanning,
    stop_scanning,
)
from .session import Session


# ═══════════════════════════════════════════════════════════════════════════════
#  Edge callbacks (scheduled from subscription handlers)
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_di_edge(
    di_val: bool, session: Session, rp,
    scan_start_node, scan_active_node, scan_stop_node,
    use_events: bool,
):
    """Called from the DI data-change callback when an edge is detected."""
    if di_val:
        session.start(trigger="DI")
        await start_scanning(rp, scan_start_node, scan_active_node, use_events)
    else:
        print("\n[SENSOR] Beam blocked — stopping scan...")
        await stop_scanning(rp, scan_stop_node, scan_active_node, use_events)
        session.stop()
        print(f"[WAIT] Waiting for sensor on DI{DI_CHANNEL}...\n")


# ═══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ═══════════════════════════════════════════════════════════════════════════════

async def _run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = Session()

    print("=" * 56)
    print("  Siemens RF695R — OPC UA RFID Logger")
    print(f"  Server    : {OPCUA_URL}")
    print(f"  Read point: {READ_POINT}")
    print(f"  Trigger   : DI{DI_CHANNEL} (IO-Link / photoelectric sensor)")
    print(f"  Output    : {OUTPUT_DIR}")
    print("  Ctrl+C to stop")
    print("=" * 56)

    while True:
        client = Client(OPCUA_URL)
        if OPCUA_USER:
            client.set_user(OPCUA_USER)
            client.set_password(OPCUA_PASS)

        try:
            print(f"\n[CONN] Connecting to {OPCUA_URL} ...")
            async with client:
                try:
                    await client.load_data_type_definitions()
                    print("[INFO] Custom data types loaded")
                except Exception as e:
                    print(f"[WARN] Could not load data types: {e}")

                if DEBUG_BROWSE:
                    await browse_tree(client)

                (rp, scan_active_node,
                 scan_start_node, scan_stop_node,
                 di_node, scan_nodes) = await find_nodes(client, READ_POINT)

                print("[INIT] Resetting scan state...")
                await stop_scanning(rp, scan_stop_node, scan_active_node, use_events=False)
                await asyncio.sleep(0.5)

                # ── Tag read strategy ─────────────────────────────────────
                scan_sub, evt_handler = None, None
                if PREFER_EVENTS:
                    scan_sub, evt_handler = await setup_scan_event_subscription(
                        client, rp, session, EVENT_PUBLISH_INTERVAL
                    )
                use_events = scan_sub is not None
                SCAN_POLL = 0.05
                loop_interval = POLL_INTERVAL if use_events else SCAN_POLL

                # ── DI / IO-Link photoelectric sensor trigger ─────────────
                if di_node is None:
                    print(f"[ERR] DI{DI_CHANNEL} node not found — cannot start DI trigger.")
                    print("      Set DEBUG_BROWSE=True and restart to inspect the OPC UA tree.")
                    await asyncio.sleep(RETRY_DELAY)
                    continue

                try:
                    raw     = await di_node.read_value()
                    prev_di = bool((int(raw) >> DI_CHANNEL) & 1)
                except Exception:
                    prev_di = False

                print(f"\n[WAIT] Waiting for sensor on DI{DI_CHANNEL}...\n")
                prev_scan_ts = None

                # DI data-change subscription
                di_sub = None
                di_handler = DIHandler(
                    initial_val=prev_di,
                    on_edge=lambda di_val: _handle_di_edge(
                        di_val, session, rp,
                        scan_start_node, scan_active_node,
                        scan_stop_node, use_events,
                    ),
                )
                try:
                    di_sub = await client.create_subscription(DI_SAMPLE_MS, di_handler)
                    await di_sub.subscribe_data_change(di_node)
                    print(f"[SUB]  Subscribed to DI{DI_CHANNEL} data changes  (sample: {DI_SAMPLE_MS} ms)")
                except Exception as e:
                    print(f"[WARN] DI subscription failed: {e} — falling back to polling")
                    di_sub = None

                di_err_count  = 0
                last_di_check = 0.0

                while True:
                    if di_sub is not None:
                        if not use_events and session.active:
                            prev_scan_ts = await poll_last_scan(session, scan_nodes, prev_scan_ts)
                        await asyncio.sleep(loop_interval)
                        continue

                    # Fallback: manual DI polling
                    now = time.monotonic()
                    if now - last_di_check >= POLL_INTERVAL:
                        last_di_check = now
                        try:
                            raw    = await di_node.read_value()
                            di_val = bool((int(raw) >> DI_CHANNEL) & 1)
                            di_err_count = 0
                        except Exception as e:
                            di_err_count += 1
                            if di_err_count <= 3:
                                print(f"[WARN] DI{DI_CHANNEL} read failed: {e}")
                            if di_err_count >= 5:
                                raise RuntimeError(f"DI read failed {di_err_count}x — reconnecting")
                            await asyncio.sleep(POLL_INTERVAL)
                            continue

                        if di_val and not prev_di:
                            session.start(trigger="DI")
                            await start_scanning(rp, scan_start_node, scan_active_node, use_events)
                            prev_scan_ts = None
                        elif not di_val and prev_di:
                            print("\n[SENSOR] Beam blocked — stopping scan...")
                            await stop_scanning(rp, scan_stop_node, scan_active_node, use_events)
                            session.stop()
                            print(f"[WAIT] Waiting for sensor on DI{DI_CHANNEL}...\n")
                        prev_di = di_val

                    if not use_events and session.active:
                        prev_scan_ts = await poll_last_scan(session, scan_nodes, prev_scan_ts)
                    await asyncio.sleep(loop_interval)

        except KeyboardInterrupt:
            print("\n[STOP] Shutting down...")
            if session.active:
                session.stop()
            print(f"[STOP] Records saved to: {OUTPUT_DIR}")
            return

        except Exception as e:
            if session.active:
                session.stop()
            print(f"[ERR] {e}")
            print(f"[RETRY] Reconnecting in {RETRY_DELAY}s...\n")
            await asyncio.sleep(RETRY_DELAY)


def main():
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
