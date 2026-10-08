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

from _harness import PROJECT, TMP_DIR, real_png  # noqa: F401  统一准备临时目录与环境变量

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


_placeholder_log = []


class _Placeholder(_Sink):
    """模拟 st.empty() 返回的占位符：记录写进去的内容，便于断言中间态。"""

    def __init__(self):
        self.markdowns = []
        _placeholder_log.append(self)

    def markdown(self, body="", *args, **kwargs):
        self.markdowns.append(body)
        return self

    def last(self):
        return self.markdowns[-1] if self.markdowns else None


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
              "popover", "text"):
    setattr(fake_st, _name, _Sink())
# st.empty() 返回可记录内容的占位符，用于断言流式中间态（如"思考中"微光提示）
fake_st.empty = lambda *a, **k: _Placeholder()


def _fake_write_stream(stream, *args, **kwargs):
    """模拟 st.write_stream：消费生成器并返回拼接后的文本（字符串流时如此）。"""
    pieces = [piece for piece in stream if isinstance(piece, str)]
    return "".join(pieces)


fake_st.write_stream = _fake_write_stream
# 逻辑测试不跑页面分支：让输入类控件返回 None（在 streamlit 里就是"没有输入"）
# 但带 key 的输入控件会按 streamlit 的行为把值写进 session_state，
# 否则 on_click 回调里从 session_state[input_key] 取值会拿到 None（编辑消息就是这样）
def _state_writing(widget):
    def call(*args, **kwargs):
        key = kwargs.get("key")
        if key is not None:
            fake_st.session_state[key] = kwargs.get("value")
        return None
    return call


fake_st.chat_input = _state_writing("chat_input")
fake_st.toggle = _state_writing("toggle")
fake_st.text_input = _state_writing("text_input")
fake_st.text_area = _state_writing("text_area")

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
for _f in list(tmp.glob("*.json")):
    _f.unlink()
for _d in list(tmp.glob("*/")):
    if _d.name != "drafts":
        import shutil as _shutil

        _shutil.rmtree(_d, ignore_errors=True)
app.ARCHIVE_DIR = tmp


def meta_of(session_id, branch="main"):
    """读某个会话的 meta.json。"""
    return tmp / session_id / "meta.json"


def msgs_of(session_id, branch="main"):
    """读某条分支的消息列表（新结构：branches/<分支>.json）。"""
    return json.loads((tmp / session_id / "branches" / f"{branch}.json").read_text(encoding="utf-8"))["message"]


def branch_files(session_id):
    return list((tmp / session_id / "branches").glob("*.json"))


def session_dirs():
    """已落盘的会话目录（排除 drafts 等内部目录）。"""
    return sorted(
        d.name for d in tmp.iterdir()
        if d.is_dir() and d.name != "drafts"
    )


def _clear_sessions():
    """清空所有会话（新结构目录 + 旧扁平文件），草稿目录保留。"""
    import shutil as _shutil

    for _f in list(tmp.glob("*.json")):
        _f.unlink()
    for _d in tmp.iterdir():
        if _d.is_dir() and _d.name != "drafts":
            _shutil.rmtree(_d, ignore_errors=True)


app.st.session_state = _State()
app.st.session_state.update(app.DEFAULT_PROFILE)
app.st.session_state.update(app.DEFAULT_ADVANCED)
app.st.session_state["thinking"] = False
app.st.session_state["message"] = [{"role": "user", "content": "你好"}]
app.st.session_state["current_session"] = "2026-01-01_120000_000"
app.st.session_state["session_title"] = "第一次聊天"
app.st.session_state["current_branch"] = "main"

app.save_session()
sid = "2026-01-01_120000_000"
saved = tmp / sid / "meta.json"
branch_file = tmp / sid / "branches" / "main.json"
check("会话存档写入成功（目录 + meta.json）", saved.exists(), str(saved))
check("消息写入当前分支文件", branch_file.exists(), str(branch_file))
data = json.loads(saved.read_text(encoding="utf-8"))
branch_data = json.loads(branch_file.read_text(encoding="utf-8"))
check("meta.json 含名称/置顶/人设/高级参数/分支指针",
      set(data) >= set(app.PROFILE_KEYS) | set(app.ADVANCED_KEYS) | {"title", "pinned", "current_branch", "branches"},
      sorted(data))
check("分支文件只有消息", set(branch_data) == {"message"}, sorted(branch_data))
check("分支指针指向 main", data["current_branch"] == "main", data.get("current_branch"))
check("分支列表含 main", [b["id"] for b in data["branches"]] == ["main"], data.get("branches"))
check("会话目录名就是会话 ID", saved.parent.name == sid, saved.parent.name)
check("原子写入不留 .tmp 残留", not list(tmp.rglob("*.tmp*")), [p.name for p in tmp.rglob("*.tmp*")])
check("会话列表按时间倒序", app.load_session_list() == [sid], app.load_session_list())

# 自定义名称：显示用名称与文件名解耦
check("会话名称被写入存档", data["title"] == "第一次聊天", data.get("title"))
check("读回自定义会话名称", app.session_title(sid) == "第一次聊天")
check("默认不置顶", data["pinned"] is False, data.get("pinned"))
check("会话元信息含昵称与置顶", app.load_session_meta(sid)["nickname"] == app.DEFAULT_PROFILE["nickname"])
check("会话元信息含当前分支", app.load_session_meta(sid)["current_branch"] == "main")

app.st.session_state["message"] = []
app.st.session_state["nickname"] = "被覆盖"
app.st.session_state["session_title"] = "被覆盖"
app.load_selected_session(sid)
check("读取会话恢复消息", app.st.session_state["message"] == [{"role": "user", "content": "你好"}])
check("读取会话恢复人设", app.st.session_state["nickname"] == app.DEFAULT_PROFILE["nickname"])
check("读取会话恢复名称", app.st.session_state["session_title"] == "第一次聊天", app.st.session_state["session_title"])
check("读取会话恢复当前分支", app.st.session_state["current_branch"] == "main")

# 高级生成参数随会话存档：调过之后再切回来，设置还在
app.st.session_state.update({"temperature": 0.35, "top_p": 0.7, "limit_tokens": True,
                            "max_tokens": 1024, "frequency_penalty": 0.5, "presence_penalty": -0.3})
app.save_session()
saved_advanced = json.loads(saved.read_text(encoding="utf-8"))
check("高级参数写入存档", saved_advanced["temperature"] == 0.35 and saved_advanced["max_tokens"] == 1024,
      {k: saved_advanced.get(k) for k in app.ADVANCED_KEYS})
app._reset_advanced()
check("重置后回到默认值", app.st.session_state["temperature"] == app.DEFAULT_ADVANCED["temperature"])
app.load_selected_session(sid)
check("切回会话恢复温度", app.st.session_state["temperature"] == 0.35, app.st.session_state["temperature"])
check("切回会话恢复 top_p", app.st.session_state["top_p"] == 0.7, app.st.session_state["top_p"])
check("切回会话恢复惩罚项", app.st.session_state["frequency_penalty"] == 0.5)
check("切回会话恢复 max_tokens", app.st.session_state["max_tokens"] == 1024)
check("切回会话恢复长度开关", app.st.session_state["limit_tokens"] is True)
# 改名/置顶走的是"读出来改字段再写回"，不能把高级参数弄丢
app.rename_session(sid, "改名后")
after_rename = json.loads(saved.read_text(encoding="utf-8"))
check("改名不影响已存的高级参数", after_rename["temperature"] == 0.35 and after_rename["max_tokens"] == 1024,
      {k: after_rename.get(k) for k in app.ADVANCED_KEYS})
check("改名不动分支文件", json.loads(branch_file.read_text(encoding="utf-8"))["message"]
      == [{"role": "user", "content": "你好"}])
