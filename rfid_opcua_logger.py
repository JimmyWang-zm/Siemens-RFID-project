"""
Siemens SIMATIC RF695R - OPC UA RFID Session Logger
====================================================

Connects to a Siemens RF695R RFID reader over OPC UA, drives scanning based on
an external trigger, and writes each scanning session's tag data to a daily
CSV file.

Trigger modes (configured via TRIGGER_SOURCE)
---------------------------------------------
    "DI"        Polls a digital input (IO-Link photoelectric sensor).
                When the sensor sees its reflector, DI goes HIGH and the script
                issues ScanStart. When the beam is broken, DI goes LOW and the
                script issues ScanStop.

    "Presence"  Polls the reader's built-in Diagnostics/Presence variable.
                Provided as a fallback when no external sensor is wired.

Tag detection
-------------
    Tags are detected by polling the LastScan* variables (Data, Antenna, RSSI,
    Timestamp). This approach is preferred over OPC UA event subscriptions
    because the event filter format varies across firmware versions.

Output
------
    A daily CSV is written to OUTPUT_DIR with one row per unique EPC per
    session. Within a single run, an EPC that has already been saved in any
    previous session is skipped, so each row in the CSV represents a tag
    that was newly identified during the run.

References
----------
    Siemens AH_OPCUA-Ident, sections 3.1.2 and 3.3.3.

Prerequisites (Web-Based Management)
------------------------------------
    Settings > Communication > OPC UA
        - Mode:     Main application   (Parallel must be unchecked)
        - Security: Allow anonymous    (or set OPCUA_USER / OPCUA_PASS below)
        - Port:     4840 (default)

    Settings > DIDO  (only required for TRIGGER_SOURCE = "DI")
        - Mode: IO-Link
        - Wire the photoelectric sensor signal to Input 0
        - Output 0 events:
            Rising  edge of Input 0 -> Output 0 = On    (yellow indicator on)
            Falling edge of Input 0 -> Output 0 = Off   (yellow indicator off)

Usage
-----
    pip install -r requirements.txt
    python rfid_opcua_logger.py

Stop with Ctrl+C.
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


# =============================================================================
# Configuration
# =============================================================================
OPCUA_URL      = "opc.tcp://192.168.0.254:4840"
OPCUA_USER     = ""                              # Empty for anonymous access
OPCUA_PASS     = ""
OUTPUT_DIR     = r"C:\rfid_logger\records"       # CSV output directory
READ_POINT     = 1                               # 1-based read-point index (RF695R supports up to 4)
POLL_INTERVAL  = 0.2                             # Seconds between trigger polls
RETRY_DELAY    = 5                               # Seconds before reconnect attempt

# Trigger source: "DI" uses an external IO-Link sensor; "Presence" uses the
# reader's built-in Diagnostics/Presence variable.
TRIGGER_SOURCE = "DI"
DI_CHANNEL     = 0                               # 0-based digital input channel index

# Set to True once to dump the OPC UA node tree on startup (useful when porting
# to a different firmware version where node paths may differ). Reset to False
# for normal operation.
DEBUG_BROWSE   = False

# Polling interval (in seconds) for the LastScan* variables while a session is
# active. The RF695R updates these on every successful tag read, so 50 ms gives
# near-real-time tag capture without saturating the network.
SCAN_POLL      = 0.05


# =============================================================================
# CSV output
# =============================================================================
def _daily_csv_path() -> str:
    """Return the path of today's CSV file inside OUTPUT_DIR."""
    return os.path.join(OUTPUT_DIR, f"RFID_{datetime.now().strftime('%Y-%m-%d')}.csv")


def _write_csv_rows(path: str, tags: dict, sid: str) -> None:
    """Append the given tags to a CSV file, writing the header on first use.

    Args:
        path: Target CSV file path.
        tags: Mapping of EPC -> list of (timestamp, antenna, rssi) reads.
              Only the most recent read for each EPC is written.
        sid:  Session identifier recorded in the "Session ID" column.
    """
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["Timestamp", "EPC/Tag ID", "Antenna", "RSSI (dBm)", "Session ID"])
        for epc, rows in tags.items():
            ts, ant, rssi = rows[-1]
            w.writerow([ts, epc, ant, rssi, sid])


