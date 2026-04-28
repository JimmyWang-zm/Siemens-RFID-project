"""
rfid_opcua_gui.py  –  Siemens RF695R · RFID Logger
───────────────────────────────────────────────────
Clean light-themed GUI.  Background OPC UA thread writes shared
state; a fast 60 ms GUI poll reads it.  No monkey-patching.

    python rfid_opcua_gui.py
"""
from __future__ import annotations
import asyncio, logging, os, queue, subprocess, sys
import threading, time, tkinter as tk
from tkinter import ttk
from datetime import datetime
import customtkinter as ctk

try:
    from rfid_opcua import setup_logging
    import rfid_opcua.config as _cfg
    from rfid_opcua.config import (
        CSV_FILENAME, OPCUA_URL, READ_POINT, DI_CHANNEL,
    )
    from rfid_opcua.handlers import DIHandler
    from rfid_opcua.opcua_helpers import (
        browse_tree, find_nodes, setup_scan_event_subscription,
        start_scanning, stop_scanning,
    )
    from rfid_opcua.session import Session, ensure_csv, csv_path
except ImportError as exc:
    sys.exit(f"Import error: {exc}")

log = logging.getLogger("rfid_opcua.gui")

# ── design tokens ─────────────────────────────────────────────────────────────
BG       = "#f0f1f3"
CARD     = "#ffffff"
BORDER   = "#dfe1e6"
TXT      = "#16161a"
TXT2     = "#4e4e5c"
MUTED    = "#8c8c9e"
PETROL   = "#009999"
PETROL_H = "#007d7d"
NAVY     = "#000028"
GREEN    = "#059669"
GREEN_BG = "#d1fae5"
RED      = "#dc2626"
RED_BG   = "#fee2e2"
AMBER    = "#d97706"
AMBER_BG = "#fef3c7"
WHITE    = "#ffffff"
F        = "Segoe UI"
M        = "Consolas"
TICK     = 60          # ms poll


