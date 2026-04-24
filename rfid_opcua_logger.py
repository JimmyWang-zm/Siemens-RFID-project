"""
rfid_opcua_logger.py
====================
Siemens SIMATIC RF695R — OPC UA 自动过站记录程序

功能 1：监测 OPC UA Presence 变量，小车到来时自动调用 ScanStart，
        小车离开时自动调用 ScanStop（参见手册 Section 3.1.2 / 3.3.3）。

功能 2：通过 RfidScanEventType 事件接收标签，会话结束后写入每日 CSV。

前置条件（WBM 配置，路径：Settings > Communication > OPC UA）：
  1. 启用 OPC UA，选择 "Main application" 模式
  2. 勾选 "Presence events"（使 Presence 变量生效）
  3. 勾选 "Last access events"（可选，用于辅助诊断）
  OPC UA 端口默认 4840；设备 IP 默认 192.168.0.254。

运行方式：python rfid_opcua_logger.py
停止方式：Ctrl+C
依  赖：pip install asyncua
"""

import asyncio
import csv
import os
import time
from datetime import datetime

try:
    from asyncua import Client, ua
except ImportError:
    raise SystemExit(
        "[ERR] 缺少依赖，请先运行：pip install asyncua"
    )

# ===================== 配置项 =====================
OPCUA_URL     = "opc.tcp://192.168.0.254:4840"
OPCUA_USER    = ""        # 留空 = 匿名登录（需在 WBM OPC UA Security 中启用 Anonymous）
OPCUA_PASS    = ""
OUTPUT_DIR    = r"C:\rfid_logger\records"
READ_POINT    = 1         # 读点编号（1 起，RF695R 最多 4 个）
POLL_INTERVAL = 0.5       # Presence 轮询间隔（秒）
RETRY_DELAY   = 5         # 断线重连等待（秒）
# ==================================================


# ──────────────────────── CSV 辅助 ────────────────────────

def _daily_csv() -> str:
    return os.path.join(OUTPUT_DIR, f"RFID_{datetime.now().strftime('%Y-%m-%d')}.csv")


def _ensure_header(path: str):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(
                ["时间戳", "EPC/标签编号", "天线", "RSSI(dBm)", "会话ID"]
            )