app.set_pinned(sid, True)
after_pin = json.loads(saved.read_text(encoding="utf-8"))
check("置顶不影响已存的高级参数", after_pin["temperature"] == 0.35, after_pin.get("temperature"))
app.set_pinned(sid, False)  # 还原，避免影响后面的排序断言
# 旧扁平存档（只有部分字段）也要能读：视为单分支 main，缺失项回填默认值
partial_id = "2000-01-01_000000_000"
partial = tmp / f"{partial_id}.json"
partial.write_text(json.dumps({"message": [], "nickname": "旧昵称"}, ensure_ascii=False), encoding="utf-8")
app.load_selected_session(partial_id)
check("旧扁平存档能读出昵称", app.st.session_state["nickname"] == "旧昵称", app.st.session_state["nickname"])
check("缺失的人设字段回填默认值", app.st.session_state["nature"] == app.DEFAULT_PROFILE["nature"])
check("旧扁平存档回退成单分支 main", app.st.session_state["current_branch"] == "main")
check("没有 title 的老存档回退成会话 ID", app.session_title(partial_id) == partial_id,
      app.session_title(partial_id))
check("读旧存档不会改动磁盘（只读兼容）", partial.exists())

# --------------------------------------------------------------------------- #
# 旧扁平存档 → 新结构：首次保存时自动迁移
# --------------------------------------------------------------------------- #
mig_id = "2001-02-03_040506_000"
(tmp / f"{mig_id}.json").write_text(json.dumps({
    "title": "迁移测试",
    "pinned": True,
    "message": [{"role": "user", "content": "旧消息"}, {"role": "assistant", "content": "旧回答"}],
    "nickname": "旧昵称",
    "temperature": 0.4,
}, ensure_ascii=False), encoding="utf-8")
app.migrate_session(mig_id)
check("迁移后旧文件被移除", not (tmp / f"{mig_id}.json").exists())
check("迁移后建立 meta.json", meta_of(mig_id).exists())
check("迁移后建立 main 分支文件", (tmp / mig_id / "branches" / "main.json").exists())
mig_meta = json.loads(meta_of(mig_id).read_text(encoding="utf-8"))
check("迁移保留会话名称", mig_meta["title"] == "迁移测试", mig_meta.get("title"))
check("迁移保留置顶", mig_meta["pinned"] is True, mig_meta.get("pinned"))
check("迁移保留人设", mig_meta.get("nickname") == "旧昵称", mig_meta.get("nickname"))
check("迁移保留高级参数", mig_meta.get("temperature") == 0.4, mig_meta.get("temperature"))
check("迁移后的消息进入 main 分支", msgs_of(mig_id) == [{"role": "user", "content": "旧消息"},
                                                        {"role": "assistant", "content": "旧回答"}])
check("迁移后分支指针是 main", mig_meta["current_branch"] == "main")
check("迁移后出现在会话列表里", mig_id in app.load_session_list(), app.load_session_list())
check("迁移是幂等的（再跑一次不出错）", app.migrate_session(mig_id) is None)

# --------------------------------------------------------------------------- #
# 分支读写：第 2 层的分支操作建立在这几个原语上
# --------------------------------------------------------------------------- #
check("分支列表初始只有 main", app.branch_ids(mig_id) == ["main"], app.branch_ids(mig_id))
check("读不存在的分支得到空消息", app.read_branch(mig_id, "b9") == {"message": []})
app.write_branch(mig_id, "b2", {"message": [{"role": "user", "content": "另一条分支"}]})
check("写分支后文件出现", (tmp / mig_id / "branches" / "b2.json").exists())
check("两条分支各自独立", msgs_of(mig_id, "b2") == [{"role": "user", "content": "另一条分支"}]
      and msgs_of(mig_id, "main")[0]["content"] == "旧消息")
check("分支文件只有 message 字段",
      set(json.loads((tmp / mig_id / "branches" / "b2.json").read_text(encoding="utf-8"))) == {"message"})

# meta 落盘时不能把内部兼容字段写进去
app.write_session_meta(mig_id, {"title": "写回测试", "_legacy": True, "_legacy_data": {"x": 1},
                                "current_branch": "main", "branches": [{"id": "main"}]})
raw_meta = json.loads(meta_of(mig_id).read_text(encoding="utf-8"))
check("meta 落盘不写内部字段", not any(k.startswith("_") for k in raw_meta), sorted(raw_meta))
check("meta 落盘保留 title", raw_meta["title"] == "写回测试", raw_meta.get("title"))
check("分支表兼容字符串形式",
      [b["id"] for b in app.normalize_branches(["main", "b2"])] == ["main", "b2"])

# 旧文件优先于新建的旧文件名：迁移完成后读的是新结构
check("迁移后 load_current_branch 读的是分支文件",
      app.load_current_branch(mig_id)["message"][0]["content"] == "旧消息")

# 删除会话：目录、分支、草稿一起清掉
app.st.session_state["current_session"] = "2002-03-04_050607_000"
app.st.session_state["message"] = []
app.save_session()
draft_of_deleted = app.draft_path("2002-03-04_050607_000")
check("草稿已建立", draft_of_deleted.exists(), str(draft_of_deleted))
app.st.session_state["current_session"] = "keep-after-delete"
app.delete_session(mig_id)
check("删除会话后目录连同分支一起消失", not (tmp / mig_id).exists())
app.delete_session("2002-03-04_050607_000")
check("删除会话时草稿一起清掉", not draft_of_deleted.exists(), str(draft_of_deleted))

# --------------------------------------------------------------------------- #
# 分支操作：新建 / 切换 / 改名 / 删除
# --------------------------------------------------------------------------- #
bsid = "2010-01-01_000000_000"
app.st.session_state["current_session"] = bsid
app.st.session_state["session_title"] = "分支测试"
app.st.session_state["current_branch"] = "main"
app.st.session_state["message"] = [{"role": "user", "content": "问题"},
                                   {"role": "assistant", "content": "回答 A"}]
app.save_session()
check("分支会话建立", app.branch_ids(bsid) == ["main"], app.branch_ids(bsid))

# fork：从"第 2 条之前"分叉，带一条 seed（编辑消息的场景）
new_branch = app.fork_branch(bsid, prefix=[{"role": "user", "content": "问题"}],
                             seed={"role": "assistant", "content": "回答 B"},
                             parent="main", fork_index=1)
check("新分支 ID 自动递增", new_branch == "b2", new_branch)
check("新分支出现在分支表里", app.branch_ids(bsid) == ["main", "b2"], app.branch_ids(bsid))
check("新分支写入自己的文件", msgs_of(bsid, "b2") == [{"role": "user", "content": "问题"},
                                                      {"role": "assistant", "content": "回答 B"}])
check("原分支消息未被改动", msgs_of(bsid, "main")[1]["content"] == "回答 A")
check("fork 后当前分支切到新分支",
      json.loads(meta_of(bsid).read_text(encoding="utf-8"))["current_branch"] == "b2")
check("分支记下父分支与分叉点",
      [(b["parent"], b["fork_index"]) for b in app.load_session_meta(bsid)["branches"] if b["id"] == "b2"]
      == [("main", 1)])
check("兄弟分支识别", app.branch_siblings(bsid, "b2") == ["b2"], app.branch_siblings(bsid, "b2"))

# 再 fork 一条同源分支 → 成为兄弟
third = app.fork_branch(bsid, prefix=[{"role": "user", "content": "问题"}],
                        seed={"role": "assistant", "content": "回答 C"},
                        parent="main", fork_index=1)
check("第三条分支 ID 继续递增", third == "b3", third)
check("同源分支互为兄弟", sorted(app.branch_siblings(bsid, "b2")) == ["b2", "b3"],
      app.branch_siblings(bsid, "b2"))

# 切换分支：写磁盘指针（内存里的消息由渲染时"以磁盘为准"重载，见下面"重新进入会话"那组断言）
app.st.session_state["current_session"] = bsid
app.st.session_state["current_branch"] = "main"
app.st.session_state["message"] = msgs_of(bsid, "main")
app.switch_branch(bsid, "b3")
check("切换后状态记录当前分支", app.st.session_state["current_branch"] == "b3")
check("切换后磁盘指针也更新",
      json.loads(meta_of(bsid).read_text(encoding="utf-8"))["current_branch"] == "b3")
check("切换目标分支的消息仍在磁盘上", msgs_of(bsid, "b3")[1]["content"] == "回答 C")
check("切换不存在的分支会被拒绝", (lambda: (app.switch_branch(bsid, "nope"),
                                      app.load_session_meta(bsid)["current_branch"] == "b3")[1])())

