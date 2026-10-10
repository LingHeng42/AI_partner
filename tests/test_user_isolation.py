"""用户隔离：两种存储后端下都验证"看不到、也删不掉别人的会话"。

为什么这条最重要：整套多用户设计的安全边界就在这里。只要有一个调用点漏了
用户维度（或者哪天有人把 uid 从 storage 根前缀挪回业务代码里），就会出现
"甲能看到乙的会话"。所以这里用**存储层函数**（不经过界面）分别验证文件后端与
MongoDB 后端，并且专门测"删除别人的会话"这种破坏性操作。

MongoDB 后端用 mongomock（纯 Python 的内存实现，API 与 pymongo 一致），
所以这条测试不需要真的连数据库。

用法：python tests/test_user_isolation.py
"""
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _harness  # noqa: E402  隔离存档目录与环境变量

PROJECT = _harness.PROJECT
sys.path.insert(0, str(PROJECT))

from lingheng import sessions, storage, store  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


def seed(session_id: str, title: str) -> None:
    """以"当前用户"的身份写下一个完整会话。"""
    storage.write_json(f"sessions/{session_id}/meta.json",
                       {"title": title, "pinned": False, "current_branch": "main",
                        "branches": [{"id": "main", "parent": None, "fork_index": None,
                                      "created_at": ""}]})
    storage.write_json(f"sessions/{session_id}/branches/main.json",
                       {"message": [{"role": "user", "content": title}]})


# --------------------------------------------------------------------------- #
# 两种后端各跑一遍相同的一组断言
# --------------------------------------------------------------------------- #
def exercise(label: str) -> None:
    print(f"\n--- 后端：{label} ---")
    storage.set_current_uid("alice")
    seed("2026-01-01_000000_000", "alice 的会话")
    storage.write_json("sessions/drafts/2026-01-01_000000_000.draft", {"nickname": "a"})
    alice_sessions = sessions.load_session_list()
    check(f"[{label}] alice 看得到自己的会话", alice_sessions == ["2026-01-01_000000_000"], alice_sessions)

    storage.set_current_uid("bob")
    bob_sessions = sessions.load_session_list()
    check(f"[{label}] bob 看不到 alice 的会话", bob_sessions == [], bob_sessions)
    check(f"[{label}] bob 读不到 alice 的 meta",
          sessions.load_session_meta("2026-01-01_000000_000").get("title") != "alice 的会话",
          sessions.load_session_meta("2026-01-01_000000_000").get("title"))
    check(f"[{label}] bob 读不到 alice 的分支内容",
          sessions.read_branch("2026-01-01_000000_000", "main").get("message") == [],
          sessions.read_branch("2026-01-01_000000_000", "main"))
    check(f"[{label}] bob 的会话列表里没有 alice 的会话",
          "2026-01-01_000000_000" not in sessions.load_session_list())

    # 破坏性操作：bob 去删 alice 的会话
    sessions.delete_session("2026-01-01_000000_000")
    storage.set_current_uid("alice")
    check(f"[{label}] bob 删不掉 alice 的会话", sessions.load_session_list() == ["2026-01-01_000000_000"],
          sessions.load_session_list())

    # bob 自己建一个，互不影响
    storage.set_current_uid("bob")
    seed("2026-02-02_000000_000", "bob 的会话")
    check(f"[{label}] bob 只看到自己的", sessions.load_session_list() == ["2026-02-02_000000_000"],
          sessions.load_session_list())
    storage.set_current_uid("alice")
    check(f"[{label}] alice 仍只看到自己的", sessions.load_session_list() == ["2026-01-01_000000_000"],
          sessions.load_session_list())

    # 附件（头像图片）也要隔离
    storage.set_current_uid("alice")
    storage.write_bytes("sessions/2026-01-01_000000_000/attachments/a.png", b"ALICE-PNG")
    storage.set_current_uid("bob")
    check(f"[{label}] bob 读不到 alice 的头像字节",
          storage.read_bytes("sessions/2026-01-01_000000_000/attachments/a.png") == b"",
          storage.read_bytes("sessions/2026-01-01_000000_000/attachments/a.png"))

    # 路径穿越：键名里带 .. 必须被拒绝
    storage.set_current_uid("bob")
    for bad in ("../alice/sessions/x", "sessions/../../x", "sessions/a/../../b"):
        try:
            storage.read_json(bad)
            check(f"[{label}] 拒绝越界路径 {bad[:20]}", False, "竟然没报错")
        except ValueError:
            check(f"[{label}] 拒绝越界路径 {bad[:20]}", True)


# ① 文件后端（本地开发/测试默认）
storage.mongo_uri = lambda: ""
storage.set_current_uid("local")
exercise("文件")

# 清理，避免影响 ②（两个后端共用一个 uid 命名空间概念，但存储位置不同）
storage.set_current_uid("local")
storage.delete_prefix("sessions")

# ② MongoDB 后端（mongomock 内存实现）
try:
    import mongomock

    client = mongomock.MongoClient()
    storage.MongoBackend.__init__.__globals__  # 只是确认模块已导入
    original_client = storage._client
    storage._client = lambda: client
    storage.mongo_uri = lambda: "mongodb://fake-for-test"
    exercise("MongoDB")
    storage._client = original_client
except ImportError:
    print("\n（mongomock 未安装，跳过 MongoDB 后端的隔离验证）")

storage.mongo_uri = lambda: ""
storage.set_current_uid("local")

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
