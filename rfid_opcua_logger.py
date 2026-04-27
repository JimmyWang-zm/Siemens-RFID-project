"""
rfid_opcua_logger.py
Siemens SIMATIC RF695R — OPC UA based RFID session logger

Trigger modes
─────────────
  "DI"       Monitors Digital Input / IO-Link channel DI_CHANNEL.
             Sensor detects reflector → DI HIGH → ScanStart (yellow LED on).
             Beam blocked by object  → DI LOW  → ScanStop.
  "Presence" Monitors the built-in Diagnostics/Presence OPC UA variable.
             (Legacy mode; unchanged from previous behaviour.)

Device: Siemens SIMATIC RF695R + RF662A antenna
OPC UA reference: AH_OPCUA-Ident (sections 3.1.2, 3.3.3)

WBM prerequisites
─────────────────
  OPC UA (Settings › Communication › OPC UA):
    - Mode: Main application  (uncheck Parallel)
    - Security: Allow anonymous access  (or fill OPCUA_USER / OPCUA_PASS below)
    - Port: 4840 (default)

  DI/DO  (Settings › DIDO) — required only for TRIGGER_SOURCE = "DI":
    - IO-Link: enabled
    - Input 0: wired to photoelectric sensor signal output
    - Output 0 events (adds two rules in the Events table):
        Rising  edge of Input 0 → Output 0 = On    ← yellow indicator ON
        Falling edge of Input 0 → Output 0 = Off   ← yellow indicator OFF
    - Save and apply

  If DEBUG_BROWSE = True the full OPC UA node tree is printed on startup.
  Use that output to find and verify the DI node path on your firmware version.

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

# ── Configuration ──────────────────────────────────────────────────────────────
OPCUA_URL      = "opc.tcp://192.168.0.254:4840"
OPCUA_USER     = ""          # leave empty for anonymous / WBM anonymous access
OPCUA_PASS     = ""
OUTPUT_DIR     = r"C:\rfid_logger\records"
READ_POINT     = 1           # read-point index (1-based; RF695R supports up to 4)
POLL_INTERVAL  = 0.2         # seconds between trigger polls (DI: 0.1–0.2; Presence: 0.5)
RETRY_DELAY    = 5           # seconds before reconnect attempt

# Trigger source ───────────────────────────────────────────────────────────────
# "DI"       : photoelectric sensor via IO-Link / digital input terminal
# "Presence" : built-in OPC UA Diagnostics/Presence variable (legacy)
TRIGGER_SOURCE = "DI"
DI_CHANNEL     = 0           # IO-Link / DI channel index (0-based)

# Set True once to print the full OPC UA node tree and locate the DI node path.
# Set back to False for normal operation after the node path is confirmed.
DEBUG_BROWSE   = False
# ──────────────────────────────────────────────────────────────────────────────


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
        self._no    = 0
        self.active = False
        self.tags:  dict  = {}
        self.sid:   str   = ""
        self.t0:    float = 0.0

    def start(self, trigger: str = ""):
        self._no   += 1
        self.active = True
        self.tags   = {}
        self.t0     = time.time()
        self.sid    = f"S{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self._no:03d}"
        label = "Reflector detected" if trigger == "DI" else "Cart arrived"
        print(f"\n[DETECT] {label}  →  session {self.sid}")

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


def _epc_to_hex(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.hex().upper()
    s = str(raw)
    return s


async def _poll_last_scan(session: "_Session", scan_nodes: dict, prev_ts):
    """
    Poll LastScan* variables for new tag reads and add them to the session.

    Returns the latest seen timestamp so the caller can detect changes.
    Used instead of OPC UA event subscription, which is unreliable across
    Siemens firmware versions due to custom event filter fields.
    """
    ts_node = scan_nodes.get("timestamp")
    if ts_node is None:
        return prev_ts
    try:
        ts = await ts_node.read_value()
    except Exception:
        return prev_ts

    if ts is None or ts == prev_ts:
        return prev_ts

    epc_raw, ant, rssi_raw = None, "?", None
    try:
        if scan_nodes.get("data"):
            epc_raw = await scan_nodes["data"].read_value()
        if scan_nodes.get("antenna"):
            ant = await scan_nodes["antenna"].read_value()
        if scan_nodes.get("rssi"):
            rssi_raw = await scan_nodes["rssi"].read_value()
    except Exception as e:
        print(f"[WARN] LastScan read error: {e}")
        return ts

    # AutoID standard reports RSSI in cdBm (0.01 dBm).  Convert to dBm.
    rssi = "?"
    if rssi_raw is not None:
        try:
            rssi = f"{int(rssi_raw) / 100:.1f}"
        except Exception:
            rssi = str(rssi_raw)

    epc = _epc_to_hex(epc_raw)
    if epc:
        session.add_tag(epc, str(ant), rssi)
    return ts


async def _browse_tree(client: Client, max_depth: int = 5):
    """Print the full OPC UA node tree to help identify the DI node path."""
    root    = client.get_root_node()
    objects = await root.get_child(["0:Objects"])

    async def _print(node, depth: int):
        if depth > max_depth:
            return
        try:
            children = await node.get_children()
        except Exception:
            return
        for child in children:
            try:
                bn  = await child.read_browse_name()
                nid = child.nodeid
                print("  " * depth + f"[{bn.NamespaceIndex}:{bn.Name}]  {nid}")
                await _print(child, depth + 1)
            except Exception:
                continue

    print("\n" + "=" * 60)
    print("  OPC UA node tree  (DEBUG_BROWSE = True)")
    print("  Search for 'Input', 'DI', 'IOLink', 'ProcessData' nodes")
    print("=" * 60)
    await _print(objects, 0)
    print("=" * 60 + "\n")


async def _find_di_node(client: Client, rp, di_channel: int):
    """
    Locate the DigitalInputs variable node on the RF695R.

    Known RF695R path (from OPC UA tree inspection):
      [read point] > IOData > DigitalIOPorts > DigitalInputs

    DigitalInputs is a bitmask variable: bit N corresponds to DI channel N.
    Bit 0 = channel 0 state, bit 1 = channel 1 state, etc.

    Falls back to a full DFS search if the direct path fails (e.g. different
    firmware version).  Set DEBUG_BROWSE = True to inspect the tree.
    """
    # ── Direct path (RF695R: rp > IOData > DigitalIOPorts > DigitalInputs) ──
    try:
        for c in await rp.get_children():
            if (await c.read_browse_name()).Name == "IOData":
                for c2 in await c.get_children():
                    if (await c2.read_browse_name()).Name == "DigitalIOPorts":
                        for c3 in await c2.get_children():
                            if (await c3.read_browse_name()).Name == "DigitalInputs":
                                print(f"       DI inputs  ✓  [IOData > DigitalIOPorts > DigitalInputs  (bit {di_channel})]")
                                return c3
    except Exception as e:
        print(f"[WARN] Direct IOData path failed: {e}")

    # ── DFS fallback (other firmware layouts) ────────────────────────────────
    root    = client.get_root_node()
    objects = await root.get_child(["0:Objects"])

    device_set = None
    for ns in range(2, 8):
        try:
            device_set = await objects.get_child([f"{ns}:DeviceSet"])
            break
        except Exception:
            continue
    search_root = device_set or objects

    ch = str(di_channel)
    value_names = {
        f"input_{ch}", f"input{ch}",
        f"di_{ch}",    f"di{ch}",
        f"digitalinput_{ch}", f"digitalinput{ch}",
        f"in{ch}", f"channel_{ch}", ch,
    }
    group_names = {
        "digitalio", "digitalinputs", "di", "inputs", "io",
        "iolink", "iolinkmaster",
        "processdata", "processinputdata", "inputdata",
    }

    async def dfs(node, in_group: bool, depth: int):
        if depth > 6:
            return None
        try:
            children = await node.get_children()
        except Exception:
            return None
        for child in children:
            try:
                raw = (await child.read_browse_name()).Name
                bn  = raw.lower().replace(" ", "").replace("-", "")
            except Exception:
                continue
            if in_group and bn in value_names:
                return child
            if bn in group_names:
                result = await dfs(child, in_group=True, depth=depth + 1)
                if result:
                    return result
            else:
                result = await dfs(child, in_group=False, depth=depth + 1)
                if result:
                    return result
        return None

    found = await dfs(search_root, in_group=False, depth=0)
    if found:
        name = (await found.read_browse_name()).Name
        print(f"       DI{di_channel} node  ✓  [{name}]  (DFS fallback)")
    else:
        print(f"[WARN] DI{di_channel} node not found.")
        print("       Set DEBUG_BROWSE=True and restart to inspect the OPC UA tree.")
    return found


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
    scan_nodes       = {"data": None, "antenna": None, "rssi": None, "timestamp": None}

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
        elif bn == "LastScanData":
            scan_nodes["data"] = c
        elif bn == "LastScanAntenna":
            scan_nodes["antenna"] = c
        elif bn == "LastScanRSSI":
            scan_nodes["rssi"] = c
        elif bn == "LastScanTimestamp":
            scan_nodes["timestamp"] = c

    if all(scan_nodes[k] is not None for k in ("data", "antenna", "rssi", "timestamp")):
        print("       LastScan*  ✓  (Data / Antenna / RSSI / Timestamp)")
    else:
        print(f"[WARN] LastScan* nodes incomplete: {[k for k,v in scan_nodes.items() if v is None]}")

    if TRIGGER_SOURCE == "Presence" and presence_node is None:
        print("[WARN] Presence node not found. Enable 'Presence events' in WBM > OPC UA.")

    di_node = None
    if TRIGGER_SOURCE == "DI":
        di_node = await _find_di_node(client, rp, DI_CHANNEL)

    return rp, presence_node, scan_active_node, scan_start_node, scan_stop_node, di_node, scan_nodes


async def _start_scanning(rp, scan_start_node, scan_active_node):
    # ScanActive = True is a direct boolean write — most reliable way to
    # activate continuous scanning and ensure tag events are fired.
    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(True, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = True")
            return
        except Exception as e:
            print(f"[WARN] ScanActive write failed: {e} — trying ScanStart")

    if scan_start_node is not None:
        try:
            scan_settings_cls = getattr(ua, "ScanSettings", None)
            if scan_settings_cls is not None:
                ss = scan_settings_cls()
                ss.Cycles        = 0
                ss.DataAvailable = True   # True = fire events for each tag found
                ss.Duration      = 0
                await rp.call_method(scan_start_node, ss)
            else:
                await rp.call_method(
                    scan_start_node,
                    ua.Variant(0,    ua.VariantType.UInt32),
                    ua.Variant(True, ua.VariantType.Boolean),  # DataAvailable = True
                    ua.Variant(0,    ua.VariantType.UInt32),
                )
            print("[CMD] ScanStart")
        except Exception as e:
            print(f"[ERR] ScanStart failed: {e}")


async def _stop_scanning(rp, scan_stop_node, scan_active_node):
    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(False, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = False")
            return
        except Exception as e:
            print(f"[WARN] ScanActive write failed: {e} — trying ScanStop")

    if scan_stop_node is not None:
        try:
            await rp.call_method(scan_stop_node)
            print("[CMD] ScanStop")
        except Exception as e:
            print(f"[ERR] ScanStop failed: {e}")


async def _run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = _Session()

    trig_label = (
        f"DI{DI_CHANNEL} (IO-Link / photoelectric sensor)"
        if TRIGGER_SOURCE == "DI"
        else "Presence (OPC UA built-in)"
    )
    print("=" * 56)
    print("  Siemens RF695R — OPC UA RFID Logger")
    print(f"  Server    : {OPCUA_URL}")
    print(f"  Read point: {READ_POINT}")
    print(f"  Trigger   : {trig_label}")
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
                    await _browse_tree(client)

                rp, presence_node, scan_active_node, scan_start_node, scan_stop_node, di_node, scan_nodes = (
                    await _find_nodes(client, READ_POINT)
                )

                print("[INIT] Resetting scan state...")
                await _stop_scanning(rp, scan_stop_node, scan_active_node)
                await asyncio.sleep(0.5)

                # Tag detection uses LastScan* polling — fast and firmware-agnostic.
                # Polled at SCAN_POLL while a session is active.
                SCAN_POLL = 0.05  # 50 ms — RF695R updates LastScan* on each tag read

                # ── DI / IO-Link photoelectric sensor trigger ──────────────────
                if TRIGGER_SOURCE == "DI":
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
                    last_di_check = 0.0
                    prev_scan_ts  = None
                    di_err_count  = 0

                    while True:
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
                                await _start_scanning(rp, scan_start_node, scan_active_node)
                                prev_scan_ts = None

                            elif not di_val and prev_di:
                                print("\n[SENSOR] Beam blocked — stopping scan...")
                                await _stop_scanning(rp, scan_stop_node, scan_active_node)
                                session.stop()
                                print(f"[WAIT] Waiting for sensor on DI{DI_CHANNEL}...\n")

                            prev_di = di_val

                        if session.active:
                            prev_scan_ts = await _poll_last_scan(session, scan_nodes, prev_scan_ts)

                        await asyncio.sleep(SCAN_POLL)

                # ── Presence (legacy) trigger ──────────────────────────────────
                else:
                    print("\n[WAIT] Waiting for cart...\n")
                    try:
                        prev_presence = int(await presence_node.read_value()) if presence_node else 0
                    except Exception:
                        prev_presence = 0

                    last_pres_check = 0.0
                    prev_scan_ts    = None
                    pres_err_count  = 0

                    while True:
                        if presence_node is None:
                            await asyncio.sleep(10)
                            print("[ERR] Presence node unavailable. Enable 'Presence events' in WBM.")
                            continue

                        now = time.monotonic()

                        if now - last_pres_check >= POLL_INTERVAL:
                            last_pres_check = now
                            try:
                                pval = int(await presence_node.read_value())
                                pres_err_count = 0
                            except Exception as e:
                                pres_err_count += 1
                                if pres_err_count <= 3:
                                    print(f"[WARN] Presence read failed: {e}")
                                if pres_err_count >= 5:
                                    raise RuntimeError(f"Presence read failed {pres_err_count}x — reconnecting")
                                await asyncio.sleep(POLL_INTERVAL)
                                continue

                            if pval > 0 and prev_presence == 0:
                                session.start(trigger="Presence")
                                await _start_scanning(rp, scan_start_node, scan_active_node)
                                prev_scan_ts = None

                            elif pval == 0 and prev_presence > 0:
                                print("\n[DETECT] Cart left — stopping scan...")
                                await _stop_scanning(rp, scan_stop_node, scan_active_node)
                                session.stop()
                                print("[WAIT] Waiting for next cart...\n")

                            prev_presence = pval

                        if session.active:
                            prev_scan_ts = await _poll_last_scan(session, scan_nodes, prev_scan_ts)

                        await asyncio.sleep(SCAN_POLL)

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
