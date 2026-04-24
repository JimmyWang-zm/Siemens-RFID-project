"""
rfid_auto_save.py
Siemens SIMATIC RF695R — TCP XML channel session logger

Connects to the reader's XML channel, sends a start command, and records
tags to a daily CSV. A session begins on the first tag detected and ends
after IDLE_TIMEOUT seconds of no tag activity (cart has left).

Device: Siemens SIMATIC RF695R + RF662A antenna
Usage:  python rfid_auto_save.py
Stop:   Ctrl+C
"""

import socket
import csv
import os
import xml.etree.ElementTree as ET
from datetime import datetime
import time
import urllib.request
import urllib.error

# ── Configuration ─────────────────────────────────────────
RFID_IP      = "192.168.0.254"
RFID_PORT    = 10001
OUTPUT_DIR   = r"C:\rfid_logger\records"
IDLE_TIMEOUT = 5.0    # seconds without tags before session closes
RECV_TIMEOUT = 1.0    # socket recv timeout (used for idle detection)
RETRY_DELAY  = 5      # seconds before reconnect

# Optional HTTP trigger URL (e.g. "http://192.168.0.254/diagrams/tagmonitor/start")
# Leave empty to use the XML command approach below.
HTTP_START_URL = ""

XML_START_COMMANDS = [
    b"<AutoRead_Req/>\r\n",
    b'<AutoRead_Req xmlns="com.siemens.industry.rfid.reader"/>\r\n',
    b"<StartInventory/>\r\n",
    b"<Cmd>StartInventory</Cmd>\r\n",
]
# ──────────────────────────────────────────────────────────


def _daily_csv():
    return os.path.join(OUTPUT_DIR, f"RFID_{datetime.now().strftime('%Y-%m-%d')}.csv")


def _ensure_header(path):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["Timestamp", "EPC/Tag ID", "Antenna", "RSSI (dBm)", "Event", "Session ID"]
            )


def _try_http_start():
    if not HTTP_START_URL:
        return False
    try:
        resp = urllib.request.urlopen(HTTP_START_URL, timeout=3)
        print(f"  [HTTP] Started via {HTTP_START_URL} (status {resp.status})")
        return True
    except urllib.error.HTTPError as e:
        if e.code in (200, 204):
            return True
        print(f"  [HTTP] HTTP {e.code}: {HTTP_START_URL}")
        return False
    except Exception as e:
        print(f"  [HTTP] Failed: {e}")
        return False


def _try_xml_start(sock):
    for cmd in XML_START_COMMANDS:
        try:
            sock.send(cmd)
            sock.settimeout(0.8)
            try:
                resp = sock.recv(512)
                if resp:
                    print(f"  [XML] Command accepted, response: {resp[:120]}")
                    return True
            except socket.timeout:
                pass
        except OSError:
            return False
    print("  [XML] Start command sent (no response — waiting for data)")
    return True


def _send_start(sock):
    print("[START] Sending scan start command...")
    if not _try_http_start():
        _try_xml_start(sock)


def _parse_tags(xml_data):
    results = []
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return results
    for event in root.iter():
        for tag in event.findall(".//Tag"):
            epc     = tag.findtext("EPC") or tag.findtext("UID") or tag.get("epc", "N/A")
            antenna = tag.findtext("AntennaName") or tag.findtext("Antenna") or "N/A"
            rssi    = tag.findtext("RSSI") or "N/A"
            results.append((epc, antenna, rssi, event.tag))
    return results


def _save_session(session_tags, session_id, session_start):
    if not session_tags:
        print("[INFO] No tags in session, skipping save\n")
        return
    path = _daily_csv()
    _ensure_header(path)
    duration = time.time() - session_start
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for epc, records in session_tags.items():
            ts, antenna, rssi, etype = records[-1]
            writer.writerow([ts, epc, antenna, rssi, etype, session_id])
    print(f"\n{'=' * 56}")
    print(f"  Session done : {session_id}  ({duration:.1f}s)")
    print(f"  Tags found   : {len(session_tags)}")
    for epc in session_tags:
        _, antenna, rssi, _ = session_tags[epc][-1]
        print(f"    {epc}  Ant:{antenna}  RSSI:{rssi}")
    print(f"  Saved to     : {path}")
    print(f"{'=' * 56}\n")


def _receive_stream(sock):
    buf = b""
    while True:
        try:
            sock.settimeout(RECV_TIMEOUT)
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            text = buf.decode("utf-8", errors="replace")
            if text.rstrip().endswith(">"):
                yield text.strip()
                buf = b""
        except socket.timeout:
            if buf:
                yield buf.decode("utf-8", errors="replace").strip()
                buf = b""
            yield None
        except OSError:
            break


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("=" * 56)
    print("  Siemens RF695R — TCP XML Session Logger")
    print(f"  Reader    : {RFID_IP}:{RFID_PORT}")
    print(f"  Output    : {OUTPUT_DIR}")
    print(f"  Idle timeout : {IDLE_TIMEOUT}s")
    print("  Ctrl+C to stop")
    print("=" * 56)

    STATE_IDLE   = "idle"
    STATE_ACTIVE = "active"

    state         = STATE_IDLE
    session_tags  = {}
    session_id    = None
    session_start = None
    last_tag_time = None
    session_no    = 0

    try:
        while True:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                print(f"\n[CONN] Connecting to {RFID_IP}:{RFID_PORT} ...")
                sock.settimeout(10)
                sock.connect((RFID_IP, RFID_PORT))
                print("[CONN] Connected")
                _send_start(sock)
                print("[WAIT] Waiting for cart...\n")

                for xml_data in _receive_stream(sock):
                    if xml_data is None:
                        if state == STATE_ACTIVE:
                            idle = time.time() - last_tag_time
                            remaining = IDLE_TIMEOUT - idle
                            if remaining <= 0:
                                _save_session(session_tags, session_id, session_start)
                                state         = STATE_IDLE
                                session_tags  = {}
                                session_id    = None
                                session_start = None
                                print("[WAIT] Waiting for next cart...\n")
                                _try_xml_start(sock)
                            else:
                                print(f"  [IDLE] Closing session in {remaining:.1f}s ...", end="\r")
                        continue

                    tags = _parse_tags(xml_data)
                    if not tags:
                        continue

                    now     = time.time()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    if state == STATE_IDLE:
                        session_no   += 1
                        session_id    = f"S{datetime.now().strftime('%Y%m%d_%H%M%S')}_{session_no:03d}"
                        session_start = now
                        state         = STATE_ACTIVE
                        print(f"[DETECT] Cart arrived  →  session {session_id}")

                    last_tag_time = now
                    for epc, antenna, rssi, etype in tags:
                        if epc not in session_tags:
                            session_tags[epc] = []
                            print(f"  [NEW]  {epc}  Ant:{antenna}  RSSI:{rssi}")
                        else:
                            print(f"  [TAG]  {epc}  Ant:{antenna}  RSSI:{rssi}", end="\r")
                        session_tags[epc].append((now_str, antenna, rssi, etype))

            except ConnectionRefusedError:
                print(f"[ERR] Connection refused, retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            except (socket.timeout, socket.error) as e:
                print(f"[ERR] Socket error: {e}, retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            finally:
                sock.close()

    except KeyboardInterrupt:
        if state == STATE_ACTIVE and session_tags:
            print("\n[STOP] Saving open session...")
            _save_session(session_tags, session_id, session_start)
        print(f"[STOP] Records saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
