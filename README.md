# Siemens RF695R RFID Session Logger

A Python service that drives a Siemens **SIMATIC RF695R** RFID reader via
**OPC UA**, automatically scans tags whenever a photoelectric sensor sees its
reflector, and writes each scanning session to a daily CSV file.

The service is designed to be left running on a workstation that sits next to
the read point. It connects to the reader, monitors a digital input, starts and
stops scanning automatically, and never duplicates tags within a single run.

---

## Implementation branches

The same end-to-end functionality is delivered through two independent
implementations, maintained on separate branches:

| Branch                    | Tag detection mechanism             | Notes |
|---------------------------|-------------------------------------|-------|
| `feature/variable-based`  | Polls the `LastScan*` OPC UA variables | Robust across firmware versions; recommended default. |
| `feature/event-based`     | Subscribes to `RfidScanEventType` OPC UA events | Lower CPU usage but depends on firmware-specific event filters. |

Both branches share the same digital-input trigger logic, configuration layout,
and CSV output format. Use whichever fits your deployment. The default branch
(`master`) tracks the **variable-based** implementation as the reference build.

---

## Hardware setup

| Component | Notes |
|-----------|-------|
| Reader    | Siemens SIMATIC RF695R |
| Antenna   | Siemens RF662A (or compatible UHF antenna) |
| Sensor    | IO-Link photoelectric sensor with reflector |
| Wiring    | Sensor signal -> reader **Input 0** (DI0) |
| Network   | Reader and PC on the same subnet (default reader IP `192.168.0.254`) |

## Software requirements

| Item    | Version              |
|---------|----------------------|
| Python  | 3.9 or newer         |
| OS      | Windows 10 / 11      |
| Library | `asyncua` >= 1.0.0   |

```powershell
pip install -r requirements.txt
```

---

## How it works

```
Sensor sees reflector  ->  DI0 = HIGH  ->  ScanActive = True   ->  tags streamed
Beam blocked           ->  DI0 = LOW   ->  ScanActive = False  ->  session saved
```

1. The script opens an OPC UA session to the reader and locates the read
   point and digital-input node.
2. A polling loop checks DI0 every 200 ms.
3. When DI0 transitions from low to high, a new **session** begins:
   `ScanActive` is set to `True` and the reader starts streaming tags.
4. Tags are read by polling the `LastScan*` variables every 50 ms.
5. When DI0 transitions from high to low, the session ends: `ScanActive` is
   set to `False` and the collected tags are flushed to the daily CSV file.
6. Tags that have already been written in any earlier session of the same
   run are skipped, so each session only logs newly identified EPCs.

A legacy `Presence` mode that uses the reader's built-in presence variable is
also supported via the `TRIGGER_SOURCE` setting.

---

## Reader configuration (Web-Based Management)

### OPC UA — *Settings > Communication > OPC UA*

| Setting  | Required value                       |
|----------|--------------------------------------|
| Mode     | **Main application** (uncheck *Parallel*) |
| Security | Allow anonymous access (or set `OPCUA_USER` / `OPCUA_PASS`) |
| Port     | 4840 (default)                       |

### Digital I/O — *Settings > DIDO*

1. At the top of the DIDO page, set the mode dropdown to **IO-Link**.
2. Wire the photoelectric sensor signal output to **Input 0** (DI0).
3. On the **Output 0** tab, add the following two event rules:

   | # | On            | Input    | Edge    | Action               |
   |---|---------------|----------|---------|----------------------|
   | 1 | Input change  | Input 0  | Rising  | Output 0 = **On**    |
   | 2 | Input change  | Input 0  | Falling | Output 0 = **Off**   |

4. Click **Save** and apply.

The Output 0 rules drive the physical yellow indicator. The script independently
monitors DI0 over OPC UA and issues `ScanStart` / `ScanStop` accordingly.

---

## Quick start

```powershell
pip install -r requirements.txt
python rfid_opcua_logger.py
```

Or simply double-click `run_opcua_logger.bat` — it installs `asyncua`
automatically on first run.

Stop the service with `Ctrl+C`.

---

## Configuration

All runtime parameters are defined at the top of `rfid_opcua_logger.py`:

| Variable          | Default                          | Description |
|-------------------|----------------------------------|-------------|
| `OPCUA_URL`       | `opc.tcp://192.168.0.254:4840`   | OPC UA endpoint of the reader |
| `OPCUA_USER`      | `""`                             | WBM username (empty = anonymous) |
| `OPCUA_PASS`      | `""`                             | WBM password |
| `OUTPUT_DIR`      | `C:\rfid_logger\records`         | CSV output directory |
| `READ_POINT`      | `1`                              | 1-based read-point index (RF695R supports up to 4) |
| `POLL_INTERVAL`   | `0.2`                            | DI / Presence polling interval, in seconds |
| `SCAN_POLL`       | `0.05`                           | LastScan* polling interval while a session is active |
| `RETRY_DELAY`     | `5`                              | Seconds before reconnect after a failure |
| `TRIGGER_SOURCE`  | `"DI"`                           | `"DI"` for IO-Link sensor, `"Presence"` for built-in detection |
| `DI_CHANNEL`      | `0`                              | 0-based DI channel index |
| `DEBUG_BROWSE`    | `False`                          | Set `True` once to dump the OPC UA tree on startup |

### First-run node discovery

If startup logs `DI0 node not found`, the OPC UA path to the digital input
differs on your firmware version. Set `DEBUG_BROWSE = True`, run the script
once, and search the printed tree for nodes named `Input`, `DI`, `IOLink`,
or `ProcessData`. Then set `DEBUG_BROWSE = False` for normal operation.

---

## Output

A daily CSV is written to `OUTPUT_DIR`:

```
RFID_YYYY-MM-DD.csv
```

If the file is locked by another process (for example, opened in Excel),
the script falls back to a per-session backup file named `RFID_<sessionID>.csv`
so no data is lost.

### CSV columns

| Column        | Description                                             |
|---------------|---------------------------------------------------------|
| `Timestamp`   | Local time of the read (`YYYY-MM-DD HH:MM:SS`)          |
| `EPC/Tag ID`  | EPC identifier in uppercase hex                         |
| `Antenna`     | Antenna port that produced the read                     |
| `RSSI (dBm)`  | Signal strength (the reader reports cdBm; converted to dBm) |
| `Session ID`  | `S<YYYYMMDD>_<HHMMSS>_<NNN>` — one ID per scan session  |

---

## Repository layout

```
rfid_logger/
├── README.md                  This file
├── rfid_opcua_logger.py       Main service
├── run_opcua_logger.bat       One-click launcher (Windows)
├── requirements.txt
├── .gitignore
└── records/                   Default CSV output directory (gitignored)
```

---

## Operational notes

* For unattended operation, run `run_opcua_logger.bat` from Windows Task
  Scheduler with the trigger *At log on*.
* The script reconnects automatically after up to five consecutive read
  failures, so transient network drops do not stop the service.
* Tag de-duplication is per-run only. Restarting the script begins a new
  de-duplication scope.
