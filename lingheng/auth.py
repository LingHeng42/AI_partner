"""身份层：谁在用这个应用。

设计要点（写下来是因为这几条容易被后来的改动破坏）：

1. **只对外暴露"当前用户是谁"**，上层（会话隔离、MongoDB、密钥判定）不关心背后是
   streamlit-authenticator 还是 st.login/OIDC。以后换实现只改这一个文件。
2. **身份取值一律来自服务器校验过的凭据**，绝不用"用户自己填的名字"当权限依据——
   否则改名冒充就能用别人的额度。
3. **登录凭据与权限名单都从服务器侧读**（Streamlit secrets 优先，环境变量兜底），
   浏览器拿不到。读取失败一律当作"未登录 / 无权限"（fail-closed）。
"""

import json
import os
from collections.abc import Mapping
import sys
from pathlib import Path

import streamlit as st

from . import config

# 每次脚本运行只构造一次登录组件（原因见 _cached 的说明）
_AUTH_CACHE_STATE = "_auth_instance"


def _runtime_override(name):
    """运行时覆盖值：测试写 ``app.ALLOWED_USERS = [...]`` 就能注入（同 config 的机制）。"""
    entry = sys.modules.get("AI_partner")
    if entry is not None:
        return getattr(entry, name, None)
    return None


# --------------------------------------------------------------------------- #
# 登录配置（凭据表 + 签名 cookie 的密钥）
# --------------------------------------------------------------------------- #
def auth_enabled() -> bool:
    """是否配置了登录。没配 = 本地开发模式（不做门控，方便自己调试）。

    测试用 ``AI_PARTNER_DISABLE_AUTH=1`` 显式关掉门控：测试不该依赖（也不该被）
    开发者本机的 secrets 影响 —— 本地一旦有 secrets.toml，界面类测试就会停在登录页。
    """
    override = _runtime_override("AUTH_ENABLED")
    if override is not None:
        return bool(override)
    disabled = str(os.environ.get("AI_PARTNER_DISABLE_AUTH") or "").strip().lower()
    if disabled in ("1", "true", "yes"):
        return False
    return bool(_credentials_raw())


def _credentials_raw():
    """凭据表：secrets[AUTH_CREDENTIALS] / 环境变量 / 本地 auth_config.yaml。

    Community Cloud 上**不要**走 yaml 文件那条路：本地文件随时会被重置，
    注册/改密这类要写回文件的功能都会失效。固定名单直接配在 secrets 里最稳。
    """
    override = _runtime_override("AUTH_CREDENTIALS_RAW")
    if override is not None:
        return override
    try:
        if "AUTH_CREDENTIALS" in st.secrets:
            return st.secrets["AUTH_CREDENTIALS"]
    except Exception:  # noqa: BLE001 - 没配 secrets
        pass
    value = os.environ.get("AUTH_CREDENTIALS")
    if value:
        return value
    path = Path(config.BASE_DIR) / "auth_config.yaml"
    return path if path.exists() else None


def credentials() -> dict:
    """解析成 dict；解析不出来返回空 dict（= 没有可用账号）。

    注意：``st.secrets[...]`` 返回的不是普通 dict，而是 Streamlit 的 ``AttrDict``
    （dict 的子类但在某些版本里不是 ``dict`` 实例），所以这里必须按"映射"判断，
    不能写 ``isinstance(raw, dict)``——否则会把已经解析好的配置当成字符串去
    ``json.loads``，静默变成空配置（登录页就再也不出现了，真踩过这个坑）。
    """
    raw = _credentials_raw()
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    text = str(raw)
    try:  # 也允许直接放一段 JSON 字符串（依赖少、复制粘贴方便）
        parsed = json.loads(text)
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    except Exception:  # noqa: BLE001
        return {}


def _cookie_config() -> dict:
    """签名 cookie 的配置（streamlit-authenticator 用它记住登录状态）。"""
    override = _runtime_override("COOKIE_KEY")
    key = str(override) if override is not None else None
    if not key:
        try:
            key = str(st.secrets["COOKIE_KEY"]) if "COOKIE_KEY" in st.secrets else None
        except Exception:  # noqa: BLE001
            key = None
    key = key or os.environ.get("COOKIE_KEY") or ""
    return {
        "name": "lingheng_auth",
        # 没配就退化成"不记住"：宁可不记住，也不要一个谁都能伪造的固定密钥
        "key": key or "lingheng-auth-remember-me-not-configured",
        "expiry_days": 7,
    }


def _plain(value):
    """把 Streamlit 的只读配置对象（AttrDict / Secrets）深拷贝成普通 dict/list。

    为什么必须这么做：streamlit-authenticator 默认 ``auto_hash=True``，构造时会
    **就地**把明文密码改写成哈希——而 Streamlit 的 secrets 对象是只读的，就地赋值
    会抛 ``TypeError: Secrets does not support item assignment``，登录页就再也出不来。
    """
    import copy

    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return copy.deepcopy(value)


