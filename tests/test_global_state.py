"""审计"进程级全局状态"留下的三类隐患（都是 `users/local` 那一类 bug 的亲戚）。

用户的要求是：「**检查是否还有 local 等可能导致 bug 的旧代码遗留**」。
查下来真正会出事故的是三处"把身份/权限存在进程级全局里"的写法：

1. **模型客户端**（ai.client）是模块级全局 —— 所有浏览器会话、所有线程共用。
   两个人（或同一个人的两个标签页）同时用时，后一次 ``ui.main()`` 的
   ``configure_client()`` 会把前一个人**正在流式请求**的客户端换掉：
   A 的提问用 B 的 Key 发出去（花错人的钱，还记到别人的额度上）。
   → 改成存在 ``st.session_state[ai.CLIENT_STATE]`` 里，按会话隔离。

2. **登录门控**原来是 ``bool(_credentials_raw())``：线上 secrets 一旦读不出来
   （TOML 写错、键名写错），门控**整个消失** —— 任何访客都能进，且所有人都是
   用户名 "local"，共用同一个 ``users/local`` 分区、互相看得见会话。
   → 改成 fail-closed：**读不到凭据表 ≠ 没配登录**，门控照旧打开并在页面上报错。

3. **回调写存储前必须自己定身份**：控件回调跑在脚本主体之前、还可能落在别的
   线程上（线程局部量里残留着别人的用户名）。已经给会话重命名/置顶/头像这些
   会写存储的回调补上 ``bind_callback_uid()``。

顺带把"换账号时清内存"从 ``ui.main()`` 里抽成 ``ui.clear_account_state()``：
以前它内联在 main 里，删掉其中任何一个键都不会有测试报警。

用法：python tests/test_global_state.py
"""
import os
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _harness  # noqa: E402

PROJECT = _harness.PROJECT
sys.path.insert(0, str(PROJECT))
os.environ["AI_PARTNER_SESSIONS_DIR"] = str(_harness.SESSIONS_DIR)
os.environ.pop("DEEPSEEK_API_KEY", None)

import mongomock  # noqa: E402

from lingheng import ai, auth, config, images, sessions, storage, ui  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


