"""人设与高级生成参数：从状态读取、写回状态、重置成默认值。"""

import streamlit as st

from . import config



def profile_from_state() -> dict:
    return {key: st.session_state[key] for key in config.PROFILE_KEYS}

def advanced_value(name: str):
    return st.session_state.get(name, config.DEFAULT_ADVANCED[name])


def advanced_from_state() -> dict:
    """当前的高级生成参数快照（存进会话存档用）。"""
    return {key: st.session_state.get(key, config.DEFAULT_ADVANCED[key]) for key in config.ADVANCED_KEYS}


def avatar_from_state() -> dict:
    """当前会话的头像设置快照（每个会话独立，存进会话存档用）。

    注意：头像是纯展示用的，**不会**进入请求，也不会发给模型。
    """
    return {key: st.session_state.get(key) for key in config.AVATAR_KEYS}


def apply_advanced(data: dict) -> None:
    """用存档里的高级参数覆盖当前设置；缺失的键回退成默认值。

    只能从回调里调用（回调先于控件实例化），否则改这些已被滑块占用的 key 会报
    StreamlitWidgetAlreadyInstantiatedError。
    """
    for key, default in config.DEFAULT_ADVANCED.items():
        value = data.get(key, default)
        st.session_state[key] = default if value is None else value