def _flush_session(tags: dict, sid: str, t0: float) -> None:
    """Save a finished session's tags to disk and print a summary."""
    if not tags:
        print("[INFO] No new tags in session, skipping save")
        return

    primary = _daily_csv_path()
    saved_to = primary
    try:
        _write_csv_rows(primary, tags, sid)
    except (PermissionError, OSError) as e:
        # The daily CSV is locked by another process (e.g. open in Excel).
        # Fall back to a per-session backup file so data is never lost; the
        # operator can merge it back into the daily CSV manually.
        backup = os.path.join(OUTPUT_DIR, f"RFID_{sid}.csv")
        print(f"[WARN] Daily CSV locked ({e.__class__.__name__}). Saving to backup: {backup}")
        try:
            _write_csv_rows(backup, tags, sid)
            saved_to = backup
        except Exception as e2:
            print(f"[ERR] Backup save also failed: {e2}")
            return

    duration = time.time() - t0
    print(f"\n{'=' * 56}")
    print(f"  Session done : {sid}  ({duration:.1f}s)")
    print(f"  New tags     : {len(tags)}")
    for epc, rows in tags.items():
        _, ant, rssi = rows[-1]
        print(f"    {epc}  Ant:{ant}  RSSI:{rssi}")
    print(f"  Saved to     : {saved_to}")
    print(f"{'=' * 56}\n")


# =============================================================================
# Session model
# =============================================================================
class _Session:
    """Tracks the tags collected between a ScanStart and the matching ScanStop.

    A session starts when the trigger goes active (e.g. the photoelectric
    sensor sees its reflector) and ends when it goes inactive again. Tags
    that were already saved in a previous session of the same run are
    silently ignored, so each session's CSV write only contains genuinely
    new EPCs.
    """

    def __init__(self) -> None:
        self._no       = 0           # Monotonic session counter for this run
        self.active    = False
        self.tags:  dict  = {}
        self.sid:   str   = ""
        self.t0:    float = 0.0
        self._seen_epcs: set = set()  # EPCs already persisted to CSV in this run

    def start(self, trigger: str = "") -> None:
        self._no   += 1
        self.active = True
        self.tags   = {}
        self.t0     = time.time()
        self.sid    = f"S{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self._no:03d}"
        label = "Reflector detected" if trigger == "DI" else "Cart arrived"
        print(f"\n[DETECT] {label}  ->  session {self.sid}")

    def stop(self) -> None:
        if self.active:
            try:
                _flush_session(self.tags, self.sid, self.t0)
                self._seen_epcs.update(self.tags.keys())
            except Exception as e:
                print(f"[ERR] Session save failed: {e}  (continuing)")
        self.active = False
        self.tags   = {}

    def add_tag(self, epc: str, ant: str, rssi: str) -> None:
        if not self.active:
            return
        if epc in self._seen_epcs:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if epc not in self.tags:
            self.tags[epc] = []
            print(f"  [NEW]  {epc}  Ant:{ant}  RSSI:{rssi}")
        else:
            print(f"  [TAG]  {epc}  Ant:{ant}  RSSI:{rssi}", end="\r")
        self.tags[epc].append((ts, ant, rssi))


# =============================================================================
# Tag polling
# =============================================================================
def _epc_to_hex(raw) -> str:
    """Convert an EPC value reported by OPC UA into an uppercase hex string."""
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.hex().upper()
    return str(raw)


