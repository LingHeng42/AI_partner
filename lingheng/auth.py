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
import sys
from pathlib import Path

import streamlit as st

from . import config


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
    """是否配置了登录。没配 = 本地开发模式（不做门控，方便自己调试）。"""
    override = _runtime_override("AUTH_ENABLED")
    if override is not None:
        return bool(override)
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
    """解析成 dict；解析不出来返回空 dict（= 没有可用账号）。"""
    raw = _credentials_raw()
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw)
    try:  # 也允许直接放一段 JSON 字符串（依赖少、复制粘贴方便）
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
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


def _authenticator():
    """构造 streamlit-authenticator 实例；没装/没配置时返回 None（页面会给出提示）。"""
    creds = credentials()
    if not creds:
        return None
    try:
        import streamlit_authenticator as stauth
    except ImportError:
        return None
    try:
        return stauth.Authenticate(
            dict(creds), _cookie_config()["name"],
            _cookie_config()["key"], _cookie_config()["expiry_days"],
        )
    except Exception:  # noqa: BLE001 - 配置写错时不要让整页崩
        return None


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