def _auth_cache_key():
    """用来判断"配置变了没有"。只比形状，不比对具体凭据内容。"""
    try:
        creds = credentials()
        users = tuple(sorted((creds.get("usernames") or {}).keys()))
    except Exception:  # noqa: BLE001
        users = ()
    cookie = _cookie_config()
    return (users, cookie["name"], cookie["key"], cookie["expiry_days"])


def _cached(factory):
    """每次脚本运行只构造一次（配置变了才重建）。

    为什么必须缓存：``stauth.Authenticate(...)`` 内部会创建
    ``extra_streamlit_components.CookieManager``，而它的组件 key 是写死的 ``"init"``。
    同一次脚本运行里构造两次就会撞
    ``StreamlitDuplicateElementKey: key='init'``——登录页直接报错点不进去。
    （``login_form_allowed()`` 与 ``do_login()`` 都会取实例，所以很容易构造两次。）
    """
    key = _auth_cache_key()
    cache = st.session_state.get(_AUTH_CACHE_STATE)
    if cache is not None and cache[0] == key:
        return cache[1]
    instance = factory()
    st.session_state[_AUTH_CACHE_STATE] = (key, instance)
    return instance


def _authenticator():
    """构造 streamlit-authenticator 实例；没装/没配置时返回 None（页面会给出提示）。"""
    def build():
        creds = credentials()
        if not creds:
            return None
        try:
            import streamlit_authenticator as stauth
        except ImportError:
            return None
        try:
            return stauth.Authenticate(
                _plain(creds), _cookie_config()["name"],
                _cookie_config()["key"], _cookie_config()["expiry_days"],
            )
        except Exception as exc:  # noqa: BLE001 - 配置写错时不要让整页崩，但必须留下原因
            # 这里不能只是静默返回 None：线上表现为"登录页不出现/点不进去"，
            # 排查时完全看不出原因。写到服务端日志（不会泄露凭据内容）。
            print(f"[auth] 构造登录组件失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            return None

    if not credentials():
        return None
    return _cached(build)


# --------------------------------------------------------------------------- #
# 当前用户
# --------------------------------------------------------------------------- #
def current_user() -> dict:
    """当前登录用户：{"username", "name", "email"}；未登录返回空 dict。

    认的是 streamlit-authenticator 写进 session_state 的 ``authentication_status``，
    那是它用签名 cookie 校验过的结果，用户改不了。
    """
    if not auth_enabled():
        return {"username": "local", "name": "本地开发", "email": ""}
    if st.session_state.get("authentication_status") is not True:
        return {}
    username = str(st.session_state.get("username") or "").strip()
    if not username:
        return {}
    return {
        "username": username,
        "name": str(st.session_state.get("name") or username),
        "email": str(st.session_state.get("email") or ""),
    }


def current_user_id() -> str:
    """用于"隔离会话"的用户标识；未登录返回空串。

    用**用户名**而不是显示名/邮箱：凭据表里用户名唯一且不会变。
    """
    return current_user().get("username", "")


def can_use_owner_key(username: str = None) -> bool:
    """这个用户是否被允许用站长的 key。

    名单为空 / 读不到配置 → False（fail-closed）：宁可让大家自己填 key，
    也不要出现"配置一出问题就全员花站长钱"的情况。
    """
    name = (username if username is not None else current_user_id()).strip().lower()
    if not name:
        return False
    return name in config.allowed_users()


def key_source_label(source: str) -> str:
    """把 key 来源翻译成给人看的短句。"""
    return {
        "user": "你自己的 Key",
        "owner": "站长的 Key",
        "none": "未配置",
    }.get(source, source)


# --------------------------------------------------------------------------- #
# 登录 / 登出（渲染部分放在 ui.py，这里只做状态操作）
# --------------------------------------------------------------------------- #
def login_form_allowed() -> bool:
    """是否具备渲染登录表单的条件（装了库 + 配了凭据）。"""
    return _authenticator() is not None


def installed() -> bool:
    """streamlit-authenticator 是否可用。"""
    return "streamlit_authenticator" in sys.modules or _module_available()


def _module_available() -> bool:
    try:
        import streamlit_authenticator  # noqa: F401
    except ImportError:
        return False
    return True


def do_login() -> bool:
    """渲染登录表单并返回是否已登录（供 ui.py 调用）。"""
    authenticator = _authenticator()
    if authenticator is None:
        return False
    # 显式给一个控件 key：不传时它内部用 "init" 之类的固定 key，重跑时容易撞上
    # StreamlitDuplicateElementKey（登录页直接报错、点不进去）。
    try:
        authenticator.login(location="main", key="lingheng_login")
    except TypeError:  # 老版本没有 key 参数
        authenticator.login(location="main")
    return st.session_state.get("authentication_status") is True


def do_logout() -> None:
    """登出：清掉登录态并重跑。"""
    authenticator = _authenticator()
    if authenticator is not None:
        try:
            authenticator.logout(location="sidebar")
        except Exception:  # noqa: BLE001 - 老版本可能签名不同
            pass
    for key in ("authentication_status", "username", "name", "email"):
        st.session_state.pop(key, None)
    st.rerun()
