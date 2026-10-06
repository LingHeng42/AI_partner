"""纯逻辑测试：用假的 streamlit / openai 模块导入 AI_partner，验证存档与上下文逻辑。

不启动 Streamlit runtime，不发起网络请求。
运行：
    .venv\\Scripts\\python.exe tests\\test_logic.py
"""
import datetime
import json
import os
import sys
import time
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TMP_DIR = Path(__file__).resolve().parent / ".tmp" / f"run_{os.getpid()}"
TMP_DIR.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(TMP_DIR)
os.environ["TEMP"] = str(TMP_DIR)
os.environ["TMP"] = str(TMP_DIR)
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test-not-used")

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------- #
# 假 streamlit：让脚本能在 bare python 里被 import
# --------------------------------------------------------------------------- #
class _Sink:
    def __getattr__(self, item):
        return _Sink()

    def __call__(self, *a, **k):
        return _Sink()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter((_Sink(), _Sink()))


class _Cache:
    def __call__(self, func=None, **kwargs):
        if func is None:
            return lambda f: f
        func.clear = lambda: None
        func.__wrapped__ = func
        return func


class _State:
    """同时支持 st.session_state.key 与 st.session_state["key"] 的极简替代品。"""

    def __init__(self, data=None):
        object.__setattr__(self, "_data", dict(data or {}))

    def __getattr__(self, item):
        try:
            return self._data[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self._data[key] = value

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def __contains__(self, key):
        return key in self._data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def setdefault(self, key, default=None):
        return self._data.setdefault(key, default)

    def update(self, other):
        self._data.update(other)

    def clear(self):
        self._data.clear()


class _FakeStreamlit(types.ModuleType):
    """任何未显式定义的 st.xxx 都返回一个可调用、可当上下文管理器的空壳。"""

    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        return _Sink()


class _Recorder:
    """记录 st.error / st.warning / st.caption 的调用，替代空壳。"""

    def __init__(self, name):
        self.name = name
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args[0] if args else "")
        return _Sink()


class _ExpanderSink(_Sink):
    """模拟 st.expander：记录标签与展开状态的每一次变化，可当上下文管理器。"""

    def __init__(self, label, expanded, key=None):
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "states", [expanded])

    @property
    def expanded(self):
        return self.states[-1]

    @expanded.setter
    def expanded(self, value):
        self.states.append(value)

    def __enter__(self):
        expanders.active.append(self)
        return self

    def __exit__(self, *exc):
        expanders.calls.append({"label": self.label, "expanded": self.expanded, "history": list(self.states)})
        if self in expanders.active:
            expanders.active.remove(self)
        return False


class _Expanders:
    """st.expander 的替代品：保留每个面板对象，便于断言最终的展开状态。"""

    def __init__(self):
        self.calls = []
        self.active = []
        self.sinks = []

    def __call__(self, label, expanded=False, **kwargs):
        sink = _ExpanderSink(label, expanded, kwargs.get("key"))
        self.sinks.append(sink)
        return sink

    def by_label(self, label):
        return [s for s in self.sinks if s.label == label]


errors = _Recorder("error")
warnings = _Recorder("warning")
expanders = _Expanders()
fake_st = _FakeStreamlit("streamlit")
fake_st.session_state = _State()
fake_st.cache_resource = _Cache()
fake_st.set_page_config = _Sink()
fake_st.error = errors
fake_st.warning = warnings
fake_st.expander = expanders
fake_st.caption = _Sink()
fake_st.stop = _Sink()
fake_st.rerun = _Sink()
for _name in ("header", "logo", "chat_message", "write", "title", "button", "columns",
              "subheader", "divider", "text_input", "text_area", "toggle", "chat_input",
              "empty", "popover", "text"):
    setattr(fake_st, _name, _Sink())
# 逻辑测试不跑页面分支：让输入类控件返回 None（在 streamlit 里就是"没有输入"）
fake_st.chat_input = lambda *a, **k: None
fake_st.toggle = lambda *a, **k: None
fake_st.text_input = lambda *a, **k: None
fake_st.text_area = lambda *a, **k: None

fake_openai = types.ModuleType("openai")


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs


fake_openai.OpenAI = _FakeOpenAI
sys.modules["streamlit"] = fake_st
sys.modules["openai"] = fake_openai
sys.path.insert(0, str(PROJECT))

import AI_partner as app  # noqa: E402

