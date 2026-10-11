"""端到端：从**界面点击删除按钮**，验证数据真的被删掉。

为什么必须端到端：之前只测了 ``storage.delete_prefix`` 这个函数本身（它是好的），
但用户遇到的是"点了删除、数据库里数据还在"。中间隔着好几层：
    按钮回调 → 待办标记 → 主脚本体执行 → delete_session → 存储层
任何一层断了，函数级测试都发现不了。

用文件后端跑（删除逻辑不分后端；MongoDB 的 delete_prefix 另有
test_user_isolation.py 覆盖），这样能直接看磁盘。

用法：python tests/test_delete_flow.py
"""
import json
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

keep = "2030-01-01_000000_000"    # 要保留的会话（用来验证"删一条不波及另一条"）
victim = "2030-02-02_000000_000"  # 要删除的会话

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


def seed(sid: str, text: str, count: int = 3) -> None:
    """直接写文件种数据（绕过存储层，避免计数/缓存干扰）。"""
    meta = _harness.session_meta_path(sid)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({
        "title": sid, "pinned": False, "current_branch": "main",
        "branches": [{"id": "main", "parent": None, "fork_index": None, "created_at": ""}],
        "nickname": "溟月", "nature": "n", "role_description": "r", "output_rules": "o",
    }, ensure_ascii=False), encoding="utf-8")
    branch = _harness.session_branch_path(sid)
    branch.parent.mkdir(parents=True, exist_ok=True)
    branch.write_text(json.dumps({
        "message": [{"role": "user", "content": f"{text}-{i}"} for i in range(count)]
    }, ensure_ascii=False), encoding="utf-8")


def on_disk(sid: str) -> bool:
    return _harness.session_meta_path(sid).exists()


seed(keep, "保留")
seed(victim, "待删")
check("两条会话都已写入", on_disk(keep) and on_disk(victim))

# 复制入口（保持重跑语义），让应用从 keep 这条会话开始。
# 另外包一层 delete_session，把"回调真的被触发过"写到一个文件里 ——
# AppTest 会回滚回调期间的 session_state 变更，所以只能靠这种外部信号来确认
# 回调确实执行了（而不是靠读 at.session_state）。
FAKE = _harness.TMP_DIR / "_delete_flow"
FAKE.mkdir(parents=True, exist_ok=True)
marker = FAKE / "callback_fired.txt"
entry = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")
prelude = (
    "import sys\n"
    # ⚠️ AppTest 是在**本进程内**执行这段脚本的，而 _harness（被应用路径牵连）在导入时
    # 会 reset_tmp() 清空整个 tests/.tmp —— 也就是把本测试刚种下的会话文件删掉，
    # 于是应用渲染出"还没有会话"，断言随机失败。先指到独立临时根，杜绝这次清空。
    f"import os; os.environ['AI_PARTNER_TEST_TMP'] = "
    f"{str((_harness.TMP_DIR.parent / '_apptmp').resolve()).replace(chr(92), '/')!r}\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    "import streamlit as st\n"
    "from lingheng import sessions as _s\n"
    f"_MARK = {str(marker).replace(chr(92), '/')!r}\n"
    "_orig_delete = _s.delete_session\n"
    "def _spy_delete(name):\n"
    "    with open(_MARK, 'a', encoding='utf-8') as fh:\n"
    "        fh.write(name + '\\n')\n"
    "    return _orig_delete(name)\n"
    "_s.delete_session = _spy_delete\n"
    "st.session_state['user_api_key'] = 'sk-probe'\n"
    f"st.session_state['current_session'] = {keep!r}\n"
    "\n"
)
copy = FAKE / "_app.py"
copy.write_text(prelude + entry, encoding="utf-8")

from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(copy), default_timeout=90).run()
check("应用启动无异常（线上事故的哨兵：导入期就崩过）", not at.exception,
      [e.message[:200] for e in at.exception])

# 找到 victim 的删除按钮并点击
btn = next((b for b in at.button if b.key == f"confirm_delete_{victim}"), None)
check("找得到待删会话的删除按钮", btn is not None, [b.key for b in at.button])
if btn is None:
    print()
    print("FAILURES:", failures)
    sys.exit(1)

at = btn.click().run()
check("点击删除后无异常", not at.exception, [e.message[:200] for e in at.exception])

# 真实 Streamlit 会在回调后自动重跑一次、由主脚本体执行待办；AppTest 不会重跑，
# 所以这里手动补一次（等价于线上那次重跑）。
from lingheng import sessions  # noqa: E402

sessions.st.session_state = at.session_state
sessions.run_pending_actions()

check("待办执行后：待删会话的数据真的没了", not on_disk(victim),
      _harness.session_meta_path(victim))
check("待办执行后：保留的会话没被波及", on_disk(keep))
check("待办执行后：保留的会话内容完好",
      [m["content"] for m in json.loads(
          _harness.session_branch_path(keep).read_text(encoding="utf-8"))["message"]]
      == ["保留-0", "保留-1", "保留-2"])

# 回调确实被触发过（外部信号，不受 session_state 回滚影响）
fired = marker.read_text(encoding="utf-8").split() if marker.exists() else []
check("删除按钮的回调确实被触发（不是按钮没反应）", victim in fired, fired)
check("待办执行后：自动切到了新会话",
      at.session_state.get("current_session") != victim,
      at.session_state.get("current_session"))

# 清理这个测试用的会话
from lingheng import storage as _storage  # noqa: E402

_storage.set_current_uid("local")
sessions.delete_session(keep)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