# 改名：分支文件跟着重命名，指针同步
app.st.session_state["current_branch"] = "b3"
app.rename_branch(bsid, "b3", "毒舌版")
check("改名后分支表更新", app.branch_ids(bsid) == ["main", "b2", "毒舌版"], app.branch_ids(bsid))
check("改名后旧文件消失", not (tmp / bsid / "branches" / "b3.json").exists())
check("改名后新文件出现且内容不变", msgs_of(bsid, "毒舌版")[1]["content"] == "回答 C")
check("改名后指针跟着改",
      json.loads(meta_of(bsid).read_text(encoding="utf-8"))["current_branch"] == "毒舌版")
check("改名后内存状态跟着改", app.st.session_state["current_branch"] == "毒舌版")
old_state = app.st.session_state["current_branch"]
app.rename_branch(bsid, "b2", "main")  # 重名应被拒绝
check("分支改名不能重名", app.branch_ids(bsid) == ["main", "b2", "毒舌版"], app.branch_ids(bsid))
check("分支改名不能带路径", (lambda: (app.rename_branch(bsid, "b2", "../evil"),
                                 app.branch_ids(bsid) == ["main", "b2", "毒舌版"])[1])())

# 删除：删当前分支要跳到父分支；只剩一条时拒绝
app.delete_branch(bsid, "毒舌版")
check("删除当前分支后跳回父分支",
      json.loads(meta_of(bsid).read_text(encoding="utf-8"))["current_branch"] == "main",
      json.loads(meta_of(bsid).read_text(encoding="utf-8")).get("current_branch"))
check("删除后分支文件消失", not (tmp / bsid / "branches" / "毒舌版.json").exists())
check("删除后状态同步", app.st.session_state["current_branch"] == "main")
app.delete_branch(bsid, "b2")
check("删到只剩 main", app.branch_ids(bsid) == ["main"], app.branch_ids(bsid))
app.delete_branch(bsid, "main")
check("只剩一条分支时拒绝删除", app.branch_ids(bsid) == ["main"], app.branch_ids(bsid))

# 删父分支：子分支改挂到祖父上（不能断链）
sub_parent = app.fork_branch(bsid, prefix=[{"role": "user", "content": "分支上的问题"}],
                             seed={"role": "assistant", "content": "分支上的回答"},
                             parent="main", fork_index=1)
sub_child = app.fork_branch(bsid, prefix=msgs_of(bsid, sub_parent),
                            seed={"role": "assistant", "content": "更深一层"},
                            parent=sub_parent, fork_index=2)
app.delete_branch(bsid, sub_parent)
deep = [b for b in app.load_session_meta(bsid)["branches"] if b["id"] == sub_child]
check("删父分支后子分支改挂到祖父", deep and deep[0]["parent"] == "main", deep)
check("删父分支后子分支文件仍在", (tmp / bsid / "branches" / f"{sub_child}.json").exists())

# 分支 ID 只增不减：删掉中间一条后，新分支不会复用那个编号
# （复用会让"ID 大小"和"创建先后"对不上，列表看起来像乱序）
id_test = "2011-11-11_000000_000"
app.st.session_state["current_session"] = id_test
app.st.session_state["current_branch"] = "main"
app.st.session_state["message"] = [{"role": "user", "content": "问题"}]
app.save_session()
first = app.fork_branch(id_test, prefix=[], seed=None, parent="main", fork_index=0)   # b2
second = app.fork_branch(id_test, prefix=[], seed=None, parent="main", fork_index=0)  # b3
check("新分支 ID 从 b2 开始递增", (first, second) == ("b2", "b3"), (first, second))
app.delete_branch(id_test, first)  # 删掉 b2，留下空缺
third = app.fork_branch(id_test, prefix=[], seed=None, parent="main", fork_index=0)
check("删掉中间分支后 ID 不复用（只增不减）", third == "b4", third)
check("分支列表按创建时间排（main 在最前）",
      app.branch_ids(id_test) == ["main", second, third], app.branch_ids(id_test))
# 手动把存档里的顺序打乱（模拟"ID 与创建先后不一致"的存档）：读出来仍应按创建时间排
meta_shuffled = app.load_session_meta(id_test)
meta_shuffled["branches"] = [
    {"id": "b9", "parent": "main", "fork_index": 0, "created_at": "2030-01-01T00:00:00"},
    {"id": "b2", "parent": "main", "fork_index": 0, "created_at": "2020-01-01T00:00:00"},
]
app.write_session_meta(id_test, meta_shuffled)
check("存档里 ID 乱序时按创建时间重排",
      app.branch_ids(id_test) == ["b2", "b9"], app.branch_ids(id_test))
app.delete_session(id_test)

# 「再次进入会话时进入正确分支」：把指针指到非 main 的那条，重新载入会话
target = app.branch_ids(bsid)[-1]
meta_now = app.load_session_meta(bsid)
meta_now["current_branch"] = target
app.write_session_meta(bsid, meta_now)
app.st.session_state["current_session"] = "some-other-session"
app.st.session_state["current_branch"] = "main"
app.st.session_state["message"] = []
app.load_selected_session(bsid)
check("重新进入会话时读取的是存档里标注的当前分支",
      app.st.session_state["current_branch"] == target, app.st.session_state["current_branch"])
check("重新进入会话时载入该分支的消息",
      app.st.session_state["message"] == msgs_of(bsid, target), app.st.session_state["message"])
check("重新进入会话时标记消息所属分支",
      app.st.session_state["_message_branch"] == target, app.st.session_state.get("_message_branch"))

# 清理掉这个测试会话，避免影响后面按会话列表排序的断言
app.delete_session(bsid)
app.st.session_state["current_session"] = "2026-01-01_120000_000"
check("分支测试会话已清理", not (tmp / bsid).exists())

blank_title = tmp / "3000-01-01_000000_000.json"
blank_title.write_text(json.dumps({"title": "  "}), encoding="utf-8")
check("空 title 也回退成会话 ID", app.session_title("3000-01-01_000000_000") == "3000-01-01_000000_000",
      app.session_title("3000-01-01_000000_000"))
check("不存在的会话名回退成自身", app.session_title("no-such-session") == "no-such-session")
# 当前会话在磁盘上，因此正常出现在列表里
check("多会话按时间倒序", app.load_session_list() == ["3000-01-01_000000_000", "2026-01-01_120000_000", "2000-01-01_000000_000"],
      app.load_session_list())
blank_title.unlink()

# 新开、还没发言的会话（没有文件）不应出现在会话历史里
app.st.session_state["current_session"] = "2999-12-31_235959_999"
check("未落盘的新会话不出现在会话历史里",
      app.load_session_list() == ["2026-01-01_120000_000", "2000-01-01_000000_000"],
      app.load_session_list())
check("未落盘的新会话没有目录", not (tmp / "2999-12-31_235959_999").exists())

check("合法会话名可解析", app._session_dir("2026-01-01_120000_000").name == "2026-01-01_120000_000")
check("合法分支名可解析", app.branch_path("2026-01-01_120000_000", "b2").name == "b2.json")
for bad in ("../../evil", "a/b", "", "..\\evil"):
    try:
        app._session_dir(bad)
        check(f"拦截非法会话名 {bad!r}", False)
    except ValueError:
        check(f"拦截非法会话名 {bad!r}", True)
    try:
        app.branch_path("2026-01-01_120000_000", bad)
        check(f"拦截非法分支名 {bad!r}", False)
    except ValueError:
        check(f"拦截非法分支名 {bad!r}", True)

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
app.st.session_state["session_title"] = "第一次聊天"
app.delete_session("2026-01-01_120000_000")
check("删除后会话目录消失", not saved.exists())
check("删除后分支文件一起消失", not (tmp / "2026-01-01_120000_000").exists())
check("删除当前会话后换新 ID", app.st.session_state["current_session"] != "2026-01-01_120000_000")
check("删除当前会话后清空消息", app.st.session_state["message"] == [])
check("删除当前会话后重置人设", app.st.session_state["nickname"] == app.DEFAULT_PROFILE["nickname"])
check("删除当前会话后重置名称", app.st.session_state["session_title"] == app.DEFAULT_SESSION_TITLE,
      app.st.session_state["session_title"])
check("删除当前会话后分支回到 main", app.st.session_state["current_branch"] == "main",
      app.st.session_state["current_branch"])