check("import 成功（假 streamlit/openai）", True)
check("客户端使用 DeepSeek base_url", app.client.init_kwargs["base_url"] == "https://api.deepseek.com")
check("客户端 key 取自 DEEPSEEK_API_KEY", app.client.init_kwargs["api_key"] == os.environ["DEEPSEEK_API_KEY"])

# --------------------------------------------------------------------------- #
# API Key 读取：.env 回退 + 缺失时的提示分支
# --------------------------------------------------------------------------- #
dotenv = TMP_DIR / "dotenv_probe.env"
dotenv.write_text(
    "# 注释行应被忽略\n"
    "\n"
    "AI_PARTNER_PROBE_KEY=from_dotenv\n"
    "AI_PARTNER_QUOTED_KEY=\"quoted_value\"\n",
    encoding="utf-8",
)
os.environ.pop("AI_PARTNER_PROBE_KEY", None)
os.environ.pop("AI_PARTNER_QUOTED_KEY", None)
app.load_dotenv_file(dotenv)
check(".env 键值被读取", os.environ.get("AI_PARTNER_PROBE_KEY") == "from_dotenv", os.environ.get("AI_PARTNER_PROBE_KEY"))
check(".env 引号被剥离", os.environ.get("AI_PARTNER_QUOTED_KEY") == "quoted_value", os.environ.get("AI_PARTNER_QUOTED_KEY"))
os.environ["AI_PARTNER_PROBE_KEY"] = "from_env"
app.load_dotenv_file(dotenv)
check("已存在的环境变量优先于 .env", os.environ["AI_PARTNER_PROBE_KEY"] == "from_env")
app.load_dotenv_file(TMP_DIR / "not_exists.env")
check("缺少 .env 文件时不报错", True)

saved_key = os.environ.pop("DEEPSEEK_API_KEY", None)
app.st.stop = _Recorder("stop")
app.require_api_key()
check("缺少 Key 时调用 st.stop 中止", app.st.stop.calls != [])
check("缺少 Key 时给出 st.error", errors.calls and "DEEPSEEK_API_KEY" in errors.calls[-1], errors.calls[-1:])
os.environ["DEEPSEEK_API_KEY"] = saved_key
app.st.stop = _Sink()
check("Key 正常时原样返回", app.require_api_key() == saved_key)

# --------------------------------------------------------------------------- #
# 会话存档
# --------------------------------------------------------------------------- #
tmp = Path(__file__).resolve().parent / ".tmp" / "sessions"
tmp.mkdir(parents=True, exist_ok=True)
for _f in tmp.glob("*.json"):
    _f.unlink()
app.SESSIONS_DIR = tmp
app.st.session_state = _State()
app.st.session_state.update(app.DEFAULT_PROFILE)
app.st.session_state.update(app.DEFAULT_ADVANCED)
app.st.session_state["thinking"] = False
app.st.session_state["message"] = [{"role": "user", "content": "你好"}]
app.st.session_state["current_session"] = "2026-01-01_120000_000"

app.save_session()
saved = tmp / "2026-01-01_120000_000.json"
check("会话存档写入成功", saved.exists())
data = json.loads(saved.read_text(encoding="utf-8"))
check("存档字段 = 四大人设 + message", set(data) == set(app.PROFILE_KEYS) | {"message"}, sorted(data))
check("原子写入不留 .tmp 残留", not list(tmp.glob("*.tmp")))
check("会话列表按时间倒序", app.load_session_list() == ["2026-01-01_120000_000"], app.load_session_list())

app.st.session_state["message"] = []
app.st.session_state["nickname"] = "被覆盖"
app.load_selected_session("2026-01-01_120000_000")
check("读取会话恢复消息", app.st.session_state["message"] == [{"role": "user", "content": "你好"}])
check("读取会话恢复人设", app.st.session_state["nickname"] == app.DEFAULT_PROFILE["nickname"])

# 只写了部分字段的存档：缺失项应回填默认值
partial = tmp / "2000-01-01_000000_000.json"
partial.write_text(json.dumps({"message": [], "nickname": "旧昵称"}, ensure_ascii=False), encoding="utf-8")
app.load_selected_session("2000-01-01_000000_000")
check("部分字段的存档能读出昵称", app.st.session_state["nickname"] == "旧昵称", app.st.session_state["nickname"])
check("缺失的人设字段回填默认值", app.st.session_state["nature"] == app.DEFAULT_PROFILE["nature"])
check("多会话按时间倒序", app.load_session_list() == ["2026-01-01_120000_000", "2000-01-01_000000_000"], app.load_session_list())

