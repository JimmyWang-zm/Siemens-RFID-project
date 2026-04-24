"""
rfid_opcua_logger.py
Siemens SIMATIC RF695R — OPC UA based RFID session logger

Connects to the reader via OPC UA, monitors the Presence variable to detect
cart arrivals and departures, and automatically starts/stops scanning.
Collected tags are written to a daily CSV file at the end of each session.

Device: Siemens SIMATIC RF695R + RF662A antenna
OPC UA reference: AH_OPCUA-Ident (sections 3.1.2, 3.3.3)

WBM prerequisites (Settings > Communication > OPC UA):
  - Mode: Main application (Parallel unchecked)
  - Presence events: enabled
  - Security: Allow anonymous access (or set OPCUA_USER / OPCUA_PASS below)

Usage:  python rfid_opcua_logger.py
Stop:   Ctrl+C
"""

import asyncio
import csv
import os
import time
from datetime import datetime

try:
    from asyncua import Client, ua
except ImportError:
    raise SystemExit("Missing dependency. Run: pip install asyncua")

# ── Configuration ─────────────────────────────────────────
OPCUA_URL     = "opc.tcp://192.168.0.254:4840"
OPCUA_USER    = ""        # leave empty for anonymous access
OPCUA_PASS    = ""
OUTPUT_DIR    = r"C:\rfid_logger\records"
READ_POINT    = 1         # read point index (1-based, RF695R supports up to 4)
POLL_INTERVAL = 0.5       # seconds between Presence polls
RETRY_DELAY   = 5         # seconds before reconnect attempt
# ──────────────────────────────────────────────────────────


def _daily_csv() -> str:
    return os.path.join(OUTPUT_DIR, f"RFID_{datetime.now().strftime('%Y-%m-%d')}.csv")


