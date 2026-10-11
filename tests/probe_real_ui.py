"""用真实 streamlit 前端协议连服务器，看页面到底渲染了什么。

为什么值得写这个：AppTest 有很多失真（模块缓存、回调状态回滚、session_state 是假的），
前面十几轮排查都被它误导过。这个脚本直接说 streamlit 的 protobuf 协议
（用 streamlit 自己的 proto 模块编解码），所以看到的就是**真实用户看到的**。

用法：
    # 1) 先启动服务器（另开一个终端）
    .venv\\Scripts\\python.exe -m streamlit run AI_partner.py --server.port=8599 --server.headless=true
    # 2) 再跑本脚本
    python tests/probe_real_ui.py
"""
import base64
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from streamlit.proto.BackMsg_pb2 import BackMsg  # noqa: E402
from streamlit.proto.ForwardMsg_pb2 import ForwardMsg  # noqa: E402

HOST, PORT = "127.0.0.1", 8599
PATH = "/_stcore/stream"


def handshake():
    sock = socket.create_connection((HOST, PORT), timeout=30)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            f"GET {PATH} HTTP/1.1\r\n"
            f"Host: {HOST}:{PORT}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: streamlit\r\n"
            "\r\n"
        ).encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += sock.recv(4096)
    head = buf.split(b"\r\n\r\n")[0]
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError(f"握手失败: {head[:200]!r}")
    return sock


def send_frame(sock, payload: bytes):
    """streamlit 用**二进制**帧。"""
    header = bytearray([0x82])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def read_frames(sock, seconds=10.0):
    sock.settimeout(1.0)
    deadline = time.time() + seconds
    buf = b""
    frames = []
    while time.time() < deadline:
        try:
            chunk = sock.recv(1 << 16)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while len(buf) >= 2:
            b0, b1 = buf[0], buf[1]
            length = b1 & 0x7F
            offset = 2
            if length == 126:
                if len(buf) < 4:
                    break
                length = struct.unpack(">H", buf[2:4])[0]
                offset = 4
            elif length == 127:
                if len(buf) < 10:
                    break
                length = struct.unpack(">Q", buf[2:10])[0]
                offset = 10
            if len(buf) < offset + length:
                break
            payload = buf[offset:offset + length]
            buf = buf[offset + length:]
            if b0 & 0x0F == 0x2:
                frames.append(payload)
    return frames


def describe(msg: ForwardMsg) -> str:
    """把一条 forward message 说成人话。"""
    kind = msg.WhichOneof("type")
    if kind == "new_session":
        return "new_session"
    if kind == "delta":
        delta = msg.delta
        dkind = delta.WhichOneof("type")
        if dkind == "new_element":
            el = delta.new_element
            ekind = el.WhichOneof("type")
            extra = ""
            if ekind == "exception":
                extra = f" EXCEPTION: {el.exception.message[:300]}"
            elif ekind == "markdown":
                extra = f" md={el.markdown.body[:60]!r}"
            elif ekind == "heading":
                extra = f" h={el.heading.body[:40]!r}"
            return f"new_element:{ekind}{extra}"
        if dkind == "add_block":
            return f"add_block:{delta.add_block.WhichOneof('type')}"
        return f"delta:{dkind}"
    if kind == "session_status_changed":
        return "session_status_changed"
    return str(kind)


def run_once(sock, label):
    msg = BackMsg()
    msg.rerun_script.query_string = ""
    msg.rerun_script.page_script_hash = ""
    msg.rerun_script.page_name = ""
    send_frame(sock, msg.SerializeToString())
    frames = read_frames(sock, seconds=12)
    print(f"\n===== {label}：收到 {len(frames)} 帧 =====")
    counts = {}
    exceptions = []
    for raw in frames:
        fwd = ForwardMsg()
        try:
            fwd.ParseFromString(raw)
        except Exception as exc:  # noqa: BLE001
            print("  （无法解析的帧）", type(exc).__name__)
            continue
        text = describe(fwd)
        key = text.split(" EXCEPTION")[0]
        counts[key] = counts.get(key, 0) + 1
        if "EXCEPTION" in text:
            exceptions.append(text)
    for key, n in sorted(counts.items(), key=lambda kv: -kv[1])[:18]:
        print(f"  {n:3d} × {key}")
    if exceptions:
        print("  ⚠ 页面里有异常：")
        for item in exceptions[:3]:
            print("   ", item)
    else:
        print("  ✅ 没有异常")
    return counts, exceptions


def main():
    sock = handshake()
    print(f"已连上 ws://{HOST}:{PORT}{PATH}")
    run_once(sock, "第 1 次渲染")
    run_once(sock, "第 2 次渲染（模拟一次交互后的重跑）")
    run_once(sock, "第 3 次渲染")
    sock.close()


if __name__ == "__main__":
    sys.exit(main())
