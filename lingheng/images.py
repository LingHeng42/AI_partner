"""会话内的图片：格式校验、按内容哈希落盘、头像读写。

头像是纯展示用的，**不会**发给模型。
"""

import hashlib

import streamlit as st
from PIL import Image

from . import config, sessions, store



def image_suffix(data: bytes) -> str:
    """按文件内容判断图片格式；不认识的格式返回空串（调用方据此拒绝）。"""
    for signature, suffix in config.IMAGE_SIGNATURES:
        if data.startswith(signature):
            return suffix
    # WebP: RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def store_image(session_name: str, data: bytes, suffix: str) -> str:
    """把图片按内容哈希存进会话的 attachments 目录，返回相对会话目录的路径。

    用内容哈希命名：同一张图重复上传天然去重，也不会互相覆盖。
    """
    digest = hashlib.sha1(data).hexdigest()[:16]
    relative = f"{config.ATTACHMENTS_SUBDIR}/{digest}.{suffix}"
    target = store.attachments_dir(session_name) / f"{digest}.{suffix}"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return relative


def avatar_file(session_name: str, key: str):
    """某个角色的头像文件绝对路径；没设置过返回 None。"""
    relative = st.session_state.get(key)
    if not relative:
        return None
    try:
        base = store._session_dir(session_name)
    except ValueError:
        return None
    candidate = (base / relative).resolve()
    # 只允许读会话目录内的文件（存档里的路径不可信）
    if base.resolve() not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def current_avatar(role: str, session_name: str = None):
    """给 st.chat_message 用的头像参数（本地绝对路径）；没有可用头像时返回 None。

    "可用"要真的验证过：文件存在还不够——损坏的图片、或 Streamlit 解不开的格式
    会让 st.chat_message 直接抛错并打断整个页面。这里**真正解码一遍**再交给它
    （只做 verify() 不够：它不校验数据是否真的能解出来），坏图退回默认头像，
    保证页面永远能渲染出来。
    """
    key = "user_avatar" if role == "user" else "assistant_avatar"
    path = avatar_file(session_name or st.session_state.get("current_session"), key)
    if path is None:
        return None
    try:
        with Image.open(path) as probe:
            probe.load()  # 完整解码，坏图会在这里抛错
    except Exception:  # noqa: BLE001 - 坏图/不支持的格式一律退回默认头像
        return None
    return path


def set_avatar(key: str) -> None:
    """头像上传回调：校验图片 → 落盘到会话目录 → 记进内存与存档。

    作为 st.file_uploader 的 on_change 回调执行（回调先于控件实例化），
    所以这里改 *_avatar 这类普通状态键是安全的。上传的文件从控件自己的
    session_state 里读（不通过 args 传：args 是渲染时求值的，那时还没选文件）。

    两道防串会话的保护：
    - `_avatar_uploaded_for` 记下这次上传属于哪个会话，只在当前会话仍是它时才应用
    - `_avatar_applied` 记下已经处理过的文件，重跑时不会把同一张图重复写一次
    """
    uploaded = st.session_state.get(f"_uploader_{key}")
    if uploaded is None:
        return
    session_name = st.session_state.get("current_session")
    if not session_name:
        return
    # 切换会话后，上传控件里可能还留着上一个会话选的文件：不能套用到新会话上
    owner = st.session_state.get("_avatar_uploaded_for")
    if owner is not None and owner != session_name:
        return
    if st.session_state.get("_avatar_applied") is uploaded:
        return  # 同一张图已经处理过（重跑时控件仍持有它）
    data = uploaded.getvalue()
    if len(data) > config.MAX_AVATAR_BYTES:
        st.error(f"图片太大（{len(data) / 1024 / 1024:.1f} MB），请压缩到 5 MB 以内")
        return
    suffix = image_suffix(data)
    if not suffix:
        st.error("无法识别的图片格式，请上传 PNG / JPEG / GIF / WebP")
        return
    try:
        st.session_state[key] = store_image(session_name, data, suffix)
    except Exception as e:  # noqa: BLE001
        st.error(f"保存头像失败: {e}")
        return
    st.session_state._avatar_uploaded_for = session_name
    st.session_state._avatar_applied = uploaded
    sessions.save_session()  # 立刻落盘（有对话进 meta.json，没对话进草稿）


def clear_avatar(key: str) -> None:
    """清除头像（只清设置，图片文件留在附件目录里不删，避免误删别的引用）。

    不能给 file_uploader 的控件 key 赋值（Streamlit 视为只读），所以改用
    `_avatar_applied` 记住"这个文件已处理过"，避免它被重复写回来。
    """
    st.session_state[key] = None
    st.session_state._avatar_applied = st.session_state.get(f"_uploader_{key}")
    sessions.save_session()