def _ensure_header(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(["Timestamp", "EPC/Tag ID", "Antenna", "RSSI (dBm)", "Session ID"])


def _flush_session(tags: dict, sid: str, t0: float):
    if not tags:
        print("[INFO] No tags in session, skipping save")
        return
    path = _daily_csv()
    _ensure_header(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for epc, rows in tags.items():
            ts, ant, rssi = rows[-1]
            w.writerow([ts, epc, ant, rssi, sid])
    dur = time.time() - t0
    print(f"\n{'=' * 56}")
    print(f"  Session done : {sid}  ({dur:.1f}s)")
    print(f"  Tags found   : {len(tags)}")
    for epc, rows in tags.items():
        _, ant, rssi = rows[-1]
        print(f"    {epc}  Ant:{ant}  RSSI:{rssi}")
    print(f"  Saved to     : {path}")
    print(f"{'=' * 56}\n")


class _Session:
    def __init__(self):
        self._no   = 0
        self.active = False
        self.tags: dict = {}
        self.sid:  str  = ""
        self.t0:   float = 0.0

    def start(self):
        self._no   += 1
        self.active = True
        self.tags   = {}
        self.t0     = time.time()
        self.sid    = f"S{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self._no:03d}"
        print(f"\n[DETECT] Cart arrived  →  session {self.sid}")

    def stop(self):
        if self.active:
            _flush_session(self.tags, self.sid, self.t0)
        self.active = False
        self.tags   = {}

    def add_tag(self, epc: str, ant: str, rssi: str):
        if not self.active:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if epc not in self.tags:
            self.tags[epc] = []
            print(f"  [NEW]  {epc}  Ant:{ant}  RSSI:{rssi}")
        else:
            print(f"  [TAG]  {epc}  Ant:{ant}  RSSI:{rssi}", end="\r")
        self.tags[epc].append((ts, ant, rssi))


def _extract_epc(result) -> str:
    sd = getattr(result, "ScanData", None)
    if sd is None:
        return "N/A"
    if hasattr(sd, "String") and sd.String:
        return str(sd.String)
    if hasattr(sd, "ByteString") and sd.ByteString:
        return sd.ByteString.hex().upper()
    if hasattr(sd, "Epc") and sd.Epc:
        uid = getattr(sd.Epc, "UId", b"")
        return uid.hex().upper() if uid else str(sd.Epc)
    return str(sd)


class _ScanHandler:
    def __init__(self, session: _Session):
        self._s = session

    def event_notification(self, event):
        try:
            results = getattr(event, "Results", None)
            if not results:
                return
            for r in results:
                self._s.add_tag(
                    _extract_epc(r),
                    str(getattr(r, "Antenna",  "N/A")),
                    str(getattr(r, "Strength", "N/A")),
                )
        except Exception as exc:
            print(f"[WARN] Event handler error: {exc}")

    def datachange_notification(self, node, val, data):
        pass


async def _find_nodes(client: Client, rp_index: int):
    root    = client.get_root_node()
    objects = await root.get_child(["0:Objects"])

    device_set = None
    for ns in range(2, 8):
        try:
            device_set = await objects.get_child([f"{ns}:DeviceSet"])
            break
        except Exception:
            continue
    if device_set is None:
        raise RuntimeError("DeviceSet node not found. Check that OPC UA is enabled in WBM.")

    children   = await device_set.get_children()
    readpoints = [
        c for c in children
        if "read_point" in (await c.read_browse_name()).Name.lower()
        or "readpoint"  in (await c.read_browse_name()).Name.lower()
    ]
    if not readpoints:
        raise RuntimeError("No read points found under DeviceSet")
    if rp_index > len(readpoints):
        raise RuntimeError(f"Read point {rp_index} not found (only {len(readpoints)} available)")

    rp = readpoints[rp_index - 1]
    print(f"[INFO] Read point : {(await rp.read_browse_name()).Name}")

    presence_node    = None
    scan_active_node = None
    scan_start_node  = None
    scan_stop_node   = None

    for c in await rp.get_children():
        bn = (await c.read_browse_name()).Name
        if bn == "Diagnostics":
            for dc in await c.get_children():
                if (await dc.read_browse_name()).Name == "Presence":
                    presence_node = dc
                    print("       Presence   ✓")
        elif bn == "ScanActive":
            scan_active_node = c
            print("       ScanActive ✓")
        elif bn == "ScanStart":
            scan_start_node = c
            print("       ScanStart  ✓")
        elif bn == "ScanStop":
            scan_stop_node = c
            print("       ScanStop   ✓")

    if presence_node is None:
        print("[WARN] Presence node not found. Enable 'Presence events' in WBM > OPC UA.")

    return rp, presence_node, scan_active_node, scan_start_node, scan_stop_node


async def _start_scanning(rp, scan_start_node, scan_active_node):
    if scan_start_node is not None:
        try:
            scan_settings_cls = getattr(ua, "ScanSettings", None)
            if scan_settings_cls is not None:
                ss = scan_settings_cls()
                ss.Cycles        = 0
                ss.DataAvailable = False
                ss.Duration      = 0
                await rp.call_method(scan_start_node, ss)
            else:
                await rp.call_method(
                    scan_start_node,
                    ua.Variant(0,     ua.VariantType.UInt32),
                    ua.Variant(False, ua.VariantType.Boolean),
                    ua.Variant(0,     ua.VariantType.UInt32),
                )
            print("[CMD] ScanStart")
            return
        except Exception as e:
            print(f"[WARN] ScanStart failed: {e} — falling back to ScanActive")

    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(True, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = True")
        except Exception as e:
            print(f"[ERR] ScanActive write failed: {e}")


async def _stop_scanning(rp, scan_stop_node, scan_active_node):
    if scan_stop_node is not None:
        try:
            await rp.call_method(scan_stop_node)
            print("[CMD] ScanStop")
            return
        except Exception as e:
            print(f"[WARN] ScanStop failed: {e} — falling back to ScanActive")

    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(False, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = False")
        except Exception as e:
            print(f"[ERR] ScanActive write failed: {e}")


async def _run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = _Session()

    print("=" * 56)
    print("  Siemens RF695R — OPC UA RFID Logger")
    print(f"  Server    : {OPCUA_URL}")
    print(f"  Read point: {READ_POINT}")
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

                rp, presence_node, scan_active_node, scan_start_node, scan_stop_node = (
                    await _find_nodes(client, READ_POINT)
                )

                print("[INIT] Resetting scan state...")
                await _stop_scanning(rp, scan_stop_node, scan_active_node)
                await asyncio.sleep(0.5)

                handler = _ScanHandler(session)
                sub = await client.create_subscription(200, handler)
                await sub.subscribe_events(rp)
                print("[SUB] Subscribed to scan events")

                print("\n[WAIT] Waiting for cart...\n")
                try:
                    prev_presence = int(await presence_node.read_value()) if presence_node else 0
                except Exception:
                    prev_presence = 0

                while True:
                    if presence_node is None:
                        await asyncio.sleep(10)
                        print("[ERR] Presence node unavailable. Enable 'Presence events' in WBM.")
                        continue

                    try:
                        pval = int(await presence_node.read_value())
                    except Exception as e:
                        print(f"[WARN] Presence read failed: {e}")
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    if pval > 0 and prev_presence == 0:
                        session.start()
                        await _start_scanning(rp, scan_start_node, scan_active_node)

                    elif pval == 0 and prev_presence > 0:
                        print("\n[DETECT] Cart left — stopping scan...")
                        await _stop_scanning(rp, scan_stop_node, scan_active_node)
                        session.stop()
                        print("[WAIT] Waiting for next cart...\n")

                    prev_presence = pval
                    await asyncio.sleep(POLL_INTERVAL)

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