# ═══════════════════════════════════════════════════════════════════════════════
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.title("Siemens RF695R — RFID Logger")
        self.configure(fg_color=BG)
        self.minsize(960, 680)
        self.geometry("1100x780")

        # ── shared state ──────────────────────────────────────────────────
        self._ses        = Session()
        self._conn       = False
        self._di         = False
        self._stop       = threading.Event()
        self._lq: queue.Queue[tuple[str, str]] = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thr: threading.Thread | None = None
        self._log_ok     = False

        # ── gui snapshot ──────────────────────────────────────────────────
        self._gc = False; self._gd = False; self._ga = False
        self._gs = ""; self._gn = 0
        self._seen: set[str] = set()
        self._ids: dict[str, str] = {}
        self._fix: set[str] = set()

        # ── layout: use grid with 4 rows ─────────────────────────────────
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=3)          # table gets most space
        self.rowconfigure(3, weight=1)          # log gets some space too

        self._mk_topbar()       # row 0
        self._mk_kpis()         # row 1
        self._mk_table()        # row 2
        self._mk_bottom()       # row 3

        self._mk_loghandler()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(TICK, self._poll)

    # ══════════════════════════════════════════════════════════════════════
    #  HEADER
    # ══════════════════════════════════════════════════════════════════════
    def _mk_topbar(self):
        hdr = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0,
                           border_width=0)
        hdr.grid(row=0, column=0, sticky="ew")

        # ── top row: brand + server info ──────────────────────────────────
        top = ctk.CTkFrame(hdr, fg_color="transparent")
        top.pack(fill="x", padx=28, pady=(16, 0))

        # left: Siemens brand
        brand = ctk.CTkFrame(top, fg_color="transparent")
        brand.pack(side="left", anchor="s")
        ctk.CTkLabel(brand, text="SIEMENS",
                     font=ctk.CTkFont(F, 24, "bold"),
                     text_color=PETROL).pack(side="left")

        # right: settings button (prominent)
        right = ctk.CTkFrame(top, fg_color="transparent")
        right.pack(side="right", anchor="s")
        ctk.CTkLabel(right, text=OPCUA_URL,
                     font=ctk.CTkFont(M, 11),
                     text_color=MUTED).pack(side="left")
        ctk.CTkLabel(right, text=f"  ·  RP {READ_POINT}  ·  DI{DI_CHANNEL}",
                     font=ctk.CTkFont(F, 11),
                     text_color=MUTED).pack(side="left")
        ctk.CTkButton(
            right, text="⚙  Settings", width=110, height=36,
            font=ctk.CTkFont(F, 13, "bold"), corner_radius=8,
            fg_color="#e8f4f4", hover_color="#d0eded",
            text_color=PETROL, border_width=1, border_color=PETROL,
            command=self._open_settings
        ).pack(side="left", padx=(16, 0))

        # ── subtitle row ─────────────────────────────────────────────────
        sub = ctk.CTkFrame(hdr, fg_color="transparent")
        sub.pack(fill="x", padx=28, pady=(2, 12))
        ctk.CTkLabel(sub, text="RF695R  —  RFID Tag Logger",
                     font=ctk.CTkFont(F, 13),
                     text_color=MUTED).pack(side="left")

        # ── petrol accent line ────────────────────────────────────────────
        ctk.CTkFrame(hdr, fg_color=PETROL, corner_radius=0, height=3
                      ).pack(fill="x", side="bottom")

    # ══════════════════════════════════════════════════════════════════════
    #  KPI CARDS  (4 small cards in a row)
    # ══════════════════════════════════════════════════════════════════════
    def _mk_kpis(self):
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=1, column=0, sticky="ew", padx=24, pady=(14, 0))
        for i in range(5):
            row.columnconfigure(i, weight=(1 if i < 4 else 0))

        self._kpi_conn    = self._kpi_card(row, 0, "Connection", "Disconnected",
                                            RED, RED_BG)
        self._kpi_sensor  = self._kpi_card(row, 1, "DI Sensor", "OFF",
                                            MUTED, BG)
        self._kpi_session = self._kpi_card(row, 2, "Session", "—",
                                            TXT2, CARD)
        self._kpi_tags    = self._kpi_card(row, 3, "Tags", "0",
                                            PETROL, CARD)

        # connect / stop buttons
        bf = ctk.CTkFrame(row, fg_color="transparent", width=130)
        bf.grid(row=0, column=4, sticky="ns", padx=(10, 0))
        bf.grid_propagate(False)
        self._b_go = ctk.CTkButton(
            bf, text="▶  Connect", font=ctk.CTkFont(F, 12, "bold"),
            fg_color=PETROL, hover_color=PETROL_H, text_color=WHITE,
            corner_radius=8, height=36, command=self._on_go)
        self._b_go.pack(fill="x", pady=(0, 5))
        self._b_st = ctk.CTkButton(
            bf, text="■  Stop", font=ctk.CTkFont(F, 12, "bold"),
            fg_color="#e5e7eb", hover_color="#d1d5db", text_color=TXT2,
            corner_radius=8, height=36, state="disabled",
            command=self._on_st)
        self._b_st.pack(fill="x")

    def _kpi_card(self, parent, col, title, value, fg, bg):
        """Create a small KPI card and return (dot_lbl, val_lbl, frame)."""
        c = ctk.CTkFrame(parent, fg_color=bg, corner_radius=12,
                         border_width=1, border_color=BORDER, height=80)
        c.grid(row=0, column=col, sticky="ew", padx=(0, 8))
        c.grid_propagate(False)

        inner = ctk.CTkFrame(c, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(inner, text=title.upper(),
                     font=ctk.CTkFont(F, 10), text_color=MUTED
                     ).pack(anchor="w")
        val_lbl = ctk.CTkLabel(inner, text=value,
                               font=ctk.CTkFont(F, 17, "bold"),
                               text_color=fg)
        val_lbl.pack(anchor="w", pady=(2, 0))
        return val_lbl, c

    # ══════════════════════════════════════════════════════════════════════
    #  TAG TABLE
    # ══════════════════════════════════════════════════════════════════════
    def _mk_table(self):
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=10,
                            border_width=1, border_color=BORDER)
        card.grid(row=2, column=0, sticky="nsew", padx=24, pady=(10, 0))

        hdr = ctk.CTkFrame(card, fg_color="transparent")
        hdr.pack(fill="x", padx=20, pady=(14, 0))
        ctk.CTkLabel(hdr, text="Scanned Tags",
                     font=ctk.CTkFont(F, 15, "bold"),
                     text_color=TXT).pack(side="left")
        self._tinfo = ctk.CTkLabel(hdr, text="",
                                   font=ctk.CTkFont(F, 13, "bold"),
                                   text_color=PETROL)
        self._tinfo.pack(side="right")

        # CSV link
        ctk.CTkButton(
            hdr, text="📂  Open CSV", font=ctk.CTkFont(F, 12, "bold"),
            fg_color="#f0f7f7", hover_color="#e0efef",
            text_color=PETROL, border_width=1, border_color=BORDER,
            corner_radius=6, height=32,
            command=self._csv).pack(side="right", padx=(0, 14))

        wrap = tk.Frame(card, bg=CARD, highlightthickness=0, bd=0)
        wrap.pack(fill="both", expand=True, padx=18, pady=(8, 16))

        s = ttk.Style()
        s.theme_use("clam")
        s.configure("R.Treeview", background=WHITE, foreground=TXT,
                    fieldbackground=WHITE, font=(M, 11), rowheight=36,
                    borderwidth=0, relief="flat")
        s.configure("R.Treeview.Heading", background="#f0f7f7",
                    foreground=PETROL_H, font=(F, 11, "bold"),
                    borderwidth=0, relief="flat", padding=(10, 9))
        s.map("R.Treeview", background=[("selected", PETROL)],
              foreground=[("selected", WHITE)])
        s.layout("R.Treeview",
                 [("R.Treeview.treearea", {"sticky": "nswe"})])

        cols = ("#", "time", "epc", "ant", "rssi")
        self._tv = ttk.Treeview(wrap, columns=cols, show="headings",
                                style="R.Treeview")
        for c, t, w, a, st in [
            ("#",    "#",             55,  "center", False),
            ("time", "Time",          105, "center", False),
            ("epc",  "EPC / Tag ID",  240, "w",      False),
            ("ant",  "Antenna",       130, "center", False),
            ("rssi", "RSSI (dBm)",    130, "center", True),
        ]:
            self._tv.heading(c, text=t, anchor=a)
            self._tv.column(c, width=w, minwidth=w - 10, anchor=a,
                            stretch=st)
        self._tv.tag_configure("a", background="#f5fafa")

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self._tv.yview)
        self._tv.configure(yscrollcommand=sb.set)
        self._tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ══════════════════════════════════════════════════════════════════════
    #  BOTTOM — compact log
    # ══════════════════════════════════════════════════════════════════════
    def _mk_bottom(self):
        self._log_expanded = False          # track expanded state
        self._log_card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=10,
                                       border_width=1, border_color=BORDER)
        self._log_card.grid(row=3, column=0, sticky="nsew", padx=24, pady=(10, 16))
        self.rowconfigure(3, weight=1, minsize=140)

        hdr = ctk.CTkFrame(self._log_card, fg_color="transparent")
        hdr.pack(fill="x", padx=18, pady=(10, 0))
        ctk.CTkLabel(hdr, text="Log Output",
                     font=ctk.CTkFont(F, 13, "bold"),
                     text_color=TXT).pack(side="left")

        # expand/collapse toggle
        self._log_toggle = ctk.CTkButton(
            hdr, text="▲  Expand", width=100, height=28,
            font=ctk.CTkFont(F, 11), corner_radius=6,
            fg_color="#f0f1f3", hover_color="#e5e7eb",
            text_color=TXT2, border_width=1, border_color=BORDER,
            command=self._toggle_log)
        self._log_toggle.pack(side="right")

        self._logw = tk.Text(
            self._log_card, height=8, bg="#fafafc", fg=TXT2,
            font=(M, 11), wrap="word", bd=0, highlightthickness=0,
            insertbackground=TXT2, selectbackground=PETROL,
            selectforeground=WHITE, state="disabled", padx=12, pady=6)
        self._logw.pack(fill="both", expand=True, padx=18, pady=(6, 14))
        for tag, fg in [("INFO", TXT2), ("WARNING", AMBER),
                        ("ERROR", RED), ("SESSION", PETROL), ("TAG", GREEN)]:
            self._logw.tag_configure(tag, foreground=fg)

    def _toggle_log(self):
        """Expand or collapse the log panel."""
        if self._log_expanded:
            # collapse: give table most weight back
            self.rowconfigure(2, weight=3)
            self.rowconfigure(3, weight=1, minsize=140)
            self._log_toggle.configure(text="▲  Expand")
        else:
            # expand: log takes most of the window
            self.rowconfigure(2, weight=0)
            self.rowconfigure(3, weight=10, minsize=400)
            self._log_toggle.configure(text="▼  Collapse")
        self._log_expanded = not self._log_expanded

    # ══════════════════════════════════════════════════════════════════════
    #  LOGGING
    # ══════════════════════════════════════════════════════════════════════
    def _mk_loghandler(self):
        h = _QH(self._lq)
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                         datefmt="%H:%M:%S"))
        logging.getLogger("rfid_opcua").addHandler(h)

    def _drain(self):
        batch: list[tuple[str, str]] = []
        for _ in range(80):
            try:
                batch.append(self._lq.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return
        self._logw.configure(state="normal")
        for t, tg in batch:
            self._logw.insert("end", t + "\n", tg)
        self._logw.see("end")
        n = int(self._logw.index("end-1c").split(".")[0])
        if n > 500:
            self._logw.delete("1.0", f"{n - 500}.0")
        self._logw.configure(state="disabled")

    # ══════════════════════════════════════════════════════════════════════
    #  POLL  (60 ms)
    # ══════════════════════════════════════════════════════════════════════
    def _poll(self):
        ses = self._ses
        # connection KPI
        c = self._conn
        if c != self._gc:
            self._gc = c
            vl, fr = self._kpi_conn
            if c:
                vl.configure(text="Connected", text_color=GREEN)
                fr.configure(fg_color=GREEN_BG)
            else:
                vl.configure(text="Disconnected", text_color=RED)
                fr.configure(fg_color=RED_BG)

        # sensor KPI
        d = self._di
        if d != self._gd:
            self._gd = d
            vl, fr = self._kpi_sensor
            if d:
                vl.configure(text="ON", text_color=GREEN)
                fr.configure(fg_color=GREEN_BG)
            else:
                vl.configure(text="OFF", text_color=MUTED)
                fr.configure(fg_color=BG)

        # session
        act, sid = ses.active, ses.sid
        if act and (not self._ga or sid != self._gs):
            self._ga, self._gs, self._gn = True, sid, 0
            self._seen.clear(); self._ids.clear(); self._fix.clear()
            self._tv.delete(*self._tv.get_children())
            vl, fr = self._kpi_session
            vl.configure(text=sid, text_color=AMBER)
            fr.configure(fg_color=AMBER_BG)
            self._kpi_tags[0].configure(text="0")
            self._tinfo.configure(text="")
        elif not act and self._ga:
            self._ga = False
            vl, fr = self._kpi_session
            t = f"{self._gs}  ✓" if self._gs else "—"
            vl.configure(text=t, text_color=TXT2)
            fr.configure(fg_color=CARD)

        # tags
        if act:
            snap = dict(ses.tags)
            for epc, ent in snap.items():
                if epc in self._seen:
                    continue
                self._seen.add(epc)
                self._gn += 1
                _, a, r = ent[-1]
                ad = a if a != "?" else "—"
                rd = r if r != "?" else "—"
                idx = self._gn
                iid = self._tv.insert(
                    "", "end",
                    values=(idx, datetime.now().strftime("%H:%M:%S"),
                            epc, ad, rd),
                    tags=("a",) if idx % 2 == 0 else ())
                self._ids[epc] = iid
                self._tv.yview_moveto(1.0)
                self._kpi_tags[0].configure(text=str(idx))
                self._tinfo.configure(text=f"\u25cf  {idx} unique tag{'s' if idx != 1 else ''}")
                if a == "?" or r == "?":
                    self._fix.add(epc)

        # patch ant/rssi
        if self._fix:
            done = set()
            snap = dict(ses.tags)
            for epc in self._fix:
                ent = snap.get(epc)
                if not ent:
                    continue
                _, a, r = ent[-1]
                if a == "?" and r == "?":
                    continue
                iid = self._ids.get(epc)
                if not iid:
                    continue
                v = list(self._tv.item(iid, "values"))
                ch = False
                if a != "?" and v[3] == "—":
                    v[3] = a; ch = True
                if r != "?" and v[4] == "—":
                    v[4] = r; ch = True
                if ch:
                    self._tv.item(iid, values=v)
                if a != "?" and r != "?":
                    done.add(epc)
            self._fix -= done

        self._drain()
        self.after(TICK, self._poll)

    # ══════════════════════════════════════════════════════════════════════
    #  ACTIONS
    # ══════════════════════════════════════════════════════════════════════
    def _open_settings(self):
        SettingsDialog(self)

    def _csv(self):
        p = csv_path()
        if os.path.exists(p):
            if sys.platform == "win32":
                try:
                    subprocess.Popen(["cmd", "/c", "start", "excel", p])
                except Exception:
                    os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-a", "Microsoft Excel", p])
            else:
                subprocess.Popen(["xdg-open", p])

    def _on_go(self):
        self._stop.clear()
        self._conn = False; self._di = False
        self._b_go.configure(state="disabled")
        self._b_st.configure(state="normal",
                             fg_color=RED, hover_color="#b91c1c",
                             text_color=WHITE)
        self._thr = threading.Thread(target=self._bg, daemon=True)
        self._thr.start()

    def _on_st(self):
        self._stop.set()
        self._conn = False; self._di = False
        self._b_st.configure(state="disabled",
                             fg_color="#e5e7eb", text_color=TXT2)
        self._b_go.configure(state="normal")

    def _close(self):
        self._stop.set(); self._conn = False
        self.after(250, self.destroy)

    # ══════════════════════════════════════════════════════════════════════
    #  BACKGROUND THREAD
    # ══════════════════════════════════════════════════════════════════════
    def _bg(self):
        if not self._log_ok:
            setup_logging(); self._log_ok = True
        os.makedirs(_cfg.OUTPUT_DIR, exist_ok=True)
        ensure_csv()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._amain())
        except Exception as e:
            if not self._stop.is_set():
                log.error("Loop error: %s", e)
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self._conn = False; self._di = False

    async def _amain(self):
        from asyncua import Client
        ses = self._ses
        while not self._stop.is_set():
            if ses.active:
                try:
                    ses.stop()
                except Exception:
                    ses.active = False; ses.tags = {}
            cl = Client(_cfg.OPCUA_URL)
            if _cfg.OPCUA_USER:
                cl.set_user(_cfg.OPCUA_USER)
                cl.set_password(_cfg.OPCUA_PASS)
            scan_sub = di_sub = None
            try:
                async with cl:
                    self._conn = True
                    log.info("Connected to %s", _cfg.OPCUA_URL)
                    try:
                        await cl.load_data_type_definitions()
                    except Exception:
                        pass
                    if _cfg.DEBUG_BROWSE:
                        await browse_tree(cl)
                    rp, sa, ss, sst, din, sn = await find_nodes(
                        cl, _cfg.READ_POINT)
                    await stop_scanning(rp, sst, sa)
                    await asyncio.sleep(0.15)
                    scan_sub, eh = await setup_scan_event_subscription(
                        cl, rp, ses, _cfg.EVENT_PUBLISH_INTERVAL,
                        scan_nodes=sn)
                    if din is None:
                        log.error("DI%d not found", _cfg.DI_CHANNEL)
                        return
                    try:
                        raw = await din.read_value()
                        prev = bool((int(raw) >> _cfg.DI_CHANNEL) & 1)
                    except Exception:
                        prev = False
                    self._di = prev
                    log.info("Ready — sensor DI%d", _cfg.DI_CHANNEL)

                    async def _edge(v: bool):
                        self._di = v
                        if v:
                            ses.start(trigger="DI")
                            if eh: eh.reset_watchdog()
                            await start_scanning(rp, ss, sa)
                        else:
                            await stop_scanning(rp, sst, sa)
                            # Let pending supplement tasks finish before
                            # flushing the session to CSV
                            await asyncio.sleep(0.3)
                            ses.stop()

                    dih = DIHandler(initial_val=prev, on_edge=_edge)
                    di_sub = await cl.create_subscription(
                        _cfg.DI_SAMPLE_MS, dih)
                    await di_sub.subscribe_data_change(din)

                    while not self._stop.is_set():
                        if (_cfg.WATCHDOG_TIMEOUT > 0 and eh
                                and ses.active and eh.watchdog_armed):
                            sl = time.monotonic() - eh.last_event_time
                            if sl > _cfg.WATCHDOG_TIMEOUT:
                                raise RuntimeError(
                                    f"Watchdog: {sl:.0f}s silence")
                        await asyncio.sleep(
                            _cfg.EVENT_PUBLISH_INTERVAL / 1000)

                    for sub in (scan_sub, di_sub):
                        if sub:
                            try: await sub.delete()
                            except Exception: pass
                    await stop_scanning(rp, sst, sa)
                    if ses.active:
                        await asyncio.sleep(0.3)
                        ses.stop()
                    self._di = False; return

            except Exception as e:
                if ses.active:
                    try: ses.stop()
                    except Exception: ses.active = False; ses.tags = {}
                self._conn = False; self._di = False
                if self._stop.is_set(): return
                log.error("%s", e)
                log.info("Reconnecting in %ds …", _cfg.RETRY_DELAY)
                for _ in range(int(_cfg.RETRY_DELAY / 0.5)):
                    if self._stop.is_set(): return
                    await asyncio.sleep(0.5)


