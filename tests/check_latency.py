"""测 MongoDB 的真实往返延迟：决定"慢"是延迟问题还是请求数量问题。

用法：
    python tests/check_latency.py "mongodb+srv://用户:密码@集群.mongodb.net/?appName=x"
    python tests/check_latency.py            # 读本地 .streamlit/secrets.toml

它会分别测量：
  ① 建连（含 DNS + TLS + 选主）
  ② 单次小读写往返
  ③ 连续 20 次读的平均延迟
并给出"一次渲染 N 次请求大约要等多久"的估算，以及优化方向。
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent))


def mask(uri: str) -> str:
    import re

    return re.sub(r"://([^:/@]+):([^@/]+)@", r"://\1:***@", uri or "")


def read_uri_from_secrets() -> str:
    path = TESTS_DIR.parent / ".streamlit" / "secrets.toml"
    if not path.exists():
        return ""
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"✗ secrets.toml 解析失败：{type(exc).__name__}: {exc}")
        return ""
    return str(data.get("MONGO_URI") or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="测 MongoDB 往返延迟")
    parser.add_argument("uri", nargs="?", default="")
    args = parser.parse_args()

    uri = args.uri.strip() or read_uri_from_secrets()
    if not uri:
        print('需要连接串：python tests/check_latency.py "mongodb+srv://..."')
        return 2

    import pymongo

    print(f"连接串：{mask(uri)}")
    print(f"pymongo：{pymongo.version}\n")

    # ① 建连
    t0 = time.perf_counter()
    try:
        client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=15000)
        client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 连不上：{type(exc).__name__}: {str(exc)[:160]}")
        return 1
    connect_ms = (time.perf_counter() - t0) * 1000
    print(f"① 建连（DNS + TLS + 选主 + ping）：{connect_ms:.0f} ms")

    collection = client["lingheng"]["files"]

    # ② 单次小读
    t0 = time.perf_counter()
    collection.find_one({"uid": "latency-probe"})
    one_ms = (time.perf_counter() - t0) * 1000
    print(f"② 单次小查询：{one_ms:.0f} ms")

    # ③ 连续 20 次读
    samples = []
    for _ in range(20):
        t0 = time.perf_counter()
        collection.find_one({"uid": "latency-probe"})
        samples.append((time.perf_counter() - t0) * 1000)
    avg = statistics.mean(samples)
    print(f"③ 连续 20 次读：平均 {avg:.0f} ms，最快 {min(samples):.0f} ms，最慢 {max(samples):.0f} ms")

    # ④ 结论
    print("\n结论：")
    if avg < 40:
        grade = "很好（数据库离应用很近）"
    elif avg < 120:
        grade = "还可以"
    elif avg < 300:
        grade = "偏慢 —— 跨区域访问的典型值"
    else:
        grade = "很慢 —— 强烈建议换区域"
    print(f"  单次往返 {avg:.0f} ms → {grade}")

    for requests in (2, 10, 22, 40):
        print(f"  若一次渲染发起 {requests:2d} 次请求 → 光网络就要等 {avg * requests / 1000:.1f} 秒")

    print("\n优化方向（按性价比排序）：")
    print("  1. 把 Atlas 集群放到离 Streamlit Cloud 更近的区域（通常是 us-east-1），")
    print("     跨太平洋访问动辄 200~300ms，换区域能降到 30~80ms，改配置即可")
    print("  2. 减少请求数量（本项目已做过：32 次 → 16 次；列表页 2 次）")
    print("  3. 会话数据放内存、异步落库（代码改动较大，且要处理容器被回收的情况）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