class _State(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as exc:
            raise AttributeError(k) from exc

    def __setattr__(self, k, v):
        self[k] = v

    def get(self, k, default=None):
        return dict.get(self, k, default)


ERRORS = []
sessions.st.rerun = lambda: None
sessions.st.error = lambda text, *a, **k: ERRORS.append(str(text))
sessions.st.warning = lambda text, *a, **k: None

C = mongomock.MongoClient()
storage._client = lambda: C
storage._mongo_lookup = lambda: ("mongodb://fake", "probe", "")
storage._mongo_ready.clear()
storage.reset_cache()
storage.set_current_uid("lingheng42")


def fresh(**kw):
    st = _State(kw)
    for key, value in config.DEFAULT_PROFILE.items():
        st.setdefault(key, value)
    for key, value in config.DEFAULT_ADVANCED.items():
        st.setdefault(key, value)
    for key in config.AVATAR_KEYS:
        st.setdefault(key, None)
    st.setdefault("message", [])
    return st


MSG = [{"role": "user", "content": "问题"},
       {"role": "assistant", "content": "回答1"}]

# --------------------------------------------------------------------------- #
# 1. 模型客户端必须按浏览器会话隔离（原来是模块级全局）
# --------------------------------------------------------------------------- #
A = fresh()
B = fresh()
sessions.st.session_state = A
ai.configure_client("client_A")
sessions.st.session_state = B
ai.configure_client("client_B")

check("客户端写进浏览器会话自己的状态里（不再是模块全局）",
      B.get(ai.CLIENT_STATE) == "client_B", B.get(ai.CLIENT_STATE))
sessions.st.session_state = A
check("**A 的提问仍然用 A 的客户端**（全局写法下这里会拿到 B 的）",
      ai._client() == "client_A", ai._client())
sessions.st.session_state = B
check("B 拿到 B 的客户端", ai._client() == "client_B", ai._client())

# 不在脚本上下文里（工具脚本 / 老用法）仍然退回模块全局
real_session_state = sessions.st.session_state


class _NoSessionState:
    def __getattr__(self, name):
        raise RuntimeError("没有脚本上下文")


sessions.st.session_state = _NoSessionState()
ai.configure_client("client_offline")
check("不在脚本上下文里时退回模块全局（老用法仍然可用）",
      ai.client == "client_offline", ai.client)
sessions.st.session_state = real_session_state
ai.client = None

# 测试会写 app.client = 假客户端：入口模块上的覆盖值优先（既有套件依赖这一点）
entry = types.ModuleType("AI_partner")
entry.client = "client_override"
sys.modules["AI_partner"] = entry
sessions.st.session_state = A
check("入口模块上的覆盖值优先（既有测试的写法没被破坏）",
      ai._client() == "client_override", ai._client())
del sys.modules["AI_partner"]

# --------------------------------------------------------------------------- #
# 2. 登录门控 fail-closed：读不到凭据表 ≠ 没配登录
# --------------------------------------------------------------------------- #
class _SecretsSt:
    """假的 streamlit 模块，只提供 auth 需要的那几个属性。"""

    def __init__(self, secrets=None, error=None):
        self._secrets = secrets
        self._error = error

    @property
    def secrets(self):
        if self._error is not None:
            raise self._error
        return self._secrets


real_auth_st = auth.st
real_disable = os.environ.pop("AI_PARTNER_DISABLE_AUTH", None)
real_credentials_env = os.environ.pop("AUTH_CREDENTIALS", None)
try:
    # (a) 压根没有 secrets 文件 → 本地开发，不做门控
    auth.st = _SecretsSt(error=FileNotFoundError("no secrets"))
    check("没有 secrets 文件 = 本地开发，不做门控", auth.auth_enabled() is False,
          auth.auth_enabled())

    # (b) 有 secrets 内容但没有凭据表 → **门控必须照旧打开**
    auth.st = _SecretsSt(secrets={"MONGO_URI": "mongodb://x"})
    check("**有 secrets 但漏配凭据表 → 门控仍然打开**（fail-closed）",
          auth.auth_enabled() is True, auth.auth_enabled())
    check("并且说清楚缺的是哪一段", "AUTH_CREDENTIALS" in str(auth.SECRETS_ERROR),
          auth.SECRETS_ERROR)

    # (c) secrets 文件本身有 TOML 错误（整个读不出来）→ 同样必须打开门控
    auth.st = _SecretsSt(error=ValueError("Invalid TOML"))
    check("**secrets 解析失败 → 门控仍然打开**（以前会整个消失）",
          auth.auth_enabled() is True, auth.auth_enabled())
    check("并且把原始报错留下来给界面看", "Invalid TOML" in str(auth.SECRETS_ERROR),
          auth.SECRETS_ERROR)

    # (d) 正常配了凭据 → 门控打开、能解析出账号
    auth.st = _SecretsSt(secrets={
        "AUTH_CREDENTIALS": {"usernames": {"alice": {"password": "x"}}}})
    check("正常配置时门控打开", auth.auth_enabled() is True, auth.auth_enabled())
    parsed = auth.credentials()
    check("凭据表能解析出账号",
          "alice" in (parsed.get("usernames") or {}), parsed)
finally:
    auth.st = real_auth_st
    if real_disable is not None:
        os.environ["AI_PARTNER_DISABLE_AUTH"] = real_disable
    if real_credentials_env is not None:
        os.environ["AUTH_CREDENTIALS"] = real_credentials_env

# --------------------------------------------------------------------------- #
# 3. 会写存储的回调必须自己把身份定下来
# --------------------------------------------------------------------------- #
SID = "2095-01-01_000000_000"
sessions.st.session_state = fresh(_uid="alice", current_session=SID,
                                  current_branch="main",
                                  message=[dict(m) for m in MSG])
storage.set_current_uid("alice")
sessions.save_session()

# 回调线程里残留着别人的用户名（线程池复用线程时的真实情形）
storage.set_current_uid("bob")
sessions.RECENT_ACTIONS.clear()
sessions.rename_session(SID, "改过的名字")
storage.set_current_uid("alice")
check("**回调里残留别人的身份时，重命名仍然写进自己的分区**",
      sessions.load_session_meta(SID).get("title") == "改过的名字",
      sessions.load_session_meta(SID).get("title"))
check("身份被修正过就有日志（点名两边）",
      any("bob" in line and "alice" in line for line in sessions.RECENT_ACTIONS),
      sessions.RECENT_ACTIONS[:2])

storage.set_current_uid("alice")
sessions.RECENT_ACTIONS.clear()
sessions.rename_session(SID, "第二次改")
check("身份本来就对时不写多余日志（不刷屏）",
      not any("修正" in line for line in sessions.RECENT_ACTIONS),
      sessions.RECENT_ACTIONS[:2])


def func_source(rel_path, func_name):
    """把某个函数的源码整段取出来（静态断言"这一层有没有做某件事"）。"""
    import ast

    text = (PROJECT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"没找到函数 {func_name}（{rel_path}）")


for _path, _func in (
    ("lingheng/sessions.py", "rename_session"),
    ("lingheng/sessions.py", "set_pinned"),
    ("lingheng/images.py", "set_avatar"),
    ("lingheng/images.py", "clear_avatar"),
):
    check(f"{_func}() 会先绑定身份再写存储",
          "bind_callback_uid" in func_source(_path, _func), _path)

for _func in ("regenerate", "submit_edit"):
    check(f"ai.{_func}() 会先绑定身份再写存储",
          "_bind_callback_uid" in func_source("lingheng/ai.py", _func), _func)

check("绑定身份的实现只有一份（sessions.bind_callback_uid）",
      "bind_session_uid" in func_source("lingheng/sessions.py", "bind_callback_uid"),
      "sessions.bind_callback_uid")

# --------------------------------------------------------------------------- #
# 4. 换账号时把上一个账号的内存态清干净
# --------------------------------------------------------------------------- #
check("关键键都在清理名单里（漏一个就是事故）",
      ai.USER_KEY_STATE in ui.ACCOUNT_SCOPED_STATE
      and ai.CLIENT_STATE in ui.ACCOUNT_SCOPED_STATE
      and "message" in ui.ACCOUNT_SCOPED_STATE
      and sessions.PENDING_DELETE in ui.ACCOUNT_SCOPED_STATE
      and sessions.PENDING_NEW in ui.ACCOUNT_SCOPED_STATE,
      ui.ACCOUNT_SCOPED_STATE)

state = fresh(_uid="alice", current_session=SID, session_title="上一个人的会话",
              message=[dict(m) for m in MSG], pending_regen=True, editing_index=2,
              user_api_key="sk-上一个人的key", _model_client="上一个人的客户端",
              _pending_delete="上一个人要删的会话", _avatar_bytes_cache={"b": b"x"})
# 顺便把头像、人设也设成"上一个人的"值
state[config.AVATAR_KEYS[0]] = "sessions/上一个/avatar.png"
state["nickname"] = "上一个人的名字"
sessions.st.session_state = state
ui.clear_account_state()

check("换账号后清掉用户自己填的 Key", state.get(ai.USER_KEY_STATE) is None,
      state.get(ai.USER_KEY_STATE))
check("换账号后清掉模型客户端", state.get(ai.CLIENT_STATE) is None,
      state.get(ai.CLIENT_STATE))
check("换账号后清掉消息", state.get("message") == [], state.get("message"))
# 会话指针必须换成**新 ID**，不能留 None：main() 里"没有 current_session 就新建一个"
# 用的是 `"current_session" not in session_state`，值是 None 的键它不管，
# 主区域会拿着 None 去读分支（"换账号后点进去一片空白"就是这么来的）。
check("换账号后换成一个全新的会话指针（不是旧的、也不是 None）",
      bool(state.get("current_session")) and state.get("current_session") != SID,
      state.get("current_session"))
check("换账号后清掉待办动作（不能落到新账号头上）",
      state.get(sessions.PENDING_DELETE) is None and state.get(sessions.PENDING_NEW) is None,
      state.get(sessions.PENDING_DELETE))
check("换账号后清掉头像缓存与头像设置",
      state.get("_avatar_bytes_cache") is None and state.get(config.AVATAR_KEYS[0]) is None,
      (state.get("_avatar_bytes_cache"), state.get(config.AVATAR_KEYS[0])))
check("换账号后人设恢复默认（不显示上一个人的名字）",
      state.get("nickname") == config.DEFAULT_PROFILE.get("nickname"),
      (state.get("nickname"), config.DEFAULT_PROFILE.get("nickname")))

# 顺序：定身份 → 清上一个人的内存 → 再执行待办动作
# 注意只在 main() 的函数体里找：clear_account_state 的**定义**本来就在 main 前面。
src = _harness.ui_source()
main_src = src[src.index("def main() -> None:"):]
i_uid = main_src.index("storage.set_current_uid(uid or \"local\")")
i_clear = main_src.index("clear_account_state()")
i_pending = main_src.index("sessions.run_pending_actions()")
check("main() 里的顺序是「定身份 → 清上一个人的内存 → 执行待办动作」",
      i_uid < i_clear < i_pending, (i_uid, i_clear, i_pending))
check("清内存只在账号真的变了时才做（同一账号不误伤）",
      "not in (None, uid)" in main_src, main_src[i_clear - 200:i_clear])

print()
if failures:
    print(f"{len(failures)} 项未通过：")
    for name in failures:
        print("  - " + name)
    sys.exit(1)
print("全部通过")
