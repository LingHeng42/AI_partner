"""存储请求预算：防止"每次渲染访问几十次数据库"的回退。

为什么值得单独钉一条测试：MongoDB 上每一次存储请求都是一次网络往返。
写多用户支持时实测一次渲染产生了 **32 次**请求（按 200ms 算就是 6 秒多白等），
原因是同一个文件被反复读取、列表页逐个会话读 meta。
加了一次渲染内的读缓存 + 批量读之后降到 16 次 —— 这条测试就是锁住这个成果。

用法：python tests/test_storage_budget.py
"""
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _harness  # noqa: E402

PROJECT = _harness.PROJECT
sys.path.insert(0, str(PROJECT))
os.environ["AI_PARTNER_SESSIONS_DIR"] = str(_harness.SESSIONS_DIR)
os.environ.pop("DEEPSEEK_API_KEY", None)

from lingheng import storage  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


SESSION_COUNT = 8

# 造 8 个会话，每个 2 条分支、每条 3 条消息（比单个测试会话更能暴露"逐个读"）。
# 注意**直接写文件**、不经过存储层：否则种数据本身也要计数，量出来的就不是
# "应用这次渲染发了多少请求"了。
import json  # noqa: E402

for i in range(SESSION_COUNT):
    sid = f"2026-03-{i + 1:02d}_000000_000"
    meta_path = _harness.session_meta_path(sid)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({
        "title": f"会话{i}", "pinned": i % 3 == 0, "current_branch": "main",
        "branches": [
            {"id": "main", "parent": None, "fork_index": None, "created_at": ""},
            {"id": "b2", "parent": "main", "fork_index": 1, "created_at": ""},
        ],
        "nickname": "溟月", "nature": "n", "role_description": "r", "output_rules": "o",
    }, ensure_ascii=False), encoding="utf-8")
    for branch in ("main", "b2"):
        branch_path = _harness.session_branch_path(sid, branch)
        branch_path.parent.mkdir(parents=True, exist_ok=True)
        branch_path.write_text(json.dumps({
            "message": [{"role": "user", "content": f"{i}-{branch}-{k}"} for k in range(3)]
        }, ensure_ascii=False), encoding="utf-8")

FAKE = _harness.TMP_DIR / "_budget_app"
FAKE.mkdir(parents=True, exist_ok=True)
entry = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")
prelude = (
    "import sys\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    "import streamlit as st\n"
    "st.session_state['user_api_key'] = 'sk-budget'\n"
    "st.session_state['current_session'] = '2026-03-01_000000_000'\n"
    "\n"
)
copy = FAKE / "_app.py"
copy.write_text(prelude + entry, encoding="utf-8")

from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(copy), default_timeout=60).run()
check("应用能正常渲染", not at.exception, [e.message[:90] for e in at.exception])

# 侧边栏会把本次渲染的请求数显示出来，直接读它（同时也是这条指标的可见性保障）
captions = [str(c.value) for c in at.caption]
counts = [c for c in captions if "次存储请求" in c]
check("侧边栏显示了本次渲染的存储请求数", bool(counts), captions[-4:])

if counts:
    first = int("".join(ch for ch in counts[0] if ch.isdigit()))
    print(f"      （8 个会话，首次渲染发起 {first} 次请求）")
    check("首次渲染的请求数在预算内（≤ 25）", first <= 25, first)
    check("首次渲染没有退化成逐个会话读（应远小于会话数×5）",
          first < SESSION_COUNT * 3, first)
else:
    first = None

at2 = at.chat_input[0].set_value("你好").run()
check("发消息后没有异常", not at2.exception, [e.message[:90] for e in at2.exception])
captions2 = [str(c.value) for c in at2.caption]
counts2 = [c for c in captions2 if "次存储请求" in c]
if counts2:
    second = int("".join(ch for ch in counts2[0] if ch.isdigit()))
    print(f"      （发一条消息，本次渲染发起 {second} 次请求）")
    check("发消息时的请求数在预算内（≤ 30）", second <= 30, second)

# 缓存必须不能让数据变旧：写完立刻读得到
storage.set_current_uid("local")
target = "sessions/2026-03-01_000000_000/meta.json"
storage.write_json(target, {"title": "改过了", "pinned": True, "branches": []})
check("写入后立刻读得到新值（写穿缓存）",
      (storage.read_json(target) or {}).get("title") == "改过了", storage.read_json(target))

new_id = "sessions/2026-09-09_000000_000/meta.json"
storage.write_json(new_id, {"title": "新会话", "pinned": False, "branches": []})
check("新写的会话立刻出现在会话列表里（列表缓存会被作废）",
      "2026-09-09_000000_000" in storage.session_ids(), storage.session_ids())

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