async def _poll_last_scan(session: "_Session", scan_nodes: dict, prev_ts):
    """Poll the LastScan* variables and forward any new tag read to ``session``.

    Returns the most recent timestamp seen so the caller can detect changes
    on the next call. We use polling instead of OPC UA event subscription
    because the RF695R event filter schema varies across firmware versions
    and is not consistently supported by asyncua.
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

    # The AutoID specification reports RSSI in cdBm (1 cdBm = 0.01 dBm).
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


# =============================================================================
# OPC UA node discovery
# =============================================================================
async def _browse_tree(client: Client, max_depth: int = 5) -> None:
    """Print the full OPC UA node tree under Objects (debug aid)."""
    root    = client.get_root_node()
    objects = await root.get_child(["0:Objects"])

    async def _print(node, depth: int) -> None:
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
    print("  Search for nodes named 'Input', 'DI', 'IOLink', 'ProcessData'")
    print("=" * 60)
    await _print(objects, 0)
    print("=" * 60 + "\n")


async def _find_di_node(client: Client, rp, di_channel: int):
    """Return the node holding the digital-input bitmask for the read point.

    The known RF695R path is::

        <ReadPoint> > IOData > DigitalIOPorts > DigitalInputs

    DigitalInputs is a bitmask variable: bit N reflects the state of channel N.
    If the direct path is not found (e.g. on a different firmware version),
    falls back to a depth-first search.
    """
    # Direct path - matches RF695R as observed in the field.
    try:
        for c in await rp.get_children():
            if (await c.read_browse_name()).Name == "IOData":
                for c2 in await c.get_children():
                    if (await c2.read_browse_name()).Name == "DigitalIOPorts":
                        for c3 in await c2.get_children():
                            if (await c3.read_browse_name()).Name == "DigitalInputs":
                                print(f"       DI inputs  OK  [IOData > DigitalIOPorts > DigitalInputs (bit {di_channel})]")
                                return c3
    except Exception as e:
        print(f"[WARN] Direct IOData path failed: {e}")

    # Fallback: depth-first search for plausibly-named DI nodes.
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
            next_in_group = in_group or (bn in group_names)
            result = await dfs(child, next_in_group, depth + 1)
            if result:
                return result
        return None

    found = await dfs(search_root, in_group=False, depth=0)
    if found:
        name = (await found.read_browse_name()).Name
        print(f"       DI{di_channel} node  OK  [{name}]  (DFS fallback)")
    else:
        print(f"[WARN] DI{di_channel} node not found.")
        print("       Set DEBUG_BROWSE=True and restart to inspect the OPC UA tree.")
    return found


async def _find_nodes(client: Client, rp_index: int):
    """Locate the OPC UA nodes used by the logger for a given read point.

    Returns a tuple of:
        (read_point, presence_node, scan_active_node, scan_start_node,
         scan_stop_node, di_node, scan_nodes)

    Raises RuntimeError if the read point cannot be found.
    """
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
        raise RuntimeError("DeviceSet node not found. Verify OPC UA is enabled in WBM.")

    children   = await device_set.get_children()
    readpoints = [
        c for c in children
        if "read_point" in (await c.read_browse_name()).Name.lower()
        or "readpoint"  in (await c.read_browse_name()).Name.lower()
    ]
    if not readpoints:
        raise RuntimeError("No read points found under DeviceSet.")
    if rp_index > len(readpoints):
        raise RuntimeError(f"Read point {rp_index} not found (only {len(readpoints)} available).")

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
                    print("       Presence   OK")
        elif bn == "ScanActive":
            scan_active_node = c
            print("       ScanActive OK")
        elif bn == "ScanStart":
            scan_start_node = c
            print("       ScanStart  OK")
        elif bn == "ScanStop":
            scan_stop_node = c
            print("       ScanStop   OK")
        elif bn == "LastScanData":
            scan_nodes["data"] = c
        elif bn == "LastScanAntenna":
            scan_nodes["antenna"] = c
        elif bn == "LastScanRSSI":
            scan_nodes["rssi"] = c
        elif bn == "LastScanTimestamp":
            scan_nodes["timestamp"] = c

    if all(scan_nodes[k] is not None for k in ("data", "antenna", "rssi", "timestamp")):
        print("       LastScan*  OK  (Data / Antenna / RSSI / Timestamp)")
    else:
        missing = [k for k, v in scan_nodes.items() if v is None]
        print(f"[WARN] LastScan* nodes incomplete: {missing}")

    if TRIGGER_SOURCE == "Presence" and presence_node is None:
        print("[WARN] Presence node not found. Enable 'Presence events' in WBM > OPC UA.")

    di_node = None
    if TRIGGER_SOURCE == "DI":
        di_node = await _find_di_node(client, rp, DI_CHANNEL)

    return rp, presence_node, scan_active_node, scan_start_node, scan_stop_node, di_node, scan_nodes


# =============================================================================
# Scan control
# =============================================================================
async def _start_scanning(rp, scan_start_node, scan_active_node) -> None:
    """Activate continuous scanning on the read point.

    Writing ScanActive=True is the most reliable activation path on the
    RF695R; it is a direct boolean variable and avoids the variability of
    method-call signatures across firmware versions. The ScanStart method
    is used only as a fallback.
    """
    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(True, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = True")
            return
        except Exception as e:
            print(f"[WARN] ScanActive write failed: {e} - falling back to ScanStart")

    if scan_start_node is not None:
        try:
            scan_settings_cls = getattr(ua, "ScanSettings", None)
            if scan_settings_cls is not None:
                ss = scan_settings_cls()
                ss.Cycles        = 0
                ss.DataAvailable = True   # Fire an event for every tag found
                ss.Duration      = 0
                await rp.call_method(scan_start_node, ss)
            else:
                await rp.call_method(
                    scan_start_node,
                    ua.Variant(0,    ua.VariantType.UInt32),
                    ua.Variant(True, ua.VariantType.Boolean),
                    ua.Variant(0,    ua.VariantType.UInt32),
                )
            print("[CMD] ScanStart")
        except Exception as e:
            print(f"[ERR] ScanStart failed: {e}")


async def _stop_scanning(rp, scan_stop_node, scan_active_node) -> None:
    """Deactivate continuous scanning on the read point."""
    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(False, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = False")
            return
        except Exception as e:
            print(f"[WARN] ScanActive write failed: {e} - falling back to ScanStop")

    if scan_stop_node is not None:
        try:
            await rp.call_method(scan_stop_node)
            print("[CMD] ScanStop")
        except Exception as e:
            print(f"[ERR] ScanStop failed: {e}")


# =============================================================================
# Main loop
# =============================================================================
async def _run() -> None:
    """Main connect-and-monitor loop. Reconnects automatically on errors."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = _Session()

    trig_label = (
        f"DI{DI_CHANNEL} (IO-Link / photoelectric sensor)"
        if TRIGGER_SOURCE == "DI"
        else "Presence (OPC UA built-in)"
    )
    print("=" * 56)
    print("  Siemens RF695R - OPC UA RFID Logger")
    print(f"  Server     : {OPCUA_URL}")
    print(f"  Read point : {READ_POINT}")
    print(f"  Trigger    : {trig_label}")
    print(f"  Output dir : {OUTPUT_DIR}")
    print("  Press Ctrl+C to stop.")
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

                (rp, presence_node, scan_active_node, scan_start_node,
                 scan_stop_node, di_node, scan_nodes) = await _find_nodes(client, READ_POINT)

                print("[INIT] Resetting scan state ...")
                await _stop_scanning(rp, scan_stop_node, scan_active_node)
                await asyncio.sleep(0.5)

                if TRIGGER_SOURCE == "DI":
                    await _run_di_loop(rp, di_node, scan_nodes,
                                       scan_active_node, scan_start_node, scan_stop_node,
                                       session)
                else:
                    await _run_presence_loop(rp, presence_node, scan_nodes,
                                             scan_active_node, scan_start_node, scan_stop_node,
                                             session)

        except KeyboardInterrupt:
            print("\n[STOP] Shutting down ...")
            if session.active:
                session.stop()
            print(f"[STOP] Records saved to: {OUTPUT_DIR}")
            return

        except Exception as e:
            if session.active:
                session.stop()
            print(f"[ERR] {e}")
            print(f"[RETRY] Reconnecting in {RETRY_DELAY}s ...\n")
            await asyncio.sleep(RETRY_DELAY)


