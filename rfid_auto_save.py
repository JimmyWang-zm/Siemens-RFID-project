# RFID 标签自动记录程序
# 设备：西门子 SIMATIC RF695R + RF662A 天线
#
# 工作流程：
#   1. 程序启动 → 连接读写器 → 自动发送"开始连续扫描"命令
#   2. 检测到第一个标签 → 进入"扫描中"状态（认为小车到达）
#   3. 连续 IDLE_TIMEOUT 秒没有收到任何标签 → 认为小车已离开
#      → 打印本次会话汇总，写入每日 CSV → 等待下一辆车
#
# 启动命令说明：
#   RF695R 的 TCP XML 通道需要先收到命令才开始推送数据。
#   本脚本会依次尝试已知命令格式；如果均无效，请用浏览器打开
#   http://192.168.0.254，在 Tag Monitor 页面按 F12 查看
#   "Continuous Acquisition" 按钮触发的 HTTP 请求，然后把
#   URL 填入下方 HTTP_START_URL 配置项。
#
# 运行方式：python rfid_auto_save.py
# 停止方式：Ctrl+C

import socket
import csv
import os
import xml.etree.ElementTree as ET
from datetime import datetime
import time
import urllib.request
import urllib.error

# ===================== 配置项 =====================
RFID_IP      = "192.168.0.254"
RFID_PORT    = 10001
OUTPUT_DIR   = r"C:\rfid_logger\records"

# 小车离开判断：连续多少秒没有收到任何标签
IDLE_TIMEOUT = 5.0

# 每次 recv() 等待时长（用于检测空闲）
RECV_TIMEOUT = 1.0

# 断线重连等待
RETRY_DELAY  = 5

# 若已找到 HTTP 接口（见顶部说明），填入完整 URL，否则留空
# 示例：HTTP_START_URL = "http://192.168.0.254/diagrams/tagmonitor/start"
HTTP_START_URL = ""

# RF695R XML 启动命令（依次尝试，直到成功）
XML_START_COMMANDS = [
    b"<AutoRead_Req/>\r\n",
    b'<AutoRead_Req xmlns="com.siemens.industry.rfid.reader"/>\r\n',
    b"<StartInventory/>\r\n",
    b"<Cmd>StartInventory</Cmd>\r\n",
]
# ==================================================


def ensure_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def get_daily_filepath():
    date_str = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(OUTPUT_DIR, f"RFID_{date_str}.csv")


def write_header_if_new(filepath):
    if not os.path.exists(filepath):
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["时间戳", "EPC/标签编号", "天线", "RSSI(dB)", "事件类型", "会话ID"]
            )


# ---------- 启动命令 ----------

def try_http_start():
    """通过 HTTP 接口触发读写器开始扫描（需先填好 HTTP_START_URL）"""
    if not HTTP_START_URL:
        return False
    try:
        resp = urllib.request.urlopen(HTTP_START_URL, timeout=3)
        print(f"  [HTTP] 启动成功：{HTTP_START_URL} (status {resp.status})")
        return True
    except urllib.error.HTTPError as e:
        if e.code in (200, 204):
            print(f"  [HTTP] 启动成功：{HTTP_START_URL}")
            return True
        print(f"  [HTTP] 返回 {e.code}：{HTTP_START_URL}")
        return False
    except Exception as e:
        print(f"  [HTTP] 失败：{e}")
        return False


def try_xml_start(sock):
    """向读写器发送 XML 启动命令"""
    for cmd in XML_START_COMMANDS:
        try:
            sock.send(cmd)
            sock.settimeout(0.8)
            try:
                resp = sock.recv(512)
                if resp:
                    print(f"  [XML] 命令已接受，响应：{resp[:120]}")
                    return True
            except socket.timeout:
                pass  # 部分固件不回应命令，静默继续
        except OSError:
            return False
    print("  [XML] 启动命令已发送（无响应，继续等待数据...）")
    return True


def send_start(sock):
    print("[START] 发送启动扫描命令...")
    http_ok = try_http_start()
    xml_ok  = try_xml_start(sock)
    if not http_ok and not xml_ok:
        print("[WARN]  未能确认启动命令成功，仍会等待读写器推送数据。")
        print("[WARN]  若长时间无数据，请参考脚本顶部说明配置 HTTP_START_URL。")


# ---------- 数据解析 ----------

def parse_tags(xml_data):
    """从 XML 报文中提取标签，返回 [(epc, antenna, rssi, event_type), ...]"""
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


# ---------- 会话保存 ----------