check("合法会话名可解析", app._safe_session_path("2026-01-01_120000_000").name == "2026-01-01_120000_000.json")
for bad in ("../../evil", "a/b", "", "..\\evil"):
    try:
        app._safe_session_path(bad)
        check(f"拦截路径穿越 {bad!r}", False)
    except ValueError:
        check(f"拦截路径穿越 {bad!r}", True)

# --------------------------------------------------------------------------- #
# 上下文截断与 system prompt
# --------------------------------------------------------------------------- #
app.st.session_state["message"] = [{"role": "user", "content": "x" * 100} for _ in range(200)]
trimmed = app.trim_context(app.st.session_state["message"])
check("按条数截断", len(trimmed) <= app.MAX_CONTEXT_MESSAGES, len(trimmed))
check("保留最新一条", trimmed[-1] is app.st.session_state["message"][-1])
check("按字符预算截断", sum(len(m["content"]) for m in trimmed) <= app.MAX_CONTEXT_CHARS)
check("截断后顺序仍是从旧到新", trimmed == app.st.session_state["message"][-len(trimmed):])
short = [{"role": "user", "content": "a"}]
check("短对话不被修改", app.trim_context(short) == short)

app.st.session_state.update(app.DEFAULT_PROFILE)
app.st.session_state["message"] = [{"role": "user", "content": "hi"}]
built = app.build_messages()
check("build_messages 首位是 system", built[0]["role"] == "system")
check("system prompt 注入昵称", app.DEFAULT_PROFILE["nickname"] in built[0]["content"], built[0]["content"][:30])
check("system prompt 含性格", app.DEFAULT_PROFILE["nature"] in built[0]["content"])
check("system prompt 含角色简介", app.DEFAULT_PROFILE["role_description"] in built[0]["content"])
check("历史消息跟随 system 之后", built[1:] == [{"role": "user", "content": "hi"}])
check("system prompt 无残留缩进", "\n                " not in built[0]["content"])

# --------------------------------------------------------------------------- #
# 删除会话
# --------------------------------------------------------------------------- #
app.st.session_state["current_session"] = "2026-01-01_120000_000"
app.delete_session("2026-01-01_120000_000")
check("删除后文件消失", not saved.exists())
check("删除当前会话后换新 ID", app.st.session_state["current_session"] != "2026-01-01_120000_000")
check("删除当前会话后清空消息", app.st.session_state["message"] == [])
check("删除当前会话后重置人设", app.st.session_state["nickname"] == app.DEFAULT_PROFILE["nickname"])

app.st.session_state["current_session"] = "keep-me"
app.delete_session("2000-01-01_000000_000")
check("删除非当前会话不影响当前会话", app.st.session_state["current_session"] == "keep-me")
check("删除非当前会话清掉文件", not partial.exists())

# --------------------------------------------------------------------------- #
# 对话流程 render_reply（真实执行流式渲染、异常处理与落盘）
# --------------------------------------------------------------------------- #
def chunk(text=None, reasoning=None, no_choices=False, no_delta=False):
    delta = None if no_delta else type("Delta", (), {"content": text, "reasoning_content": reasoning})()
    choice = type("Choice", (), {"delta": delta})()
    return type("Chunk", (), {"choices": [] if no_choices else [choice]})()


class FakeCompletions:
    def __init__(self, chunks=None, boom=False, empty=False):
        self.calls = []
        self.chunks = chunks if chunks is not None else ["你好", "，", "人类"]
        self.boom = boom
        self.empty = empty

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.boom:
            raise RuntimeError("fake network error")
        if self.empty:
            return iter([])
        return iter(self.chunks if all(hasattr(c, "choices") for c in self.chunks)
                    else [chunk(t) for t in self.chunks])


class FakeClient:
    def __init__(self, **kwargs):
        self.chat = type("Chat", (), {"completions": kwargs.pop("completions")})()


def fresh_state(**overrides):
    app.st.session_state = _State()
    app.st.session_state.update(app.DEFAULT_PROFILE)
    app.st.session_state.update(app.DEFAULT_ADVANCED)
    app.st.session_state["thinking"] = False
    app.st.session_state["message"] = [{"role": "user", "content": "你好"}]
    app.st.session_state["current_session"] = "2026-01-01_120000_000"
    for key, value in overrides.items():
        app.st.session_state[key] = value


fresh_state()
completions = FakeCompletions()
app.client = FakeClient(completions=completions)
app.render_reply(_Sink())

check("流式分片拼接为一条回答",
      app.st.session_state.message[-1] == {"role": "assistant", "content": "你好，人类"},
      app.st.session_state.message)
