"""
rfid_opcua_logger.py
Siemens SIMATIC RF695R — OPC UA based RFID session logger

Thin launcher — all logic lives in the ``rfid_opcua`` package.
Edit ``rfid_opcua/config.py`` to change settings.

Usage:  python rfid_opcua_logger.py   (or:  python -m rfid_opcua)
Stop:   Ctrl+C
"""

from rfid_opcua.main import main

if __name__ == "__main__":
    main()

