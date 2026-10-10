"""切会话必须真的换内容（回归测试）。

背景：主区域原有一个"内存消息属于哪条分支"的守卫，只比**分支名**。而每个会话的
分支都叫 ``main``，所以切会话时守卫判定"已经加载过了"，主区域继续显示上一个会话
的消息 —— 表现就是"点历史会话，按钮变红但对话内容没变"。
修法：归属标记改成 ``会话ID::分支ID``（见 sessions.message_owner）。

用法：python tests/test_session_switch.py
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

OLD_ID = "2026-01-01_000000_000"
NEW_ID = "2026-02-02_000000_000"
OLD_TEXT = "旧会话的内容"
NEW_TEXT = "新会话的内容"

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


def seed(session_id: str, title: str, text: str, branch: str = "main") -> None:
    """写一个完整会话（两个会话的分支都叫 main —— 这正是原来的坑）。"""
    storage.set_current_uid("local")
    storage.write_json(f"sessions/{session_id}/meta.json", {
        "title": title, "pinned": False, "current_branch": branch,
        "branches": [{"id": branch, "parent": None, "fork_index": None, "created_at": ""}],
        "nickname": "溟月", "nature": "n", "role_description": "r", "output_rules": "o",
    })
    storage.write_json(f"sessions/{session_id}/branches/{branch}.json", {
        "message": [
            {"role": "user", "content": f"{text}-用户"},
            {"role": "assistant", "content": f"{text}-AI"},
        ]
    })


seed(OLD_ID, "旧会话", OLD_TEXT)
seed(NEW_ID, "新会话", NEW_TEXT)

# 复制入口（保持重跑语义，不要预先 import AI_partner）
FAKE = _harness.TMP_DIR / "_switch_app"
FAKE.mkdir(parents=True, exist_ok=True)
entry = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")
prelude = (
    "import sys\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    "import streamlit as st\n"
    "st.session_state['user_api_key'] = 'sk-probe'\n"
    f"st.session_state['current_session'] = {OLD_ID!r}\n"
    "\n"
)
copy = FAKE / "_app.py"
copy.write_text(prelude + entry, encoding="utf-8")

from streamlit.testing.v1 import AppTest  # noqa: E402


def rendered(app) -> str:
    return " ".join(str(m.value) for m in app.markdown)


at = AppTest.from_file(str(copy), default_timeout=60).run()
check("初始进入没有异常", not at.exception, [e.message[:90] for e in at.exception])
# 首次进入时主区域按"当前会话"从存储读，两个会话的分支都叫 main
body0 = rendered(at)
check("初始显示的是旧会话内容或尚未加载（不应显示新会话）",
      NEW_TEXT not in body0, body0[-120:])

# 点击新会话那一条
target = next((b for b in at.button if b.key == f"session_{NEW_ID}"), None)
check("找得到新会话的按钮", target is not None, [b.key for b in at.button])
at2 = target.click().run()
check("切换后没有异常", not at2.exception, [e.message[:120] for e in at2.exception])

body1 = rendered(at2)
check("切换后主区域显示新会话的内容", NEW_TEXT in body1, body1[-160:])
check("切换后主区域不再显示旧会话的内容", OLD_TEXT not in body1, body1[-160:])
# 标题也应跟着换（标题取自当前会话的 meta）
check("切换后页面标题是当前会话的名称",
      any(t.value == "新会话" for t in at2.header), [t.value for t in at2.header])

# 注意：这里刻意**不**断言 at2.session_state['message']。
# AppTest 在控件回调结束后会把 session_state 回滚到点击前的快照（已多次踩到），
# 但**渲染出来的页面是对的**——用户看到的就是渲染结果，所以按渲染断言。
# 归属标记同理：它在真实服务器上会被正确更新，AppTest 里读到的可能是旧快照。

# 再切回旧会话，确保双向都对
back = next((b for b in at2.button if b.key == f"session_{OLD_ID}"), None)
check("找得到旧会话的按钮", back is not None)
at3 = back.click().run()
check("切回后没有异常", not at3.exception, [e.message[:120] for e in at3.exception])
body2 = rendered(at3)
check("切回后重新显示旧会话的内容", OLD_TEXT in body2, body2[-160:])
check("切回后不再显示新会话的内容", NEW_TEXT not in body2, body2[-160:])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