check("请求使用 deepseek-flash", completions.calls[-1]["model"] == "deepseek-flash")
check("请求 stream=True", completions.calls[-1]["stream"] is True)
check("请求带上了 system + 历史", completions.calls[-1]["messages"][1] == {"role": "user", "content": "你好"})
check("思考关闭时不发 reasoning_effort", "reasoning_effort" not in completions.calls[-1])
check("思考关闭时 thinking=disabled", completions.calls[-1]["extra_body"] == {"thinking": {"type": "disabled"}})
check("思考关闭时不渲染思考折叠面板", expanders.by_label("🤔 思考过程") == [], [s.label for s in expanders.sinks])
check("回答后自动落盘", (tmp / "2026-01-01_120000_000.json").exists())
check("落盘内容包含新回答",
      json.loads((tmp / "2026-01-01_120000_000.json").read_text(encoding="utf-8"))["message"][-1]["content"] == "你好，人类")

app.st.session_state["thinking"] = True
app.render_reply(_Sink())
check("思考开启时 reasoning_effort=low", completions.calls[-1]["reasoning_effort"] == "low")
check("思考开启时 thinking=enabled", completions.calls[-1]["extra_body"] == {"thinking": {"type": "enabled"}})
check("思考开启时渲染思考折叠面板", len(expanders.by_label("🤔 思考过程")) == 1,
      [s.label for s in expanders.sinks])

# --------------------------------------------------------------------------- #
# 深度思考：推理内容流式进入折叠面板，并随回答一起保存
# --------------------------------------------------------------------------- #
expanders.sinks.clear()
fresh_state(thinking=True)
reasoning_chunks = [
    chunk(reasoning="先看他问的是什么。"),
    chunk(reasoning="嗯，得用第一人称回答。"),
    chunk(text="我"),
    chunk(text="在。"),
]
app.client = FakeClient(completions=FakeCompletions(chunks=reasoning_chunks))
app.render_reply(_Sink())

reply = app.st.session_state.message[-1]
check("推理内容被记录下来", reply.get("reasoning_content") == "先看他问的是什么。嗯，得用第一人称回答。", reply)
check("正文不混入推理内容", reply["content"] == "我在。", reply)
blocks = expanders.by_label("🤔 思考过程")
check("思考过程渲染在折叠面板里", len(blocks) == 1, [s.label for s in expanders.sinks])
check("思考中自动展开过", blocks and True in blocks[0].states, blocks[0].states if blocks else None)
check("流式结束后保持展开（不强制折叠）", blocks and blocks[0].expanded is True, blocks[0].states if blocks else None)
check("思考面板带 key（用户手动开合会被记住）", blocks and blocks[0].key == "reasoning_panel",
      blocks[0].key if blocks else None)
# 模拟用户手动折叠：on_change 回调把组件状态镜像到状态键
app.st.session_state["reasoning_panel"] = False
app._remember_expander("reasoning_panel", "show_reasoning")
check("用户手动折叠后被记住", app.st.session_state["show_reasoning"] is False, app.st.session_state["show_reasoning"])
check("推理内容随会话落盘",
      json.loads((tmp / "2026-01-01_120000_000.json").read_text(encoding="utf-8"))["message"][-1].get("reasoning_content")
      == "先看他问的是什么。嗯，得用第一人称回答。")

# 带推理的历史回放：只对含推理的回答开折叠面板
expanders.sinks.clear()
app.st.session_state.message = [
    {"role": "user", "content": "问题"},
    {"role": "assistant", "content": "带推理的回答", "reasoning_content": "推理过程"},
    {"role": "assistant", "content": "不带推理的回答"},
]
app.render_history()
check("回放时只对含推理的回答生成折叠面板", len(expanders.sinks) == 1 and expanders.sinks[0].label == "🤔 思考过程",
      [s.label for s in expanders.sinks])
check("回放时思考面板默认折叠", expanders.sinks and expanders.sinks[0].states == [False],
      expanders.sinks[0].states if expanders.sinks else None)
check("回放的面板各有独立 key", expanders.sinks and expanders.sinks[0].key == "history_reasoning_1",
      expanders.sinks[0].key if expanders.sinks else None)

# 带推理的历史会随请求回传给 API（官方示例的用法）
app.st.session_state.message = [
    {"role": "user", "content": "问题"},
    {"role": "assistant", "content": "回答", "reasoning_content": "推理"},
]
sent = app.build_messages()
check("对话历史带上 reasoning_content", sent[-1].get("reasoning_content") == "推理", sent[-1])
check("普通消息不带 reasoning_content", "reasoning_content" not in sent[1], sent[1])

