"""会话条目渲染检查：置顶标记与 col2 的「置顶 / 重命名 / 删除」操作菜单。

为什么单独一个文件：AppTest 在同一进程里只有第一次运行会真正执行应用脚本
（之后 import 命中 sys.modules 缓存），而"会话条目"要求渲染前磁盘上就已经有存档。
所以这里把存档准备好之后再做那唯一一次渲染。

运行：
    .venv\\Scripts\\python.exe tests\\test_row_render.py
"""
import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(__file__).resolve().parent / ".tmp" / f"row_{os.getpid()}"
TMP_DIR.mkdir(parents=True, exist_ok=True)
sys.dont_write_bytecode = True

os.environ["TMPDIR"] = str(TMP_DIR)
os.environ["TEMP"] = str(TMP_DIR)
os.environ["TMP"] = str(TMP_DIR)
os.environ["DEEPSEEK_API_KEY"] = "sk-test-not-used"
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(TMP_DIR))

from streamlit.testing.v1 import AppTest  # noqa: E402

SESSIONS_DIR = PROJECT / "sessions"
SESSION_ID = "2099-09-09_000000_000"
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------- #
# 假客户端（脚本 import OpenAI 时替换掉）
# --------------------------------------------------------------------------- #
(TMP_DIR / "_fake_openai.py").write_text(
    '''
import streamlit as st
import AI_partner as _app


class _Completions:
    def create(self, **kwargs):
        return iter([])


class FakeOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = type("Chat", (), {"completions": _Completions()})()


st.cache_resource.clear()
_app.OpenAI = FakeOpenAI
''',
    encoding="utf-8",
)

wrapper = TMP_DIR / "_row_wrapper.py"
wrapper.write_text(
    "import os, sys\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    f"sys.path.insert(0, {str(TMP_DIR).replace(chr(92), '/')!r})\n"
    "os.environ['DEEPSEEK_API_KEY'] = 'sk-test-not-used'\n"
    "import _fake_openai\n"
    "import streamlit as st\n"
    # 关键：AppTest 每次都是全新的空 session_state，所以状态必须在脚本里、
    # 且在应用开始渲染之前种下（此后 setdefault 会保留这些值）
    f"st.session_state['current_session'] = {SESSION_ID!r}\n"
    "st.session_state['message'] = [{'role': 'user', 'content': '第一条'}]\n"
    "import AI_partner\n"
    # 标记脚本确实执行过（Streamlit 运行器会复用已缓存的模块）
    "st.session_state['_row_render_ran'] = True\n"
    "st.session_state['_dbg_after_import'] = repr(st.session_state.get('current_session'))\n",
    encoding="utf-8",
)

# --------------------------------------------------------------------------- #
# 准备：备份真实存档，写入一条已置顶的测试会话
# --------------------------------------------------------------------------- #
backup = {}
SESSIONS_DIR.mkdir(exist_ok=True)
for path in SESSIONS_DIR.glob("*.json"):
    backup[path.name] = path.read_text(encoding="utf-8")
    path.unlink()

try:
    (SESSIONS_DIR / f"{SESSION_ID}.json").write_text(
        json.dumps(
            {
                "title": "渲染检查会话",
                "pinned": True,
                "message": [{"role": "user", "content": "历史消息"}],
                "nickname": "溟月",
                "nature": "测试性格",
                "role_description": "测试简介",
                "output_rules": "测试规则",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # ----------------------------------------------------------------------- #
    # 唯一一次真正执行应用脚本的渲染
    # ----------------------------------------------------------------------- #
    at = AppTest.from_file(str(wrapper), default_timeout=60).run()
    buttons = [b.label for b in at.button]
    text_inputs = [t.label for t in at.text_input]

    check("页面无异常启动", not at.exception, str(at.exception))
    check("应用脚本确实被执行（未被模块缓存跳过）",
          at.session_state.get("_row_render_ran") is True)
    # 注：这里不断言「（当前）」标记。AppTest 复用了自己的 SessionState 对象，
    # 应用在渲染期间通过属性访问看到的是另一个会话 ID，这个标记在本环境下
    # 观测不到（真实浏览器里正常）——宁可不测，也不要写一条假通过的断言。
    check("会话条目带置顶标记 📌", any("📌" in label for label in buttons), buttons)
    check("会话条目显示自定义名称", any("渲染检查会话" in label for label in buttons), buttons)
    check("操作菜单里是「取消置顶」（已置顶）",
          any(label == "取消置顶" for label in buttons), buttons)
    check("操作菜单里含重命名输入框", "重命名" in text_inputs, text_inputs)
    check("重命名不再有保存按钮（改为回车生效）",
          not any("保存名称" in label for label in buttons), buttons)
    check("操作菜单里含删除确认", any("确认删除" in label for label in buttons), buttons)
    check("会话名称输入框仍在", "会话名称" in text_inputs, text_inputs)
    check("会话条目显示的名称来自存档", any("渲染检查会话" in label for label in buttons), buttons)

    # ----------------------------------------------------------------------- #
    # 在原报错路径上验证：在重命名输入框里改完按回车（set_value + run 等价于回车提交）
    # 之前这里会抛 StreamlitWidgetAlreadyInstantiatedError
    # ----------------------------------------------------------------------- #
    rename_box = next((t for t in at.text_input if t.label == "重命名"), None)
    check("找到重命名输入框", rename_box is not None)
    if rename_box is not None:
        rename_box.set_value("改名后的会话")
        after = at.run()
        errors = [e.message for e in after.exception]
        check("回车重命名不抛异常", not errors, errors)
        check("回车重命名写入存档",
              json.loads((SESSIONS_DIR / f"{SESSION_ID}.json").read_text(encoding="utf-8"))["title"] == "改名后的会话",
              json.loads((SESSIONS_DIR / f"{SESSION_ID}.json").read_text(encoding="utf-8")).get("title"))
        check("回车重命名同步了会话名称状态",
          after.session_state["session_title"] == "改名后的会话",
          after.session_state["session_title"])
    check("重命名不留下临时文件",
          not list(SESSIONS_DIR.glob("*.tmp*")), [p.name for p in SESSIONS_DIR.glob("*.tmp*")])
finally:
    for path in list(SESSIONS_DIR.glob("*.json")) + list(SESSIONS_DIR.glob("*.tmp*")):
        path.unlink()
    for name, text in backup.items():
        (SESSIONS_DIR / name).write_text(text, encoding="utf-8")

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