# ═══════════════════════════════════════════════════════════════════════════════
#  Settings Dialog
# ═══════════════════════════════════════════════════════════════════════════════

class SettingsDialog(ctk.CTkToplevel):
    """Modal settings window — edits rfid_opcua.config at runtime."""

    _FIELDS = [
        # (label, config attr, type, description)
        ("OPC UA Server URL",     "OPCUA_URL",              str,   "e.g. opc.tcp://192.168.0.254:4840"),
        ("OPC UA User",           "OPCUA_USER",             str,   "leave empty for anonymous"),
        ("OPC UA Password",       "OPCUA_PASS",             str,   "leave empty for anonymous"),
        ("Read Point",            "READ_POINT",             int,   "1–4"),
        ("DI Channel",            "DI_CHANNEL",             int,   "0-based digital input index"),
        ("DI Debounce (s)",       "DI_DEBOUNCE_S",          float, "re-trigger lockout"),
        ("DI Stop Delay (s)",     "DI_STOP_DELAY_S",        float, "falling-edge hold-off"),
        ("Event Publish (ms)",    "EVENT_PUBLISH_INTERVAL",  int,   "OPC UA subscription interval"),
        ("DI Sample (ms)",        "DI_SAMPLE_MS",           int,   "OPC UA DI sampling interval"),
        ("Watchdog Timeout (s)",  "WATCHDOG_TIMEOUT",       int,   "0 = disabled"),
        ("Reconnect Delay (s)",   "RETRY_DELAY",            int,   "seconds before retry"),
        ("CSV Filename",          "CSV_FILENAME",           str,   "output file name"),
    ]

    def __init__(self, parent: App):
        super().__init__(parent)
        self.title("Settings")
        self.configure(fg_color=BG)
        self.geometry("520x620")
        self.minsize(460, 500)
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        self._parent = parent
        self._entries: dict[str, ctk.CTkEntry] = {}

        # header
        ctk.CTkLabel(self, text="Settings",
                     font=ctk.CTkFont(F, 18, "bold"),
                     text_color=TXT).pack(padx=24, pady=(18, 4), anchor="w")
        ctk.CTkLabel(self, text="Changes take effect on next Connect.",
                     font=ctk.CTkFont(F, 11),
                     text_color=MUTED).pack(padx=24, anchor="w")

        # scrollable form
        form = ctk.CTkScrollableFrame(self, fg_color=CARD, corner_radius=10,
                                       border_width=1, border_color=BORDER)
        form.pack(fill="both", expand=True, padx=24, pady=(12, 0))

        for label, attr, typ, desc in self._FIELDS:
            row = ctk.CTkFrame(form, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=(10, 0))

            ctk.CTkLabel(row, text=label,
                         font=ctk.CTkFont(F, 12, "bold"),
                         text_color=TXT).pack(anchor="w")
            ctk.CTkLabel(row, text=desc,
                         font=ctk.CTkFont(F, 10),
                         text_color=MUTED).pack(anchor="w")

            entry = ctk.CTkEntry(row, font=ctk.CTkFont(M, 12),
                                 height=34, corner_radius=6,
                                 border_width=1, border_color=BORDER,
                                 fg_color=WHITE, text_color=TXT)
            entry.pack(fill="x", pady=(4, 0))
            current = getattr(_cfg, attr, "")
            entry.insert(0, str(current))
            if attr == "OPCUA_PASS" and current:
                entry.configure(show="•")
            self._entries[attr] = entry

        # spacer at bottom of form
        ctk.CTkFrame(form, fg_color="transparent", height=8).pack()

        # buttons
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(fill="x", padx=24, pady=(12, 18))

        ctk.CTkButton(
            bf, text="Save", font=ctk.CTkFont(F, 13, "bold"),
            fg_color=PETROL, hover_color=PETROL_H, text_color=WHITE,
            corner_radius=8, height=38, width=100,
            command=self._save
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            bf, text="Cancel", font=ctk.CTkFont(F, 13),
            fg_color="#e5e7eb", hover_color="#d1d5db", text_color=TXT2,
            corner_radius=8, height=38, width=100,
            command=self.destroy
        ).pack(side="right")

    def _save(self):
        """Write values back to rfid_opcua.config module attributes."""
        for label, attr, typ, desc in self._FIELDS:
            raw = self._entries[attr].get().strip()
            try:
                val = typ(raw)
            except (ValueError, TypeError):
                continue
            setattr(_cfg, attr, val)

        # Feedback
        log.info("Settings updated — reconnect to apply.")
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════════
class _QH(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__(); self._q = q
    def emit(self, record):
        try:
            m = self.format(record); t = record.levelname
            if t not in ("WARNING", "ERROR"):
                if ">>>" in m or "<<<" in m or "Session" in m: t = "SESSION"
                elif "+" in m[:20]: t = "TAG"
                else: t = "INFO"
            self._q.put_nowait((m, t))
        except Exception: pass

if __name__ == "__main__":
    App().mainloop()
