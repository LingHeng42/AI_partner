"""AI_partner.py 冒烟测试：用假的 OpenAI 客户端跑真实 Streamlit 页面，不发起网络请求。

覆盖：页面能否无异常启动、侧边栏控件是否齐全、缺少 API Key 时是否给出可读提示。
对话流程（流式拼接 / 异常处理 / 空回答 / 上下文截断 / 存档）由 tests/test_logic.py 覆盖。

运行：
    .venv\\Scripts\\python.exe tests\\test_smoke.py
"""
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(__file__).resolve().parent / ".tmp" / f"run_{os.getpid()}"
TMP_DIR.mkdir(parents=True, exist_ok=True)

# 把字节码缓存也限制在本次运行的目录里，避免复用上一次运行留下的 .pyc
sys.dont_write_bytecode = True

# 临时文件留在仓库内，避免写到系统临时目录
os.environ["TMPDIR"] = str(TMP_DIR)
os.environ["TEMP"] = str(TMP_DIR)
os.environ["TMP"] = str(TMP_DIR)
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(TMP_DIR))

from streamlit.testing.v1 import AppTest  # noqa: E402

# 注意：不要在测试进程里预先 import AI_partner。
# Streamlit 的脚本运行器会复用 sys.modules 里已有的模块，导致脚本主体被跳过。
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


def write_wrapper(name: str, body: str) -> Path:
    path = TMP_DIR / name
    path.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
        f"sys.path.insert(0, {str(TMP_DIR).replace(chr(92), '/')!r})\n"
        + body,
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# 假客户端：AI_partner 在脚本里 import OpenAI 时拿到的是这个类
# --------------------------------------------------------------------------- #
(TMP_DIR / "_fake_openai.py").write_text(
    '''
import streamlit as st
import AI_partner as _app

STATE = {"calls": [], "mode": "ok", "chunks": ["你好", "，", "人类"]}


def _chunk(text):
    delta = type("Delta", (), {"content": text})()
    choice = type("Choice", (), {"delta": delta})()
    return type("Chunk", (), {"choices": [choice]})()


class _Completions:
    def create(self, **kwargs):
        STATE["calls"].append(kwargs)
        if STATE["mode"] == "boom":
            raise RuntimeError("fake network error")
        return iter([_chunk(t) for t in STATE["chunks"]])


class _Chat:
    completions = _Completions()


class FakeOpenAI:
    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.chat = _Chat()


st.cache_resource.clear()
_app.OpenAI = FakeOpenAI
''',
    encoding="utf-8",
)

wrapper = write_wrapper(
    "_app_wrapper.py",
    "os.environ['DEEPSEEK_API_KEY'] = 'sk-test-not-used'\n"
    "import _fake_openai\n"
    "import AI_partner\n",
)

# --------------------------------------------------------------------------- #
# A. 页面渲染
# --------------------------------------------------------------------------- #
at = AppTest.from_file(str(wrapper), default_timeout=60).run()
import _fake_openai  # noqa: E402  脚本执行时已导入，这里拿到同一个模块对象

check("假客户端已注入", isinstance(_fake_openai.STATE, dict), type(_fake_openai.STATE).__name__)
check("页面无异常启动", not at.exception, str(at.exception))
check("标题不再占据主区域", not any("凌恒的酒馆" in h.value for h in at.header), [h.value for h in at.header])
check("标题移到侧边栏（markdown 渲染）", any("凌恒的酒馆" in (m.value or "") for m in at.markdown),
      [m.value for m in at.markdown][:3])
check("聊天输入框存在", len(at.chat_input) == 1)
check("会话名称输入框存在", "会话名称" in [t.label for t in at.text_input], [t.label for t in at.text_input])
check("会话名称默认值", at.session_state["session_title"] == "新会话", at.session_state["session_title"])
check("昵称输入框存在", "昵称" in [t.label for t in at.text_input], [t.label for t in at.text_input])
check("昵称字段名为 nickname", at.session_state["nickname"] == "溟月", at.session_state["nickname"])
check("侧边栏三个多行输入框齐全", [t.label for t in at.text_area] == ["性格", "角色简介", "输出规则"], [t.label for t in at.text_area])
check("侧边栏有深度思考开关", [t.label for t in at.toggle] == ["深度思考"], [t.label for t in at.toggle])
check("新建会话按钮存在", any(b.label == "新建会话" for b in at.button))
advanced = [e for e in at.expander if "高级配置" in (e.label or "")]
check("高级配置折叠面板存在", len(advanced) == 1, [e.label for e in at.expander])
check("高级配置默认折叠", advanced and advanced[0].proto.expanded is False, advanced[0].proto.expanded if advanced else None)
check("会话历史分区存在", any(s.value == "会话历史" for s in at.subheader), [s.value for s in at.subheader])
check("管理角色分区存在", any(s.value == "管理角色" for s in at.subheader), [s.value for s in at.subheader])
check("生成参数分区存在", any(s.value == "生成参数" for s in at.subheader), [s.value for s in at.subheader])
check("主区域不再显示会话 ID", not any("当前会话：" in c.value for c in at.caption), [c.value for c in at.caption][:3])
check("启动阶段未发起任何模型请求", _fake_openai.STATE["calls"] == [])

# --------------------------------------------------------------------------- #
# B. 点击「新建会话」：回调里改 session_title 不能触发
#    StreamlitWidgetAlreadyInstantiatedError（会话名称输入框已先实例化）
# --------------------------------------------------------------------------- #
new_button = next((b for b in at.button if b.label == "新建会话"), None)
check("找到新建会话按钮", new_button is not None)
if new_button is not None:
    before = at.session_state["current_session"]
    new_button.click().run()  # 回调在控件实例化之前执行，这里正是原报错的路径
    check("点击新建会话不抛异常", not at.exception, [e.message for e in at.exception] or "")
    check("新建会话后换新 ID", at.session_state["current_session"] != before, at.session_state["current_session"])
    check("新建会话后名称回到默认", at.session_state["session_title"] == "新会话", at.session_state["session_title"])
    check("新建会话后对话清空", at.session_state["message"] == [], at.session_state["message"])

# --------------------------------------------------------------------------- #
# C. 缺少 API Key 时给出可读提示，而不是 SDK 堆栈
#    该分支在 tests/test_logic.py 里通过 require_api_key() 直接断言，
#    这里只确认页面不会以异常形式炸掉。
# --------------------------------------------------------------------------- #
no_key_wrapper = write_wrapper(
    "_no_key_wrapper.py",
    "os.environ.pop('DEEPSEEK_API_KEY', None)\n"
    "import AI_partner\n",
)
at_no_key = AppTest.from_file(str(no_key_wrapper), default_timeout=60).run()
check("缺 Key 时不抛 SDK 堆栈", not at_no_key.exception, str(at_no_key.exception))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
