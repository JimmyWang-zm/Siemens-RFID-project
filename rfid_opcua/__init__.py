# rfid_opcua — Siemens RF695R OPC UA RFID logger package

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_BACKUP_COUNT, LOG_DIR, LOG_LEVEL, LOG_MAX_BYTES


def setup_logging():
    """
    Configure the root ``rfid_opcua`` logger.

    * Console handler  — always present, coloured-tag format.
    * File handler     — rotating log files in LOG_DIR.
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    # Silence noisy asyncua library messages
    logging.getLogger("asyncua").setLevel(logging.ERROR)

    root_log = logging.getLogger("rfid_opcua")
    root_log.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    # Prevent duplicate handlers on reconnect / reimport
    if root_log.handlers:
        return root_log

    # Console — compact (no module name)
    console_fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(message)s",
        datefmt="%H:%M:%S",
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(console_fmt)
    root_log.addHandler(ch)

    # Rotating file — detailed (includes module)
    file_fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log_path = os.path.join(LOG_DIR, "rfid_opcua.log")
    fh = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(file_fmt)
    root_log.addHandler(fh)

    return root_log