async def _run_di_loop(rp, di_node, scan_nodes,
                       scan_active_node, scan_start_node, scan_stop_node,
                       session: "_Session") -> None:
    """Trigger loop driven by a digital-input photoelectric sensor."""
    if di_node is None:
        print(f"[ERR] DI{DI_CHANNEL} node not found - cannot run DI trigger.")
        print("      Set DEBUG_BROWSE=True and restart to inspect the OPC UA tree.")
        await asyncio.sleep(RETRY_DELAY)
        return

    try:
        raw     = await di_node.read_value()
        prev_di = bool((int(raw) >> DI_CHANNEL) & 1)
    except Exception:
        prev_di = False

    print(f"\n[WAIT] Waiting for sensor on DI{DI_CHANNEL} ...\n")
    last_di_check = 0.0
    prev_scan_ts  = None
    err_count     = 0

    while True:
        now = time.monotonic()

        if now - last_di_check >= POLL_INTERVAL:
            last_di_check = now
            try:
                raw       = await di_node.read_value()
                di_val    = bool((int(raw) >> DI_CHANNEL) & 1)
                err_count = 0
            except Exception as e:
                err_count += 1
                if err_count <= 3:
                    print(f"[WARN] DI{DI_CHANNEL} read failed: {e}")
                if err_count >= 5:
                    raise RuntimeError(f"DI read failed {err_count}x - reconnecting")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if di_val and not prev_di:
                session.start(trigger="DI")
                await _start_scanning(rp, scan_start_node, scan_active_node)
                prev_scan_ts = None

            elif not di_val and prev_di:
                print("\n[SENSOR] Beam blocked - stopping scan ...")
                await _stop_scanning(rp, scan_stop_node, scan_active_node)
                session.stop()
                print(f"[WAIT] Waiting for sensor on DI{DI_CHANNEL} ...\n")

            prev_di = di_val

        if session.active:
            prev_scan_ts = await _poll_last_scan(session, scan_nodes, prev_scan_ts)

        await asyncio.sleep(SCAN_POLL)