def save_session(session_tags, session_id, session_start):
    """将本次小车过站的所有标签写入 CSV，并打印汇总"""
    if not session_tags:
        print("[INFO] 本次会话未检测到任何标签，跳过保存。\n")
        return

    filepath = get_daily_filepath()
    write_header_if_new(filepath)
    duration = time.time() - session_start

    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for epc, records in session_tags.items():
            # 每个 EPC 只写最后一条（可改为全部写入）
            ts, antenna, rssi, etype = records[-1]
            writer.writerow([ts, epc, antenna, rssi, etype, session_id])

    print("\n" + "=" * 55)
    print(f"  会话结束：{session_id}")
    print(f"  持续时间：{duration:.1f} 秒")
    print(f"  识别标签：{len(session_tags)} 个")
    for epc in session_tags:
        _, antenna, rssi, _ = session_tags[epc][-1]
        print(f"    {epc}  Ant:{antenna}  RSSI:{rssi}")
    print(f"  已保存 → {filepath}")
    print("=" * 55 + "\n")


# ---------- 接收流（带空闲心跳） ----------

def receive_stream(sock):
    """
    生成器：每收到一条完整 XML 就 yield 其文本；
    若本轮 recv 超时（无数据），yield None 用于空闲检测。
    """
    buf = b""
    while True:
        try:
            sock.settimeout(RECV_TIMEOUT)
            chunk = sock.recv(4096)
            if not chunk:
                break           # 连接正常关闭
            buf += chunk
            text = buf.decode("utf-8", errors="replace")
            # RF695R 每条报文以 ">" 结尾
            if text.rstrip().endswith(">"):
                yield text.strip()
                buf = b""
        except socket.timeout:
            if buf:             # 缓冲区有残片，一并 yield
                yield buf.decode("utf-8", errors="replace").strip()
                buf = b""
            yield None          # 心跳：本轮无数据
        except OSError:
            break               # 连接断开，退出生成器


# ---------- 主循环 ----------

def main():
    ensure_output_dir()
    print("=" * 55)
    print("  RFID 自动过站记录程序")
    print(f"  读写器：{RFID_IP}:{RFID_PORT}")
    print(f"  输出目录：{OUTPUT_DIR}")
    print(f"  空闲超时：{IDLE_TIMEOUT} 秒（无标签即认为小车已离开）")
    print("  Ctrl+C 退出")
    print("=" * 55)

    STATE_IDLE   = "idle"
    STATE_ACTIVE = "active"

    state         = STATE_IDLE
    session_tags  = {}      # {epc: [(ts, antenna, rssi, etype), ...]}
    session_id    = None
    session_start = None
    last_tag_time = None
    session_no    = 0

    try:
        while True:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                print(f"[CONN] 正在连接 {RFID_IP}:{RFID_PORT} ...")
                sock.settimeout(10)
                sock.connect((RFID_IP, RFID_PORT))
                print("[CONN] 已连接。")
                send_start(sock)
                print(f"[WAIT] 等待小车到来...\n")

                for xml_data in receive_stream(sock):

                    # ── 无数据心跳 ──────────────────────────────
                    if xml_data is None:
                        if state == STATE_ACTIVE:
                            idle = time.time() - last_tag_time
                            remaining = IDLE_TIMEOUT - idle
                            if remaining <= 0:
                                # 超时 → 小车已离开
                                save_session(session_tags, session_id, session_start)
                                state         = STATE_IDLE
                                session_tags  = {}
                                session_id    = None
                                session_start = None
                                print("[WAIT] 等待下一辆小车...\n")
                                # 重新发送启动命令（某些固件需要重触发）
                                try_xml_start(sock)
                            else:
                                print(f"  [IDLE] 等待标签消失确认... "
                                      f"({remaining:.1f}s 后保存)", end="\r")
                        continue

                    # ── 收到 XML ────────────────────────────────
                    tags = parse_tags(xml_data)
                    if not tags:
                        print(f"  [DEBUG] 收到报文但无标签字段：{xml_data[:200]}")
                        continue

                    now     = time.time()
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    # 第一个标签 → 小车到来，开启新会话
                    if state == STATE_IDLE:
                        session_no   += 1
                        session_id    = (f"S{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                                        f"_{session_no:03d}")
                        session_start = now
                        state         = STATE_ACTIVE
                        print(f"[DETECT] ★ 小车到来！开始会话 {session_id}")

                    last_tag_time = now

                    for epc, antenna, rssi, etype in tags:
                        if epc not in session_tags:
                            session_tags[epc] = []
                            print(f"  [NEW TAG] {epc}  Ant:{antenna}  RSSI:{rssi}")
                        else:
                            print(f"  [TAG]     {epc}  Ant:{antenna}  RSSI:{rssi}", end="\r")
                        session_tags[epc].append((now_str, antenna, rssi, etype))

            except ConnectionRefusedError:
                print(f"[ERR] 连接被拒绝，{RETRY_DELAY}s 后重试...")
                time.sleep(RETRY_DELAY)
            except (socket.timeout, socket.error) as e:
                print(f"[ERR] 连接异常：{e}，{RETRY_DELAY}s 后重连...")
                time.sleep(RETRY_DELAY)
            finally:
                sock.close()

    except KeyboardInterrupt:
        # 强制保存当前未完成的会话
        if state == STATE_ACTIVE and session_tags:
            print("\n[Ctrl+C] 正在保存未完成会话...")
            save_session(session_tags, session_id, session_start)
        print(f"[STOP] 已停止，记录位于：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
