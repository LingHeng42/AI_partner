"""端到端：应用跑在 MongoDB 后端上，验证"发言真的写进数据库，且重开能读回"。

这是线上真正会发生的事（Community Cloud 的本地文件随时会被重置，所以必须用
外部数据库）。测试用 mongomock 在内存里模拟 MongoDB，不需要真连数据库，
但走的代码路径与线上一致（storage.MongoBackend）。

**为什么要"复制入口"而不是写一个包装脚本**：包装脚本会 `import AI_partner`，
而 Python 的 import 有缓存——AppTest 重跑时入口不会重新执行，交互类的断言就会
失真（表现为"提交后页面空白、消息没入列"，其实是探针假象）。
复制一份入口、把配置代码插在前面，才能既注入假依赖、又保持重跑语义。

用法：python tests/test_mongo_ui.py
"""
import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _harness  # noqa: E402

PROJECT = _harness.PROJECT
sys.path.insert(0, str(PROJECT))

from streamlit.testing.v1 import AppTest  # noqa: E402

SESSION_ID = "2099-08-08_080808_000"
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


FAKE = _harness.TMP_DIR / "_mongo_probe"
FAKE.mkdir(parents=True, exist_ok=True)

# 共享的 mongomock 客户端：放在一个模块里，应用侧与测试侧拿到同一个实例
(FAKE / "_mongo_client.py").write_text(
    "import mongomock\nCLIENT = mongomock.MongoClient()\n",
    encoding="utf-8",
)

# --------------------------------------------------------------------------- #
# 复制入口：配置代码插在 import 之前（AI_partner 只被导入一次，保持重跑语义）
# --------------------------------------------------------------------------- #
entry = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")
prelude = (
    "import sys\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    f"sys.path.insert(0, {str(FAKE).replace(chr(92), '/')!r})\n"
    "import _mongo_client\n"
    "from lingheng import storage\n"
    "# 把存储切到 MongoDB 后端（内存实现），其余代码完全不变\n"
    "storage.mongo_uri = lambda: 'mongodb://fake-in-test'\n"
    "storage._client = lambda: _mongo_client.CLIENT\n"
    "import streamlit as st\n"
    "st.session_state['user_api_key'] = 'sk-test-user-key'\n"
    f"st.session_state['current_session'] = {SESSION_ID!r}\n"
    "\n"
)
copy = FAKE / "_mongo_app.py"
copy.write_text(prelude + entry, encoding="utf-8")

at = AppTest.from_file(str(copy), default_timeout=60).run()
check("MongoDB 后端下应用能启动（无异常）", not at.exception, [e.message for e in at.exception])
check("本地文件系统里没有存档（确实写到了数据库）",
      not list(_harness.sessions_root().rglob("meta.json")),
      [str(p) for p in _harness.sessions_root().rglob("meta.json")][:3])
check("渲染出了聊天输入框", len(at.chat_input) == 1, len(at.chat_input))

# 发一条消息：走真实的发言分支（入列 → 生成 → 落库）
at = at.chat_input[0].set_value("你好").run()
check("发言没有报错", not at.exception, [e.message for e in at.exception])
check("消息已入列", at.session_state.get("message") == [{"role": "user", "content": "你好"}],
      at.session_state.get("message"))

# --------------------------------------------------------------------------- #
# 从测试进程直接查库：确认真的写进了 MongoDB
# --------------------------------------------------------------------------- #
from _mongo_client import CLIENT  # noqa: E402

keys = sorted(d["key"] for d in CLIENT["lingheng"]["files"].find({"uid": "local"}))
meta_key = f"users/local/sessions/{SESSION_ID}/meta.json"
branch_key = f"users/local/sessions/{SESSION_ID}/branches/main.json"
check("meta.json 写进了数据库", meta_key in keys, keys)
check("分支文件写进了数据库", branch_key in keys, keys)

doc = CLIENT["lingheng"]["files"].find_one({"key": branch_key})
messages = json.loads(bytes(doc["data"]).decode("utf-8"))["message"]
check("数据库里存着用户发出的这条消息",
      [m["content"] for m in messages] == ["你好"], messages)

# --------------------------------------------------------------------------- #
# 模拟"重启后再读"：用存储层重新读一遍（新对象、同一条代码路径）
# --------------------------------------------------------------------------- #
from lingheng import sessions, storage  # noqa: E402

storage.mongo_uri = lambda: "mongodb://fake-in-test"
storage._client = lambda: CLIENT
storage.set_current_uid("local")
check("重启后能列出这个会话", sessions.load_session_list() == [SESSION_ID], sessions.load_session_list())
check("重启后能读回消息",
      [m["content"] for m in sessions.read_branch(SESSION_ID, "main")["message"]] == ["你好"],
      sessions.read_branch(SESSION_ID, "main"))

storage.set_current_uid("someone-else")
check("换成别人的身份就读不到了（MongoDB 后端下的隔离）",
      sessions.load_session_list() == [], sessions.load_session_list())
storage.set_current_uid("local")

# 重复交互不能丢数据：再发一条，消息应该继续累积
at = at.chat_input[0].set_value("再来一条").run()
check("再发一条仍然正常", not at.exception, [e.message for e in at.exception])
doc = CLIENT["lingheng"]["files"].find_one({"key": branch_key})
later = json.loads(bytes(doc["data"]).decode("utf-8"))["message"]
check("第二条也落了库（消息累积而不是覆盖）",
      [m["content"] for m in later] == ["你好", "再来一条"],
      [m["content"] for m in later])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