def _flush_session(tags: dict, sid: str, t0: float):
    if not tags:
        print("[INFO] 本次会话未检测到任何标签，跳过保存\n")
        return
    path = _daily_csv()
    _ensure_header(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for epc, rows in tags.items():
            ts, ant, rssi = rows[-1]
            w.writerow([ts, epc, ant, rssi, sid])
    dur = time.time() - t0
    print("\n" + "=" * 60)
    print(f"  会话结束 : {sid}  用时 {dur:.1f}s")
    print(f"  识别标签 : {len(tags)} 个")
    for epc, rows in tags.items():
        _, ant, rssi = rows[-1]
        print(f"    {epc}  Ant:{ant}  RSSI:{rssi}")
    print(f"  → {path}")
    print("=" * 60 + "\n")


# ──────────────────────── 会话状态 ────────────────────────

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
        print(f"\n[DETECT] ★ 小车到来！→ 会话 {self.sid}")

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
            print(f"  [NEW TAG] {epc}  Ant:{ant}  RSSI:{rssi}")
        else:
            print(f"  [TAG]     {epc}  Ant:{ant}  RSSI:{rssi}", end="\r")
        self.tags[epc].append((ts, ant, rssi))


# ──────────────────────── EPC 提取 ────────────────────────

def _extract_epc(result) -> str:
    """从 RfidScanResult 中提取 EPC/UID 字符串。"""
    sd = getattr(result, "ScanData", None)
    if sd is None:
        return "N/A"
    # 优先 String 类型（CodeTypes=1）
    if hasattr(sd, "String") and sd.String:
        return str(sd.String)
    # ByteString 类型（CodeTypes=0）
    if hasattr(sd, "ByteString") and sd.ByteString:
        return sd.ByteString.hex().upper()
    # ScanDataEpc 结构体（CodeTypes=2）—— 含 PC 字段 + UId
    if hasattr(sd, "Epc") and sd.Epc:
        epc_struct = sd.Epc
        uid = getattr(epc_struct, "UId", b"")
        return uid.hex().upper() if uid else str(epc_struct)
    return str(sd)


# ──────────────────────── OPC UA 事件处理 ────────────────────────

class _ScanHandler:
    """
    接收 RfidScanEventType 事件，将标签写入当前会话。
    asyncua 1.x 不需要继承 SubHandler，实现对应方法即可。
    回调在内部线程中同步执行，无需 await。
    """

    def __init__(self, session: _Session):
        self._s = session

    def event_notification(self, event):
        try:
            results = getattr(event, "Results", None)
            if not results:
                return
            for r in results:
                epc  = _extract_epc(r)
                ant  = str(getattr(r, "Antenna",  "N/A"))
                rssi = str(getattr(r, "Strength", "N/A"))
                self._s.add_tag(epc, ant, rssi)
        except Exception as exc:
            print(f"[WARN] 事件处理异常: {exc}")

    def datachange_notification(self, node, val, data):
        pass  # Presence 由主循环轮询，此处无需处理


# ──────────────────────── 节点查找 ────────────────────────

async def _find_nodes(client: Client, rp_index: int):
    """
    在 OPC UA 地址空间中查找:
      - 读点节点 (DeviceSet > Read_point_x)
      - Diagnostics > Presence
      - ScanActive 变量
      - ScanStart / ScanStop 方法
    返回 (rp, presence, scan_active, scan_start, scan_stop)，找不到则为 None。
    """
    root    = client.get_root_node()
    objects = await root.get_child(["0:Objects"])

    # DeviceSet 的 namespace index 因设备/固件不同通常为 2 或 3
    device_set = None
    for ns in range(2, 8):
        try:
            device_set = await objects.get_child([f"{ns}:DeviceSet"])
            break
        except Exception:
            continue
    if device_set is None:
        raise RuntimeError(
            "OPC UA 地址空间中未找到 DeviceSet 节点\n"
            "  请确认：① 设备 IP 正确 ② OPC UA 已在 WBM 中启用"
        )

    children   = await device_set.get_children()
    readpoints = [
        c for c in children
        if "read_point" in (await c.read_browse_name()).Name.lower()
        or "readpoint"  in (await c.read_browse_name()).Name.lower()
    ]
    if not readpoints:
        raise RuntimeError("DeviceSet 下未找到任何读点节点")
    if rp_index > len(readpoints):
        raise RuntimeError(
            f"读点 {rp_index} 不存在（共 {len(readpoints)} 个）"
        )

    rp = readpoints[rp_index - 1]
    print(f"[INFO] 读点: {(await rp.read_browse_name()).Name}")

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
                    print("  ✓ Diagnostics > Presence")
        elif bn == "ScanActive":
            scan_active_node = c
            print("  ✓ ScanActive")
        elif bn == "ScanStart":
            scan_start_node = c
            print("  ✓ ScanStart")
        elif bn == "ScanStop":
            scan_stop_node = c
            print("  ✓ ScanStop")

    if presence_node is None:
        print(
            "[WARN] 未找到 Presence 节点\n"
            "  请在 WBM 中启用: Settings > Communication > OPC UA > Presence events\n"
            "  并确认设备运行在 Presence 模式"
        )

    return rp, presence_node, scan_active_node, scan_start_node, scan_stop_node


# ──────────────────────── 扫描控制 ────────────────────────

async def _start_scanning(rp, scan_start_node, scan_active_node):
    """
    启动扫描（手册 3.1.2）：
      1. 优先调用 ScanStart 方法（异步，标签通过 RfidScanEventType 事件推送）
         ScanSettings: Cycles=0, DataAvailable=False, Duration=0 → 无限扫描
      2. ScanStart 失败时，写 ScanActive=True（简单模式，标签通过 LastScanData 变量更新）
    """
    if scan_start_node is not None:
        try:
            # 优先尝试使用已注册的 ScanSettings 类型（需先成功调用 load_data_type_definitions）
            scan_settings_cls = getattr(ua, "ScanSettings", None)
            if scan_settings_cls is not None:
                ss = scan_settings_cls()
                ss.Cycles        = 0
                ss.DataAvailable = False
                ss.Duration      = 0
                await rp.call_method(scan_start_node, ss)
            else:
                # 回退：将三个字段作为独立 Variant 传入
                # 注意：部分固件版本接受此格式，部分不接受
                await rp.call_method(
                    scan_start_node,
                    ua.Variant(0,     ua.VariantType.UInt32),
                    ua.Variant(False, ua.VariantType.Boolean),
                    ua.Variant(0,     ua.VariantType.UInt32),
                )
            print("[CMD] ScanStart ✓")
            return
        except Exception as e:
            print(f"[WARN] ScanStart 调用失败: {e}")
            print("[INFO] 回退到 ScanActive 模式...")

    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(True, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = True ✓")
        except Exception as e:
            print(f"[ERR] ScanActive 写入失败: {e}")
    else:
        print("[WARN] ScanStart 和 ScanActive 节点均未找到，无法控制扫描")


async def _stop_scanning(rp, scan_stop_node, scan_active_node):
    """
    停止扫描（手册 3.1.2）：
      1. 优先调用 ScanStop 方法
      2. 失败时写 ScanActive=False
    """
    if scan_stop_node is not None:
        try:
            await rp.call_method(scan_stop_node)
            print("[CMD] ScanStop ✓")
            return
        except Exception as e:
            print(f"[WARN] ScanStop 调用失败: {e}")

    if scan_active_node is not None:
        try:
            await scan_active_node.write_value(
                ua.DataValue(ua.Variant(False, ua.VariantType.Boolean))
            )
            print("[CMD] ScanActive = False ✓")
        except Exception as e:
            print(f"[ERR] ScanActive 写入失败: {e}")


# ──────────────────────── 主循环 ────────────────────────

async def _run():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    session = _Session()

    print("=" * 60)
    print("  RFID OPC UA 自动过站记录程序")
    print(f"  服务器   : {OPCUA_URL}")
    print(f"  读点编号 : {READ_POINT}")
    print(f"  输出目录 : {OUTPUT_DIR}")
    print("  Ctrl+C 退出")
    print("=" * 60)

    while True:
        client = Client(OPCUA_URL)
        if OPCUA_USER:
            client.set_user(OPCUA_USER)
            client.set_password(OPCUA_PASS)

        try:
            print(f"\n[CONN] 连接 {OPCUA_URL} ...")
            async with client:
                # 加载设备自定义数据类型（ScanSettings / RfidScanResult 等）
                # 成功后 ua.ScanSettings 可用，事件 Results 字段可正确解码
                try:
                    await client.load_data_type_definitions()
                    print("[INFO] 自定义数据类型已加载 ✓")
                except Exception as e:
                    print(f"[WARN] 自定义类型加载失败（将使用泛型解码）: {e}")

                rp, presence_node, scan_active_node, scan_start_node, scan_stop_node = (
                    await _find_nodes(client, READ_POINT)
                )

                # 连接后先停止扫描，清除上次意外退出留下的扫描状态
                print("[INIT] 复位扫描状态...")
                await _stop_scanning(rp, scan_stop_node, scan_active_node)
                await asyncio.sleep(0.5)

                # 订阅扫描事件（RfidScanEventType 继承自 BaseEventType）
                handler = _ScanHandler(session)
                sub = await client.create_subscription(200, handler)
                await sub.subscribe_events(rp)
                print("[SUB] RfidScanEventType 事件已订阅 ✓")

                print("\n[WAIT] 等待小车到来...\n")
                # 读取当前 Presence 作为初始值（避免启动时误触发）
                try:
                    prev_presence = int(await presence_node.read_value()) if presence_node else 0
                except Exception:
                    prev_presence = 0

                while True:
                    if presence_node is None:
                        # Presence 节点不可用，每 10s 提示一次
                        await asyncio.sleep(10)
                        print(
                            "[ERR] Presence 节点不可用，程序无法自动触发扫描\n"
                            "  请在 WBM 中启用: Settings > Communication > OPC UA > Presence events"
                        )
                        continue

                    try:
                        pval = int(await presence_node.read_value())
                    except Exception as e:
                        print(f"[WARN] 读取 Presence 失败: {e}")
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # ── 小车到来：Presence 0 → >0 ──────────────────────
                    if pval > 0 and prev_presence == 0:
                        session.start()
                        await _start_scanning(rp, scan_start_node, scan_active_node)

                    # ── 小车离开：Presence >0 → 0 ──────────────────────
                    elif pval == 0 and prev_presence > 0:
                        print("\n[DETECT] 小车已离开，停止扫描...")
                        await _stop_scanning(rp, scan_stop_node, scan_active_node)
                        session.stop()
                        print("[WAIT] 等待下一辆小车...\n")

                    prev_presence = pval
                    await asyncio.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n[Ctrl+C] 正在停止...")
            # 保存当前未完成的会话（无需重连，直接写文件）
            if session.active:
                session.stop()
            print(f"[STOP] 已停止，记录位于: {OUTPUT_DIR}")
            return

        except Exception as e:
            if session.active:
                session.stop()
            print(f"[ERR] {e}")
            print(f"[RETRY] {RETRY_DELAY}s 后重连...\n")
            await asyncio.sleep(RETRY_DELAY)


def main():
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