async def _run_presence_loop(rp, presence_node, scan_nodes,
                             scan_active_node, scan_start_node, scan_stop_node,
                             session: "_Session") -> None:
    """Trigger loop driven by the reader's built-in Presence variable."""
    print("\n[WAIT] Waiting for cart ...\n")
    try:
        prev_presence = int(await presence_node.read_value()) if presence_node else 0
    except Exception:
        prev_presence = 0

    last_pres_check = 0.0
    prev_scan_ts    = None
    err_count       = 0

    while True:
        if presence_node is None:
            await asyncio.sleep(10)
            print("[ERR] Presence node unavailable. Enable 'Presence events' in WBM.")
            continue

        now = time.monotonic()

        if now - last_pres_check >= POLL_INTERVAL:
            last_pres_check = now
            try:
                pval      = int(await presence_node.read_value())
                err_count = 0
            except Exception as e:
                err_count += 1
                if err_count <= 3:
                    print(f"[WARN] Presence read failed: {e}")
                if err_count >= 5:
                    raise RuntimeError(f"Presence read failed {err_count}x - reconnecting")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if pval > 0 and prev_presence == 0:
                session.start(trigger="Presence")
                await _start_scanning(rp, scan_start_node, scan_active_node)
                prev_scan_ts = None

            elif pval == 0 and prev_presence > 0:
                print("\n[DETECT] Cart left - stopping scan ...")
                await _stop_scanning(rp, scan_stop_node, scan_active_node)
                session.stop()
                print("[WAIT] Waiting for next cart ...\n")

            prev_presence = pval

        if session.active:
            prev_scan_ts = await _poll_last_scan(session, scan_nodes, prev_scan_ts)

        await asyncio.sleep(SCAN_POLL)


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
