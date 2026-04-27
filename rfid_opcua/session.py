"""
rfid_opcua.session
──────────────────
Session tracking and CSV persistence.
"""

import csv
import os
import time
from datetime import datetime

from .config import OUTPUT_DIR


def daily_csv_path() -> str:
    """Return today's CSV file path."""
    return os.path.join(OUTPUT_DIR, f"RFID_{datetime.now().strftime('%Y-%m-%d')}.csv")


def _ensure_header(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(
                ["Timestamp", "EPC/Tag ID", "Antenna", "RSSI (dBm)", "Session ID"]
            )


def flush_session(tags: dict, sid: str, t0: float):
    """Write collected tags to the daily CSV and print summary."""
    if not tags:
        print("[INFO] No tags in session, skipping save")
        return
    path = daily_csv_path()
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


class Session:
    """Tracks one sensor-trigger → scan → save cycle."""

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
            flush_session(self.tags, self.sid, self.t0)
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