app.st.session_state["current_session"] = "keep-me"
app.st.session_state["session_title"] = "保留的名字"
app.delete_session("2000-01-01_000000_000")
check("删除非当前会话不影响当前会话", app.st.session_state["current_session"] == "keep-me")
check("删除非当前会话不影响当前名称", app.st.session_state["session_title"] == "保留的名字")
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
check("思考关闭时不渲染思考折叠面板", expanders.by_label("思考过程") == [], [s.label for s in expanders.sinks])
check("回答后自动落盘", meta_of("2026-01-01_120000_000").exists())
check("落盘内容包含新回答",
      msgs_of("2026-01-01_120000_000")[-1]["content"] == "你好，人类")

app.st.session_state["thinking"] = True
app.render_reply(_Sink())
check("思考开启时 reasoning_effort=low", completions.calls[-1]["reasoning_effort"] == "low")
check("思考开启时 thinking=enabled", completions.calls[-1]["extra_body"] == {"thinking": {"type": "enabled"}})
check("思考开启时渲染思考折叠面板", len(expanders.by_label("思考过程")) == 1,
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
_placeholder_log.clear()  # 只统计本次 render_reply 写入的内容
app.render_reply(_Sink())
placeholder_writes = [w for ph in _placeholder_log for w in ph.markdowns]

reply = app.st.session_state.message[-1]
check("推理内容被记录下来", reply.get("reasoning_content") == "先看他问的是什么。嗯，得用第一人称回答。", reply)
check("正文不混入推理内容", reply["content"] == "我在。", reply)
blocks = expanders.by_label("思考过程")
check("思考过程渲染在折叠面板里", len(blocks) == 1, [s.label for s in expanders.sinks])
check("思考中自动展开过", blocks and True in blocks[0].states, blocks[0].states if blocks else None)
check("流式结束后保持展开（不强制折叠）", blocks and blocks[0].expanded is True, blocks[0].states if blocks else None)
check("思考面板带 key（用户手动开合会被记住）", blocks and blocks[0].key == "reasoning_panel",
      blocks[0].key if blocks else None)
check("思考开始时显示微光提示",
      any(":shimmer[思考中" in (v or "") for v in placeholder_writes), placeholder_writes[:3])
check("推理开始后微光被真实推理替换",
      any("先看他问的是什么。" in (v or "") for v in placeholder_writes), placeholder_writes)
check("推理结束后不再有微光", ":shimmer" not in (placeholder_writes[-1] or ""), placeholder_writes[-1:])
# 模拟用户手动折叠：on_change 回调把组件状态镜像到状态键
app.st.session_state["reasoning_panel"] = False
app._remember_expander("reasoning_panel", "show_reasoning")
check("用户手动折叠后被记住", app.st.session_state["show_reasoning"] is False, app.st.session_state["show_reasoning"])
check("推理内容随会话落盘",
      msgs_of("2026-01-01_120000_000")[-1].get("reasoning_content")
      == "先看他问的是什么。嗯，得用第一人称回答。")

# 带推理的历史回放：只对含推理的回答开折叠面板
expanders.sinks.clear()
app.st.session_state.message = [
    {"role": "user", "content": "问题"},
    {"role": "assistant", "content": "带推理的回答", "reasoning_content": "推理过程"},
    {"role": "assistant", "content": "不带推理的回答"},
]
app.render_history()
check("回放时只对含推理的回答生成折叠面板", len(expanders.sinks) == 1 and expanders.sinks[0].label == "思考过程",
      [s.label for s in expanders.sinks])
check("回放时思考面板默认折叠", expanders.sinks and expanders.sinks[0].states == [False],
      expanders.sinks[0].states if expanders.sinks else None)
check("回放的面板各有独立 key（带分支名与序号）",
      expanders.sinks and expanders.sinks[0].key == "history_reasoning_main_1",
      expanders.sinks[0].key if expanders.sinks else None)
check("回放面板 key 含分支名（切分支不会串位）",
      expanders.sinks and "main" in (expanders.sinks[0].key or ""),
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
# 重新生成：新建分支（旧回答保留），新分支里放新的回答
# --------------------------------------------------------------------------- #
regen_id = "2027-07-07_070707_000"
fresh_state(current_session=regen_id, current_branch="main", session_title="重新生成测试")
app.st.session_state["message"] = [{"role": "user", "content": "问题一"},
                                   {"role": "assistant", "content": "旧回答"},
                                   {"role": "user", "content": "问题二"},
                                   {"role": "assistant", "content": "旧回答二"}]
app.save_session()
check("重新生成前只有一条分支", app.branch_ids(regen_id) == ["main"], app.branch_ids(regen_id))

# 点击「重新生成」：回调只准备新分支与标记，不发请求
calls_before = len(completions.calls)
app.regenerate(3)
regen_branch = app.st.session_state["current_branch"]
check("重新生成新建了分支", regen_branch == "b2", regen_branch)
check("新分支继承被重新生成那条之前的前缀",
      msgs_of(regen_id, "b2") == [{"role": "user", "content": "问题一"},
                                  {"role": "assistant", "content": "旧回答"},
                                  {"role": "user", "content": "问题二"}],
      msgs_of(regen_id, "b2"))
check("旧分支原样保留", msgs_of(regen_id, "main")[3]["content"] == "旧回答二")
check("新分支记下分叉点", [(b["parent"], b["fork_index"]) for b in app.load_session_meta(regen_id)["branches"]
                          if b["id"] == "b2"] == [("main", 3)])
check("标记了待生成", app.st.session_state["pending_regen"] is True)
check("回调阶段没有发起请求（请求交给页面主体）", len(completions.calls) == calls_before,
      len(completions.calls) - calls_before)

# 页面主体处理标记：真正生成并写入新分支
regen_completions = FakeCompletions(chunks=["新回答"])
app.client = FakeClient(completions=regen_completions)
app.render_pending_regen()
check("生成后清除标记", app.st.session_state["pending_regen"] is False)
check("新回答追加进新分支",
      [m["content"] for m in msgs_of(regen_id, "b2")] == ["问题一", "旧回答", "问题二", "新回答"],
      msgs_of(regen_id, "b2"))
check("旧分支仍然没被动", [m["content"] for m in msgs_of(regen_id, "main")] ==
      ["问题一", "旧回答", "问题二", "旧回答二"], msgs_of(regen_id, "main"))
check("重新进入会话会回到重新生成后的分支",
      json.loads(meta_of(regen_id).read_text(encoding="utf-8"))["current_branch"] == "b2")
check("请求带上被重新生成那条之前的历史",
      [m["content"] for m in regen_completions.calls[0]["messages"][1:]] ==
      ["问题一", "旧回答", "问题二"], regen_completions.calls[0]["messages"])
check("再次重新生成会再建一条分支",
      (lambda: (app.regenerate(3), app.st.session_state["current_branch"] == "b3")[1])())
app.delete_session(regen_id)

# --------------------------------------------------------------------------- #
# 编辑消息：新建分支 + 从被编辑的位置重新生成（就像 DeepSeek 网页）
# --------------------------------------------------------------------------- #
edit_id = "2028-08-08_080808_000"
fresh_state(current_session=edit_id, current_branch="main", session_title="编辑测试")
app.st.session_state["message"] = [{"role": "user", "content": "原问题"},
                                   {"role": "assistant", "content": "原回答"}]
app.save_session()

# 进入编辑态
app.start_edit(0)
check("进入编辑态", app.st.session_state["editing_index"] == 0)
app.cancel_edit()
check("可以取消编辑", app.st.session_state["editing_index"] is None)

# 编辑用户消息 → 新分支（含改写后的用户消息）→ 生成新回答
app.st.session_state["edit_box_main_0"] = "改写后的问题"
app.submit_edit(0, "edit_box_main_0")
check("编辑用户消息后新建分支", app.st.session_state["current_branch"] == "b2",
      app.st.session_state["current_branch"])
check("新分支里是被改写后的消息", msgs_of(edit_id, "b2") == [{"role": "user", "content": "改写后的问题"}],
      msgs_of(edit_id, "b2"))
check("原分支原样保留", [m["content"] for m in msgs_of(edit_id, "main")] == ["原问题", "原回答"],
      msgs_of(edit_id, "main"))
check("编辑后退出编辑态", app.st.session_state["editing_index"] is None)
check("编辑用户消息后需要模型继续回答", app.st.session_state["pending_regen"] is True)
check("新分支记下分叉点", [(b["parent"], b["fork_index"]) for b in app.load_session_meta(edit_id)["branches"]
                          if b["id"] == "b2"] == [("main", 0)])
edit_calls = []
app.client = FakeClient(completions=FakeCompletions(chunks=["改写后的新回答"]))
app.render_pending_regen()
check("生成的新回答追加在改写后的消息之后",
      [m["content"] for m in msgs_of(edit_id, "b2")] == ["改写后的问题", "改写后的新回答"],
      msgs_of(edit_id, "b2"))
check("编辑后原分支仍然只有两条", len(msgs_of(edit_id, "main")) == 2)
check("编辑产生的分支被记为当前分支",
      json.loads(meta_of(edit_id).read_text(encoding="utf-8"))["current_branch"] == "b2")

# 编辑 AI 回复 → 新分支里放改写后的回答，且不再额外生成
app.st.session_state["edit_box_b2_1"] = "我手写的回答"
app.submit_edit(1, "edit_box_b2_1")
check("编辑 AI 回复后新建分支", app.st.session_state["current_branch"] == "b3",
      app.st.session_state["current_branch"])
check("新分支 = 前缀 + 改写后的回答",
      [m["content"] for m in msgs_of(edit_id, "b3")] == ["改写后的问题", "我手写的回答"],
      msgs_of(edit_id, "b3"))
check("编辑 AI 回复后不再触发生成", app.st.session_state["pending_regen"] is False)
check("改写的 AI 回复不带旧推理内容", "reasoning_content" not in msgs_of(edit_id, "b3")[1],
      msgs_of(edit_id, "b3")[1])
check("b2 分支未被改动", [m["content"] for m in msgs_of(edit_id, "b2")] == ["改写后的问题", "改写后的新回答"])

# 在分支上继续编辑：父分支记录成当前分支（嵌套分叉）
app.switch_branch(edit_id, "b3")
app.st.session_state["message"] = msgs_of(edit_id, "b3")
app.st.session_state["edit_box_b3_0"] = "再改一次"
app.submit_edit(0, "edit_box_b3_0")
nested = app.st.session_state["current_branch"]
check("在分支上编辑会再建分支", nested == "b4", nested)
check("嵌套分叉的父分支是 b3",
      [(b["parent"], b["fork_index"]) for b in app.load_session_meta(edit_id)["branches"]
       if b["id"] == "b4"] == [("b3", 0)])
check("兄弟分支关系正确", app.branch_siblings(edit_id, "b3") == ["b3"], app.branch_siblings(edit_id, "b3"))

# 空内容不落盘、不建分支
before_branches = app.branch_ids(edit_id)
app.st.session_state["edit_box_b4_0"] = "   "
app.submit_edit(0, "edit_box_b4_0")
check("空内容不新建分支", app.branch_ids(edit_id) == before_branches, app.branch_ids(edit_id))
check("空内容给出提示", errors.calls and "不能为空" in errors.calls[-1], errors.calls[-1:])

# 切换回原分支：消息应换成原分支的内容
app.switch_branch(edit_id, "main")
app.st.session_state["message"] = msgs_of(edit_id, "main")
check("切回原分支后消息是原始的", [m["content"] for m in app.st.session_state["message"]] == ["原问题", "原回答"],
      app.st.session_state["message"])
app.delete_session(edit_id)

# --------------------------------------------------------------------------- #
# 对话头像：每个会话独立，纯展示（绝不进入请求）
# --------------------------------------------------------------------------- #
# 格式判定只读文件头，因此这几条可以用极短的样本；
# 但真正 set_avatar 时应用会用 Pillow 校验，必须用真实合法的图片，否则会被当成坏图
PNG_BYTES = real_png((255, 0, 0))
JPG_SAMPLE = b"\xff\xd8\xff\xe0" + b"jpg-header-only"
GIF_SAMPLE = b"GIF89a" + b"gif-header-only"
WEBP_SAMPLE = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"webp-header-only"


class _FakeUpload:
    """模拟 st.file_uploader 返回的 UploadedFile（只需要 getvalue）。"""

    def __init__(self, data):
        self._data = data

    def getvalue(self):
        return self._data


check("PNG 格式识别", app.image_suffix(PNG_BYTES) == "png")
check("JPEG 格式识别", app.image_suffix(JPG_SAMPLE) == "jpg")
check("GIF 格式识别", app.image_suffix(GIF_SAMPLE) == "gif")
check("WebP 格式识别", app.image_suffix(WEBP_SAMPLE) == "webp")
check("非图片内容被拒绝", app.image_suffix(b"not an image at all") == "")
check("按内容判断：脚本文件不会被当成图片", app.image_suffix(b"#!/bin/sh\necho hi") == "")

avatar_id = "2029-09-09_090909_000"
fresh_state(current_session=avatar_id, current_branch="main", session_title="头像测试")
app.st.session_state["message"] = [{"role": "user", "content": "你好"}]
app.save_session()
check("默认没有自定义头像", app.current_avatar("user", avatar_id) is None)
check("默认没有自定义 AI 头像", app.current_avatar("assistant", avatar_id) is None)

# 上传用户头像（走真实回调路径）
app.st.session_state["_uploader_user_avatar"] = _FakeUpload(PNG_BYTES)
app.set_avatar("user_avatar")
saved_rel = app.st.session_state["user_avatar"]
check("上传后记录了相对路径", saved_rel and saved_rel.startswith("attachments/"), saved_rel)
avatar_abs = tmp / avatar_id / saved_rel
check("图片真的落盘到会话目录", avatar_abs.exists(), str(avatar_abs))
check("落盘内容与上传一致", avatar_abs.read_bytes() == PNG_BYTES)
check("current_avatar 返回绝对路径", str(app.current_avatar("user", avatar_id)) == str(avatar_abs.resolve()),
      app.current_avatar("user", avatar_id))
check("用户头像不影响 AI 头像", app.current_avatar("assistant", avatar_id) is None)

# 上传 AI 头像
app.st.session_state["_uploader_assistant_avatar"] = _FakeUpload(real_png((0, 255, 0)))
app.set_avatar("assistant_avatar")
check("AI 头像也设置成功", app.current_avatar("assistant", avatar_id) is not None)
check("两个头像互不干扰", app.current_avatar("user", avatar_id) != app.current_avatar("assistant", avatar_id))

# 头像随会话存档
saved_meta = json.loads(meta_of(avatar_id).read_text(encoding="utf-8"))
check("头像写进 meta.json", saved_meta.get("user_avatar") == saved_rel, saved_meta.get("user_avatar"))
check("AI 头像写进 meta.json", (saved_meta.get("assistant_avatar") or "").startswith("attachments/"),
      saved_meta.get("assistant_avatar"))
check("头像不进入分支文件（消息里没有它）",
      "avatar" not in json.dumps(msgs_of(avatar_id), ensure_ascii=False))

# 纯展示：绝不进入请求
app.st.session_state["message"] = [{"role": "user", "content": "看图吗"}]
payload = app.build_request_payload()
serialized = json.dumps(payload, ensure_ascii=False)
check("请求体里没有头像字段名", "avatar" not in serialized)
check("请求体里没有 image_url 块", "image_url" not in serialized)
check("请求历史只有文本 content", all(isinstance(m["content"], str) for m in payload["messages"]),
      [type(m["content"]).__name__ for m in payload["messages"]])
check("头像路径不出现在请求里", "attachments/" not in serialized)

# 同一张图重复上传：内容哈希命名 → 复用同一个文件，不重复占空间
files_before = sorted(p.name for p in app.attachments_dir(avatar_id).glob("*"))
app.st.session_state["_uploader_user_avatar"] = _FakeUpload(PNG_BYTES)
app.set_avatar("user_avatar")
check("同内容图片不重复落盘",
      sorted(p.name for p in app.attachments_dir(avatar_id).glob("*")) == files_before)

# 超大图与非法格式被拒绝（且不会改动已有设置）
app.st.session_state["_uploader_assistant_avatar"] = _FakeUpload(b"x" * (app.MAX_AVATAR_BYTES + 1))
app.set_avatar("assistant_avatar")
check("超过大小限制的头像被拒绝",
      (json.loads(meta_of(avatar_id).read_text(encoding="utf-8")).get("assistant_avatar") or "").endswith(".png"))
errors.calls.clear()
app.st.session_state["_uploader_assistant_avatar"] = _FakeUpload(b"totally not an image")
app.set_avatar("assistant_avatar")
check("非法格式的头像被拒绝并提示", errors.calls and "格式" in errors.calls[-1], errors.calls[-1:])

# 存档里的路径不可信：越权路径读不到
app.st.session_state["user_avatar"] = "../../../etc/passwd"
check("越权头像路径被拒绝", app.avatar_file(avatar_id, "user_avatar") is None)
app.st.session_state["user_avatar"] = saved_rel  # 还原

# 清除头像：设置没了，但图片文件保留（别的会话可能引用同一张）
app.clear_avatar("assistant_avatar")
check("清除后不再显示自定义头像", app.current_avatar("assistant", avatar_id) is None)
check("清除后仍在存档里记为 None",
      json.loads(meta_of(avatar_id).read_text(encoding="utf-8")).get("assistant_avatar") is None)

# 每个会话各自记着自己的头像（这正是"每个会话独立"的含义）
first_id = avatar_id
app.load_selected_session(first_id)
check("载入会话后仍是自己设置的头像", app.st.session_state["user_avatar"] == saved_rel,
      app.st.session_state["user_avatar"])
app.st.session_state["current_session"] = first_id
app.new_session()  # 点「新建会话」
second_id = app.st.session_state["current_session"]
check("新建会话后头像回到默认", app.st.session_state["user_avatar"] is None)
# 新会话还没产生对话（只有草稿），此时设置头像不能丢
app.st.session_state["_uploader_user_avatar"] = _FakeUpload(real_png((0, 0, 255)))
app.set_avatar("user_avatar")
second_rel = app.st.session_state["user_avatar"]
check("新会话（只有草稿）设置头像后立刻生效", app.current_avatar("user") is not None, second_rel)
check("新会话（只有草稿）的头像写进草稿",
      json.loads(app.draft_path(second_id).read_text(encoding="utf-8")).get("user_avatar") == second_rel)
app.load_selected_session(first_id)
check("切回第一个会话仍是它的头像", app.st.session_state["user_avatar"] == saved_rel,
      app.st.session_state["user_avatar"])
app.load_selected_session(second_id)
check("切到第二个会话是它自己的头像", app.st.session_state["user_avatar"] == second_rel,
      app.st.session_state["user_avatar"])
check("同一个用户在不同会话可以是不同头像", saved_rel != second_rel)

# 坏图片/解不开的内容：不能让整个页面崩（st.chat_message 会真去解码图片）
broken_dir = tmp / avatar_id / "attachments"
broken_dir.mkdir(parents=True, exist_ok=True)
# 只有 PNG 文件头、后面是垃圾：连解码都过不去
(broken_dir / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"this-is-not-a-real-png-body")
app.st.session_state["user_avatar"] = "attachments/broken.png"
check("坏图片被 current_avatar 兜住（退回默认头像）",
      app.current_avatar("user", avatar_id) is None, app.current_avatar("user", avatar_id))
check("坏图片本身仍能取到路径（只是不拿去渲染）",
      app.avatar_file(avatar_id, "user_avatar") is not None)
# 真实图片 + 尾部垃圾：Pillow 本来就容忍（解码只看图片数据本身），
# 这种情况也不需要拦——Streamlit 同样能正常渲染，所以如实断言它会通过
(broken_dir / "trailing.png").write_bytes(PNG_BYTES + b"garbage-after-iend")
app.st.session_state["user_avatar"] = "attachments/trailing.png"
check("尾部有垃圾但仍可解码的图片照常使用（Pillow 不视其为损坏）",
      app.current_avatar("user", avatar_id) is not None,
      app.current_avatar("user", avatar_id))
app.st.session_state["user_avatar"] = saved_rel  # 还原

app.delete_session(avatar_id)
app.delete_session(second_id)

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
# 新建会话回调 + 空对话不落盘（"首次创建对话会创建两次" 的回归测试）
# --------------------------------------------------------------------------- #
_clear_sessions()
app.st.session_state = _State()
app.st.session_state.update(app.DEFAULT_PROFILE)
app.st.session_state.update(app.DEFAULT_ADVANCED)
app.st.session_state["thinking"] = False
app.st.session_state["message"] = []
app.st.session_state["current_session"] = "2026-01-01_120000_000"
app.st.session_state["session_title"] = "旧名字"

app.save_session()
check("空对话不落盘", session_dirs() == [], session_dirs())

# 模拟首次启动：只有内存里的会话，磁盘上没有 → 不应出现在会话历史里
check("未落盘的会话不出现在会话历史", app.load_session_list() == [], app.load_session_list())

# 第一次真正发言
app.st.session_state["message"] = [{"role": "user", "content": "你好"}]
app.render_reply(_Sink())
check("首次发言只产生一个存档", len(session_dirs()) == 1, session_dirs())
check("该存档就是当前会话", meta_of("2026-01-01_120000_000").exists())
check("产生对话后才出现在会话历史里", app.load_session_list() == ["2026-01-01_120000_000"], app.load_session_list())

# 新建会话：旧档保留，新会话在发言前不落盘、也不出现在列表里
app.st.session_state["session_title"] = "旧名字"
app.new_session()
new_id = app.st.session_state["current_session"]
check("新建会话后换新 ID", new_id != "2026-01-01_120000_000")
check("新建会话后名称回到默认", app.st.session_state["session_title"] == app.DEFAULT_SESSION_TITLE)
check("新建会话后对话清空", app.st.session_state["message"] == [])
check("旧会话仍保留在磁盘", meta_of("2026-01-01_120000_000").exists())
check("新会话发言前不落盘", not (tmp / new_id).exists())
check("新会话发言前不出现在会话历史里", app.load_session_list() == ["2026-01-01_120000_000"], app.load_session_list())

# 再次新建（连续点两次按钮）不应该产生任何空档
import time as _time
_time.sleep(0.02)
app.new_session()
second_id = app.st.session_state["current_session"]
check("连续新建会话不产生空档", session_dirs() == ["2026-01-01_120000_000"],
      session_dirs())
check("连续新建会话得到不同 ID", second_id != new_id)
check("连续新建后会话历史仍只有已保存的那条", app.load_session_list() == ["2026-01-01_120000_000"],
      app.load_session_list())

# 第二个会话发言后，历史里就有两条了
app.st.session_state["message"] = [{"role": "user", "content": "你好"}]
app.render_reply(_Sink())
check("第二个会话发言后历史有两条", app.load_session_list() == [second_id, "2026-01-01_120000_000"],
      app.load_session_list())

# --------------------------------------------------------------------------- #
# 「第一轮对话后自动进入会话历史」的完整链路
# 侧边栏只做两件事：load_session_list() 取列表、用 current_session 判断是否当前。
# 下面按这个顺序断言：发言 → 落盘 → 列表包含它 → 被标记为当前。
# --------------------------------------------------------------------------- #
_clear_sessions()
auto_id = "2099-03-03_000000_000"
app.st.session_state = _State()
app.st.session_state.update(app.DEFAULT_PROFILE)
app.st.session_state.update(app.DEFAULT_ADVANCED)
app.st.session_state["thinking"] = False
app.st.session_state["message"] = []
app.st.session_state["current_session"] = auto_id
app.st.session_state["session_title"] = "自动归档测试"

check("发言前历史里没有它", auto_id not in app.load_session_list(), app.load_session_list())
check("发言前处于新建会话状态", app.is_fresh_session() is True)

app.st.session_state["message"] = [{"role": "user", "content": "第一条"}]
app.client = FakeClient(completions=FakeCompletions())
app.render_reply(_Sink())
check("发言并落盘后不再是新建会话状态", app.is_fresh_session() is False)

check("发言后自动落盘", meta_of(auto_id).exists(), session_dirs())
check("发言后自动出现在会话历史里", auto_id in app.load_session_list(), app.load_session_list())
check("它在历史里排在第一位（最新）", app.load_session_list()[0] == auto_id, app.load_session_list())
check("侧边栏会把它标记为当前会话", app.st.session_state["current_session"] == auto_id)
check("它在历史里用的是自定义名称", app.session_title(auto_id) == "自动归档测试", app.session_title(auto_id))
check("发言后历史里只有它一条", app.load_session_list() == [auto_id], app.load_session_list())

# --------------------------------------------------------------------------- #
# 侧边栏渲染顺序：侧边栏在脚本顶部渲染，那时新会话的文件可能还不存在，
# 所以 render_reply 必须在"首次落盘"后主动重跑一次，否则会话不会出现在历史里。
# 这里显式检查这个重跑确实被触发。
# --------------------------------------------------------------------------- #
reruns = _Recorder("rerun")
app.st.rerun = reruns

# 场景 1：全新会话的第一条消息 → 首次落盘 → 必须重跑
_clear_sessions()
app.st.session_state = _State()
app.st.session_state.update(app.DEFAULT_PROFILE)
app.st.session_state.update(app.DEFAULT_ADVANCED)
app.st.session_state["thinking"] = False
app.st.session_state["message"] = []
app.st.session_state["current_session"] = "2099-04-04_000000_000"
app.st.session_state["session_title"] = "首条消息"
app.st.session_state["message"] = [{"role": "user", "content": "第一条"}]
app.client = FakeClient(completions=FakeCompletions())
app.render_reply(_Sink())
check("新会话首次落盘后触发了重跑", len(reruns.calls) == 1, reruns.calls)
check("重跑前会话已落盘", meta_of("2099-04-04_000000_000").exists())

# 场景 2：已存在的会话继续聊天 → 不需要重跑
reruns.calls.clear()
app.st.session_state["message"].append({"role": "assistant", "content": "你好"})
app.st.session_state["message"].append({"role": "user", "content": "继续"})
app.client = FakeClient(completions=FakeCompletions())
app.render_reply(_Sink())
check("已有存档的会话继续聊天不重跑", len(reruns.calls) == 0, reruns.calls)

# 场景 3：全新会话 + 空回答 → 仍然会因为那条用户消息而首次落盘，所以要重跑一次
reruns.calls.clear()
_clear_sessions()
app.st.session_state["current_session"] = "2099-04-05_000000_000"
app.st.session_state["message"] = [{"role": "user", "content": "空回答"}]
app.client = FakeClient(completions=FakeCompletions(empty=True))
app.render_reply(_Sink())
check("全新会话的空回答也会落盘（用户消息在）", meta_of("2099-04-05_000000_000").exists())
check("全新会话的空回答同样重跑一次", len(reruns.calls) == 1, reruns.calls)

# 场景 4：完全没有消息 → 不落盘也不重跑
reruns.calls.clear()
_clear_sessions()
app.st.session_state["current_session"] = "2099-04-06_000000_000"
app.st.session_state["message"] = []
app.render_reply(_Sink())
check("无消息时不落盘", session_dirs() == [], session_dirs())
check("无消息时不重跑", len(reruns.calls) == 0, reruns.calls)

# 场景 5：重新生成成功后要重跑一次，否则新回答下面没有 ⋯ 操作入口
#        （render_history 在脚本顶部就渲染完了，新回答是之后才追加的）
reruns.calls.clear()
_clear_sessions()
app.st.session_state["current_session"] = "2099-04-07_000000_000"
app.st.session_state["current_branch"] = "main"
app.st.session_state["message"] = [{"role": "user", "content": "问题"}]
app.st.session_state["pending_regen"] = True
app.client = FakeClient(completions=FakeCompletions(chunks=["重生成的回答"]))
app.render_pending_regen()
check("重新生成成功后重跑一次（让 ⋯ 立刻出现）", len(reruns.calls) == 1, reruns.calls)
check("重跑前回答已落盘",
      [m["content"] for m in msgs_of("2099-04-07_000000_000")] == ["问题", "重生成的回答"],
      msgs_of("2099-04-07_000000_000"))
# 生成失败（空回答）→ 不落盘也不重跑，保留可重试的状态
reruns.calls.clear()
app.st.session_state["pending_regen"] = True
app.client = FakeClient(completions=FakeCompletions(empty=True))
app.render_pending_regen()
check("重新生成失败时不重跑", len(reruns.calls) == 0, reruns.calls)
check("重新生成失败时保留原分支内容",
      [m["content"] for m in msgs_of("2099-04-07_000000_000")] == ["问题", "重生成的回答"],
      msgs_of("2099-04-07_000000_000"))

app.st.rerun = _Sink()
check("session_file_exists 能正确判断", app.session_file_exists("no-such") is False)

# --------------------------------------------------------------------------- #
# 草稿：还没产生对话时，改人设/高级参数也要立刻落盘，但不能进会话历史
# --------------------------------------------------------------------------- #
_clear_sessions()
app._discard_all_drafts() if hasattr(app, "_discard_all_drafts") else None
app.st.session_state = _State()
app.st.session_state.update(app.DEFAULT_PROFILE)
app.st.session_state.update(app.DEFAULT_ADVANCED)
app.st.session_state["thinking"] = False
app.st.session_state["message"] = []
app.st.session_state["current_session"] = "2099-06-01_000000_000"
app.st.session_state["session_title"] = "草稿会话"

app.save_session()
draft = app.DRAFTS_DIR / "2099-06-01_000000_000.draft"
check("空对话时写的是草稿", draft.exists(), [p.name for p in app.DRAFTS_DIR.glob("*")])
check("空对话时不产生正式存档", not meta_of("2099-06-01_000000_000").exists())
check("草稿不进会话历史", app.load_session_list() == [], app.load_session_list())
check("草稿里存了人设", json.loads(draft.read_text(encoding="utf-8"))["nickname"] == app.DEFAULT_PROFILE["nickname"])

# 改人设 → 立刻落盘到草稿
app.st.session_state["nickname"] = "改过的昵称"
app.st.session_state["temperature"] = 0.25
app.save_session()
draft_data = json.loads(draft.read_text(encoding="utf-8"))
check("改人设后草稿立刻更新", draft_data["nickname"] == "改过的昵称", draft_data.get("nickname"))
check("改参数后草稿立刻更新", draft_data["temperature"] == 0.25, draft_data.get("temperature"))

# 没有对话时切回来仍能恢复草稿内容
app.st.session_state["nickname"] = "被覆盖"
app.st.session_state["temperature"] = 1.0
app.load_selected_session("2099-06-01_000000_000")
check("从草稿恢复人设", app.st.session_state["nickname"] == "改过的昵称", app.st.session_state["nickname"])
check("从草稿恢复参数", app.st.session_state["temperature"] == 0.25, app.st.session_state["temperature"])
check("从草稿恢复时对话为空", app.st.session_state["message"] == [])

# 产生第一条对话：草稿转正、正式存档建立、草稿被清理
app.st.session_state["message"] = [{"role": "user", "content": "第一条"}]
app.client = FakeClient(completions=FakeCompletions())
app.st.session_state["current_session"] = "2099-06-01_000000_000"
app.save_session()
check("产生对话后建立正式存档", meta_of("2099-06-01_000000_000").exists())
check("转正后草稿被清掉", not draft.exists(), [p.name for p in app.DRAFTS_DIR.glob("*")])
check("转正后进入会话历史", app.load_session_list() == ["2099-06-01_000000_000"], app.load_session_list())
saved_promoted = json.loads(meta_of("2099-06-01_000000_000").read_text(encoding="utf-8"))
check("转正后保留草稿里改过的人设", saved_promoted["nickname"] == "改过的昵称", saved_promoted.get("nickname"))
check("转正后保留草稿里改过的参数", saved_promoted["temperature"] == 0.25, saved_promoted.get("temperature"))

# 删除只有草稿的会话：草稿也要清掉
app.st.session_state["current_session"] = "2099-06-02_000000_000"
app.st.session_state["message"] = []
app.save_session()
draft2 = app.DRAFTS_DIR / "2099-06-02_000000_000.draft"
check("第二个草稿已建立", draft2.exists())
app.delete_session("2099-06-02_000000_000")
check("删除会话时草稿一起清掉", not draft2.exists(), [p.name for p in app.DRAFTS_DIR.glob("*")])

# 「恢复默认值」按钮的回调：还原并落盘
app.st.session_state["message"] = []
app.st.session_state["current_session"] = "2099-06-03_000000_000"
app.st.session_state.update({"temperature": 1.5, "top_p": 0.2})
app.reset_advanced_and_save()
draft3 = app.DRAFTS_DIR / "2099-06-03_000000_000.draft"
check("恢复默认值后立刻落盘", draft3.exists(), [p.name for p in app.DRAFTS_DIR.glob("*")])
check("恢复默认值后草稿里是默认参数",
      json.loads(draft3.read_text(encoding="utf-8"))["temperature"] == app.DEFAULT_ADVANCED["temperature"])
for _f in list(app.DRAFTS_DIR.glob("*")):
    _f.unlink()

# --------------------------------------------------------------------------- #
# 「新建会话」按钮的高亮状态：处于新建会话时高亮，产生对话后恢复
# --------------------------------------------------------------------------- #
_clear_sessions()
app.st.session_state["current_session"] = "2099-05-01_000000_000"
app.st.session_state["message"] = []
check("没有任何对话时属于新建会话状态（按钮高亮）", app.is_fresh_session() is True, app.is_fresh_session())
app.st.session_state["message"] = [{"role": "user", "content": "第一句"}]
app.save_session()
check("产生对话后不再是新建会话（按钮恢复）", app.is_fresh_session() is False, app.is_fresh_session())
# 再点一次新建会话 → 又回到高亮状态
app.new_session()
check("再次新建会话后重新高亮", app.is_fresh_session() is True, app.is_fresh_session())

# --------------------------------------------------------------------------- #
# 重命名与置顶
# --------------------------------------------------------------------------- #
_clear_sessions()
app.st.rerun = _Recorder("rerun")
app.st.session_state = _State()
app.st.session_state.update(app.DEFAULT_PROFILE)
app.st.session_state.update(app.DEFAULT_ADVANCED)
app.st.session_state["thinking"] = False
app.st.session_state["session_title"] = "旧名字"
app.st.session_state["session_pinned"] = False
app.st.session_state["current_session"] = "2026-05-05_120000_000"
app.st.session_state["message"] = [{"role": "user", "content": "内容"}]
app.save_session()

app.rename_session("2026-05-05_120000_000", "崭新名字")
check("重命名写入存档", app.session_title("2026-05-05_120000_000") == "崭新名字",
      app.session_title("2026-05-05_120000_000"))
check("重命名不改变文件名", meta_of("2026-05-05_120000_000").exists())
check("重命名保留消息",
      msgs_of("2026-05-05_120000_000")
      == [{"role": "user", "content": "内容"}])
check("重命名当前会话会同步输入框的值", app.st.session_state["session_title"] == "崭新名字",
      app.st.session_state["session_title"])

# 回调形式：不传 new_title，从输入框的 key 里取值（这就是回车重命名的路径）
app.st.session_state["rename_input_test"] = "回车改的名"
app.rename_session("2026-05-05_120000_000", input_key="rename_input_test")
check("从输入框 key 取值重命名", app.session_title("2026-05-05_120000_000") == "回车改的名",
      app.session_title("2026-05-05_120000_000"))
check("回车重命名同样同步 session_title", app.st.session_state["session_title"] == "回车改的名",
      app.st.session_state["session_title"])

# 输入框为空时回退成默认名称
app.st.session_state["rename_input_blank"] = "   "
app.rename_session("2026-05-05_120000_000", input_key="rename_input_blank")
check("输入框为空回退成默认名称", app.session_title("2026-05-05_120000_000") == app.DEFAULT_SESSION_TITLE,
      app.session_title("2026-05-05_120000_000"))

# 写盘失败时不留临时文件（这正是之前改名失败后残留 .json.tmp 的原因）
real_replace = app.os.replace
app.os.replace = lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked"))
try:
    app.rename_session("2026-05-05_120000_000", "写不进去")
    check("写盘失败时给出 st.error", errors.calls and "重命名失败" in errors.calls[-1], errors.calls[-1:])
finally:
    app.os.replace = real_replace
check("写盘失败后不留临时文件", not list(tmp.glob("*.tmp*")), [p.name for p in tmp.glob("*.tmp*")])
check("写盘失败不影响原存档",
      app.session_title("2026-05-05_120000_000") == app.DEFAULT_SESSION_TITLE,
      app.session_title("2026-05-05_120000_000"))

# 空名字回退成默认名称
app.rename_session("2026-05-05_120000_000", "   ")
check("空名字回退成默认名称", app.session_title("2026-05-05_120000_000") == app.DEFAULT_SESSION_TITLE,
      app.session_title("2026-05-05_120000_000"))

# 置顶
app.set_pinned("2026-05-05_120000_000", True)
check("置顶写入存档", app.load_session_meta("2026-05-05_120000_000")["pinned"] is True)
check("置顶不影响名称", app.session_title("2026-05-05_120000_000") == app.DEFAULT_SESSION_TITLE)

# 置顶排序（上一步已把 2026-05-05 置顶，所以先取消掉再验证时间倒序）
app.st.session_state["current_session"] = "2026-05-06_120000_000"
app.st.session_state["message"] = [{"role": "user", "content": "第二个"}]
app.save_session()
app.set_pinned("2026-05-05_120000_000", False)
check("未置顶时按时间倒序",
      app.load_session_list() == ["2026-05-06_120000_000", "2026-05-05_120000_000"],
      app.load_session_list())
app.set_pinned("2026-05-05_120000_000", True)
check("置顶更早的会话后排到最前",
      app.load_session_list() == ["2026-05-05_120000_000", "2026-05-06_120000_000"],
      app.load_session_list())
app.set_pinned("2026-05-06_120000_000", True)
check("两个都置顶时按时间倒序",
      app.load_session_list() == ["2026-05-06_120000_000", "2026-05-05_120000_000"],
      app.load_session_list())
app.set_pinned("2026-05-06_120000_000", False)
check("取消置顶后落到未置顶段（仍在置顶项之后）",
      app.load_session_list() == ["2026-05-05_120000_000", "2026-05-06_120000_000"],
      app.load_session_list())
app.set_pinned("2026-05-05_120000_000", False)
check("全部取消置顶后恢复时间倒序",
      app.load_session_list() == ["2026-05-06_120000_000", "2026-05-05_120000_000"],
      app.load_session_list())

# 加载会话时带上置顶状态
app.set_pinned("2026-05-05_120000_000", True)
app.st.session_state["session_pinned"] = False
app.load_selected_session("2026-05-05_120000_000")
check("加载会话恢复置顶状态", app.st.session_state["session_pinned"] is True)
check("加载会话恢复名称", app.st.session_state["session_title"] == app.DEFAULT_SESSION_TITLE)

# 新建会话重置置顶状态
app.new_session()
check("新建会话后置顶状态归零", app.st.session_state["session_pinned"] is False)

# 老存档没有 pinned 字段时按未置顶处理
legacy_no_pin = tmp / "2026-05-07_120000_000.json"
legacy_no_pin.write_text(json.dumps({"title": "老档", "message": []}), encoding="utf-8")
check("缺 pinned 字段的老存档视为未置顶", app.load_session_meta("2026-05-07_120000_000")["pinned"] is False)
legacy_no_pin.unlink()
app.st.rerun = _Sink()

app.st.session_state.update({"temperature": 0.2, "top_p": 0.3, "limit_tokens": True,
                             "max_tokens": 512, "frequency_penalty": 1.0, "presence_penalty": -1.0})
app.reset_advanced_and_save()  # 「恢复默认值」按钮的回调（还原 + 落盘）
for _key, _value in app.DEFAULT_ADVANCED.items():
    check(f"恢复默认值 {_key}", app.st.session_state[_key] == _value, app.st.session_state[_key])

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
check("存档目录支持 AI_PARTNER_SESSIONS_DIR 隔离", "AI_PARTNER_SESSIONS_DIR" in src)
# 会话列表的组件 key 不能带列表序号：删掉上面一条会整体上移，
# 带序号会导致弹层展开状态/输入框内容串到下一条会话上
sidebar_src = src.split('st.subheader("会话历史")', 1)[-1].split('st.subheader("管理角色")', 1)[0]
check("会话列表组件 key 不含列表序号", "{index}" not in sidebar_src, sidebar_src[:40])
check("使用 Path 解析脚本目录", "Path(__file__).resolve().parent" in src)
check("os.replace 原子落盘", "os.replace" in src)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