# 异常：记录 st.error，不写入 assistant 消息，页面不抛异常
errors.calls.clear()
fresh_state()
app.st.session_state["message"] = [{"role": "user", "content": "会失败"}]
app.client = FakeClient(completions=FakeCompletions(boom=True))
app.render_reply(_Sink())
check("接口异常被捕获为 st.error", errors.calls and "fake network error" in errors.calls[-1], errors.calls[-1:])
check("异常时不写入 assistant 消息", app.st.session_state.message == [{"role": "user", "content": "会失败"}])

# 空回答：给出 warning，同样不写入历史
warnings.calls.clear()
fresh_state()
app.st.session_state["current_session"] = "2026-01-01_130000_000"
app.st.session_state["message"] = [{"role": "user", "content": "空回答"}]
app.client = FakeClient(completions=FakeCompletions(empty=True))
app.render_reply(_Sink())
check("空回答给出 st.warning", warnings.calls and "没有返回" in warnings.calls[-1], warnings.calls[-1:])
check("空回答不写入历史", app.st.session_state.message == [{"role": "user", "content": "空回答"}])

# --------------------------------------------------------------------------- #
# 高级配置：请求参数是否按配置拼装
# --------------------------------------------------------------------------- #
fresh_state()
payload = app.build_request_payload()
check("默认带上 temperature=1.0", payload["temperature"] == 1.0, payload.get("temperature"))
check("默认带上 top_p=1.0", payload["top_p"] == 1.0, payload.get("top_p"))
check("默认不发送 max_tokens", "max_tokens" not in payload)
check("惩罚项为 0 时不发送", "frequency_penalty" not in payload and "presence_penalty" not in payload)
check("system prompt 在 payload 里", payload["messages"][0]["role"] == "system")

fresh_state(temperature=0.3, top_p=0.8, limit_tokens=True, max_tokens=1024,
            frequency_penalty=0.5, presence_penalty=-0.2)
payload = app.build_request_payload()
check("自定义温度生效", payload["temperature"] == 0.3)
check("自定义 top_p 生效", payload["top_p"] == 0.8)
check("勾选后发送 max_tokens", payload["max_tokens"] == 1024)
check("非零 frequency_penalty 生效", payload["frequency_penalty"] == 0.5)
check("非零 presence_penalty 生效", payload["presence_penalty"] == -0.2)

fresh_state(limit_tokens=False, max_tokens=1024)
check("取消勾选后不发送 max_tokens", "max_tokens" not in app.build_request_payload())

fresh_state(temperature=0.5)
app.client = FakeClient(completions=FakeCompletions())
app.render_reply(_Sink())
check("高级参数真正传给了模型调用", app.client.chat.completions.calls[-1]["temperature"] == 0.5)

# --------------------------------------------------------------------------- #
# 会话 ID 与源码层面的重复度
# --------------------------------------------------------------------------- #
ids = {app.new_session_id() for _ in range(300)}
# Windows 上 datetime.now() 的真实粒度约 15ms，连续调用必然重号；
# 这里只记录观测值，真正要保证的是"相邻两次点击不会拿到同一个 ID"。
print(f"NOTE  300 次连续调用得到 {len(ids)} 个不同 ID（时钟粒度限制，非缺陷）")
first = app.new_session_id()
time.sleep(0.05)
second = app.new_session_id()
check("间隔 50ms 的两次会话 ID 不同", first != second, f"{first} vs {second}")
check("会话 ID 可被 strptime 解析", datetime.datetime.strptime(first, "%Y-%m-%d_%H%M%S_%f"))

src = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")
for key in ("nature", "output_rules"):
    check(f"默认 {key} 字面量只出现一次", src.count(app.DEFAULT_PROFILE[key]) == 1, src.count(app.DEFAULT_PROFILE[key]))
# role_description 在源码里是跨行隐式拼接的，按片段校验唯一性
for fragment in app.DEFAULT_PROFILE["role_description"].split("，"):
    check(f"role_description 片段只出现一次 {fragment[:12]}", src.count(fragment) == 1, src.count(fragment))
check("昵称字段已统一为 nickname", "nickname" in src and "nike_name" not in src)
check("不再保留旧的迁移映射", "LEGACY_KEY_MAP" not in src)
check("使用 Path 解析脚本目录", "Path(__file__).resolve().parent" in src)
check("os.replace 原子落盘", "os.replace" in src)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
