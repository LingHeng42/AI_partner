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

    def queue(self, chunks):
        """测试用：让下一次 render_reply 返回指定正文分片。"""
        STATE["chunks"] = list(chunks)


class _Chat:
    completions = _Completions()


class FakeOpenAI:
    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.chat = _Chat()


st.cache_resource.clear()
_app.OpenAI = FakeOpenAI
_app.__dict__["_FAKE_OPENAI_STATE"] = STATE
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
#     注意：一个测试进程里只有第一次 AppTest 运行会真正执行应用脚本，之后的
#     运行会复用 sys.modules 里已缓存的 AI_partner（脚本主体被跳过），所以
#     所有针对完整页面的断言都必须挂在这一段上。
# --------------------------------------------------------------------------- #
SESSION_BACKUP = {}
sessions_dir = PROJECT / "sessions"
sessions_dir.mkdir(exist_ok=True)
for _f in sessions_dir.glob("*.json"):  # 先备份真实存档，测试结束后还原
    SESSION_BACKUP[_f.name] = _f.read_text(encoding="utf-8")
    _f.unlink()

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
sessions_dir = PROJECT / "sessions"
check("主区域不再显示会话 ID", not any("当前会话：" in c.value for c in at.caption), [c.value for c in at.caption][:3])
check("没有对话时不显示会话条目",
      not any("（当前）" in b.label for b in at.button), [b.label for b in at.button])
check("空会话给出会话历史提示",
      any("还没有会话" in c.value for c in at.caption), [c.value for c in at.caption])
check("启动阶段不创建空存档", not list(sessions_dir.glob("*.json")),
      [f.name for f in sessions_dir.glob("*.json")])
check("启动阶段未发起任何模型请求", _fake_openai.STATE["calls"] == [])

# --------------------------------------------------------------------------- #
# B. 点击「新建会话」：回调里改 session_title 不能触发
#    StreamlitWidgetAlreadyInstantiatedError（会话名称输入框已先实例化）
# --------------------------------------------------------------------------- #
new_button = next((b for b in at.button if b.label == "新建会话"), None)
check("找到新建会话按钮", new_button is not None)
if new_button is not None:
    before = at.session_state["current_session"]
    # .run() 返回的是新的 AppTest（旧对象的元素树不会被更新），必须接住它
    at = new_button.click().run()
    check("点击新建会话不抛异常", not at.exception, [e.message for e in at.exception] or "")
    check("新建会话后换新 ID", at.session_state["current_session"] != before, at.session_state["current_session"])
    check("新建会话后名称回到默认", at.session_state["session_title"] == "新会话", at.session_state["session_title"])
    check("新建会话后对话清空", at.session_state["message"] == [], at.session_state["message"])
    check("新建会话不会创建空存档", not list(sessions_dir.glob("*.json")),
          [f.name for f in sessions_dir.glob("*.json")])
for leftover in sessions_dir.glob("*.json"):
    leftover.unlink()

# --------------------------------------------------------------------------- #
# D. 「第一轮对话后会话自动出现在历史里」的验证放在 tests/test_logic.py：
#    它直接按 发消息 → render_reply 落盘 → load_session_list 的顺序断言，
#    而这里同一进程内只有一次 AppTest 能真正渲染页面，做不出"先空后满"的两轮观察。
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# C. 缺少 API Key 的分支
#     该分支由 tests/test_logic.py 直接调用 require_api_key() 断言（能真正拿到
#     st.error / st.stop 的调用记录）。这里不再重复，因为同一进程里 AI_partner
#     已被缓存，包装脚本的 import 不会重新执行，跑出来的结果是假的。
# --------------------------------------------------------------------------- #

# 还原测试前备份的真实存档
for _f in sessions_dir.glob("*.json"):
    _f.unlink()
for _name, _text in SESSION_BACKUP.items():
    (sessions_dir / _name).write_text(_text, encoding="utf-8")

# --------------------------------------------------------------------------- #
# D. 会话条目的操作入口（置顶 / 重命名 / 删除）渲染检查放在 tests/test_row_render.py：
#    那个文件在唯一一次渲染之前就把存档准备好，所以会话条目会真的渲染出来。
# --------------------------------------------------------------------------- #

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
