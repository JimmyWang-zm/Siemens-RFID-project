"""
rfid_opcua.session
──────────────────
Session tracking and CSV persistence.

The CSV is **overwritten** on every session so that it always contains
only the latest scan result — ready for pick-up by a SCADA system.
"""

import csv
import logging
import os
import tempfile
import time
from datetime import datetime

from . import config as _cfg

log = logging.getLogger(__name__)

_CSV_HEADER = ["Timestamp", "EPC/Tag ID", "Antenna", "RSSI (dBm)", "Session ID"]


def csv_path() -> str:
    """Return the path to the single persistent CSV file (reads config at runtime)."""
    return os.path.join(_cfg.OUTPUT_DIR, _cfg.CSV_FILENAME)


def ensure_csv() -> str:
    """
    Ensure the CSV exists (with header).  Called at startup so the
    file is visible immediately, even before the first session completes.
    Returns the path.
    """
    path = csv_path()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            f.write("sep=;\n")   # tell Excel to use ; as separator
            csv.writer(f, delimiter=";").writerow(_CSV_HEADER)
    return path


def flush_session(tags: dict, sid: str, t0: float):
    """
    Overwrite the CSV with header + current session tags.

    Atomic write: rows go to a temp file first, then the temp file is
    renamed over the real CSV.  If the process crashes mid-write only
    the temp file is lost — the previous CSV stays intact.
    """
    if not tags:
        log.info("Session %s — no tags, nothing saved", sid)
        return

    path = csv_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Build rows
    rows: list[list[str]] = []
    for epc, entries in tags.items():
        ts, ant, rssi = entries[-1]
        # Replace placeholder '?' with empty string for clean CSV/Excel output
        a = ant if ant != "?" else ""
        r = rssi if rssi != "?" else ""
        rows.append([ts, epc, a, r, sid])

    # Atomic write: temp file → rename over real CSV
    dir_name = os.path.dirname(path)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".csv.tmp", dir=dir_name)
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as tmp_f:
            tmp_f.write("sep=;\n")   # tell Excel to use ; as separator
            w = csv.writer(tmp_f, delimiter=";")
            w.writerow(_CSV_HEADER)
            w.writerows(rows)

        # On Windows os.rename fails if target exists — use os.replace
        os.replace(tmp_path, path)
        tmp_path = None  # successfully moved
    except Exception as e:
        log.error("CSV write failed: %s", e)
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return

    dur = time.time() - t0
    log.info(
        "<<< Session %s done — %d tag(s) in %.1fs — saved to %s",
        sid, len(tags), dur, os.path.basename(path),
    )
    log.debug("  Tags: %s", ", ".join(tags.keys()))


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
        log.info(">>> Session %s started", self.sid)

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
            log.info("    + %s  (Ant:%s  RSSI:%s)", epc, ant, rssi)
        self.tags[epc].append((ts, ant, rssi))
