"""存储原语：会话/分支/附件的**键名**、会话 ID、JSON 读写。

一个会话一棵子树（键名是相对路径，具体落在文件系统还是 MongoDB 由 storage.py 决定）：

    sessions/<会话ID>/meta.json              会话级信息
    sessions/<会话ID>/branches/<分支>.json    一条分支 = 一条线性消息列表
    sessions/<会话ID>/attachments/<哈希>.png  会话内图片（头像）
    sessions/drafts/<会话ID>.draft            还没产生对话时的草稿

注意：**键名里不带用户**。用户隔离由 storage.py 在根前缀上完成（``users/<用户名>/``），
所以这里（以及 sessions/branches/images）都不需要知道"当前是谁"，也就不会出现
"某个调用点忘了带 uid 导致串会话"的漏洞。

本模块不依赖上层（也不依赖 streamlit）：存栏目录一律**运行时**从
``config.archive_dir()`` 读取，所以测试把它改成临时目录后整套代码都跟着走。
"""

import datetime
from pathlib import Path

from . import config, storage

DEFAULT_SESSION_TITLE = "新会话"
SESSIONS_ROOT = "sessions"
BRANCHES_SUBDIR = config.BRANCHES_SUBDIR
ATTACHMENTS_SUBDIR = config.ATTACHMENTS_SUBDIR


def new_session_id() -> str:
    """生成会话 ID（= 存档目录名）：精确到毫秒，避免同一秒内连续新建会话互相覆盖。"""
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]


def _plain_name(value) -> str:
    """校验"只允许一整段名字"，挡掉 ../ 与绝对路径这类越界。"""
    text = str(value)
    if not text or text != Path(text).name or text in (".", ".."):
        raise ValueError(f"非法的名字：{value!r}")
    return text


def _session_dir(session_name: str) -> str:
    """某个会话的存档目录（键名前缀）。"""
    return f"{SESSIONS_ROOT}/{_plain_name(session_name)}"


def branches_dir(session_name: str) -> str:
    return f"{_session_dir(session_name)}/{BRANCHES_SUBDIR}"


def branch_path(session_name: str, branch_id: str) -> str:
    """某条分支的键名（分支 ID 同样只允许一整段名字，挡掉路径穿越）。"""
    return f"{branches_dir(session_name)}/{_plain_name(branch_id)}.json"


def attachments_dir(session_name: str) -> str:
    """会话内的图片目录（头像等），与分支文件同级。"""
    return f"{_session_dir(session_name)}/{ATTACHMENTS_SUBDIR}"

