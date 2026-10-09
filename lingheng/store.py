"""存储原语：原子写 JSON、会话/分支/附件路径、会话 ID。

一个会话一个目录：
    <存栏目录>/<会话ID>/meta.json             会话级信息
    <存栏目录>/<会话ID>/branches/<分支>.json   一条分支 = 一条线性消息列表
    <存栏目录>/<会话ID>/attachments/<哈希>.png 会话内图片（头像）
    <存栏目录>/drafts/<会话ID>.draft           还没产生对话时的草稿

本模块不依赖上层（也不依赖 streamlit），是最底层：存栏目录一律**运行时**从
``config.ARCHIVE_DIR`` 读取，所以测试把它改成临时目录后整套代码都跟着走。
"""

import datetime
import json
import os
import time
from pathlib import Path

from . import config



DEFAULT_SESSION_TITLE = "新会话"


def new_session_id() -> str:
    """生成会话 ID（= 存档文件名）：精确到毫秒，避免同一秒内连续新建会话互相覆盖。"""
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]

def _session_dir(session_name: str) -> Path:
    """某个会话的存档目录（一个会话一个目录，分支文件放在 branches/ 下）。"""
    if not session_name or Path(session_name).name != session_name:
        raise ValueError(f"非法会话名：{session_name!r}")
    return config.archive_dir() / session_name


def branches_dir(session_name: str) -> Path:
    return _session_dir(session_name) / config.BRANCHES_SUBDIR


def branch_path(session_name: str, branch_id: str) -> Path:
    """某条分支的文件路径（分支 ID 同样只允许纯文件名，挡掉路径穿越）。"""
    if not branch_id or Path(branch_id).name != branch_id:
        raise ValueError(f"非法分支名：{branch_id!r}")
    return branches_dir(session_name) / f"{branch_id}.json"


def attachments_dir(session_name: str) -> Path:
    """会话内的图片目录（头像等），与分支文件同级。"""
    return _session_dir(session_name) / config.ATTACHMENTS_SUBDIR

def _write_json_atomic(path: Path, data: dict) -> None:
    """原子写 JSON：先写临时文件再替换，失败时清理临时文件。

    临时文件用随机后缀，避免和上一次失败残留的 .tmp 互相干扰。
    """
    path.parent.mkdir(parents=True, exist_ok=True)  # 草稿在 sessions/drafts 下
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        for attempt in range(5):  # Windows 上杀毒/索引可能短暂占用文件，重试几次
            try:
                os.replace(tmp, path)  # 原子替换
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        # 成功时 tmp 已被 replace 掉；失败时把它删掉，不留孤儿文件
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
