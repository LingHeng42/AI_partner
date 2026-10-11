"""存储后端：把"会话存哪里"和"会话怎么用"分开。

为什么要有这一层
----------------
Community Cloud 上**容器本地文件随时会被重置**（休眠、改设置、push 代码、平台维护
都会触发），所以线上必须把存档放到外部数据库；而本地开发和测试用文件系统最方便。
两者用同一个接口，上层（store/sessions/images）不需要知道自己在用哪个。

数据是按**用户**分区的
----------------------
所有路径都自动带上当前用户的根前缀（``users/<用户名>/``），所以：
- 用户只能列出/读到自己的会话（``load_session_list`` 就是在自己的前缀下列目录）
- 一个用户删自己的东西时，不可能碰到别人的（键名里就带着 uid）

路径形状（同一个形状在文件和 Mongo 里都成立）：

    <根>/sessions/<会话ID>/meta.json
    <根>/sessions/<会话ID>/branches/<分支>.json
    <根>/sessions/<会话ID>/attachments/<哈希>.png
    <根>/sessions/drafts/<会话ID>.draft

Mongo 后端把每个文件存成一个文档（``key`` = 相对路径，``data`` = 原样的字节），
这样上层代码读到的内容与文件后端完全一致，行为没有分歧。
"""

import json
import os
import shutil
import sys
import threading
from pathlib import Path

from . import config

MONGO_ENV_VAR = "MONGO_URI"
MONGO_DB_NAME = "lingheng"
MONGO_COLLECTION = "files"
# 用户根前缀：每个人只在自己的这棵子树里读写
LOCAL_BACKEND = "local"
MONGO_BACKEND = "mongo"

_state = threading.local()


# --------------------------------------------------------------------------- #
# 当前用户
# --------------------------------------------------------------------------- #
def set_current_uid(uid: str) -> None:
    """设置当前请求属于哪个用户（ui.main() 每次重跑都会调一次）。

    Streamlit 每个会话跑在自己的线程里，所以用 threading.local 是安全的：
    另一个用户的请求不会看到这个值。未设置时（纯逻辑测试、脚本调用）退化成
    "local" 用户，这样本地单独跑某个函数也不会炸。
    """
    _state.uid = str(uid or "").strip() or "local"


def current_uid() -> str:
    return getattr(_state, "uid", None) or "local"


def _safe(part: str) -> str:
    """挡掉路径穿越：任何一段都不允许出现 .. 或路径分隔符。"""
    text = str(part or "").strip()
    if not text or text != Path(text).name or text in (".", ".."):
        raise ValueError(f"非法的路径片段：{part!r}")
    return text


def _norm(rel: str) -> str:
    """把相对路径规范化成 POSIX 风格，并做越界检查。"""
    text = str(rel).replace("\\", "/").strip("/")
    if not text or ".." in text.split("/"):
        raise ValueError(f"非法的相对路径：{rel!r}")
    return text


# --------------------------------------------------------------------------- #
# 本地文件后端
# --------------------------------------------------------------------------- #
class LocalBackend:
    """文件系统后端。``base`` 由 config.archive_dir() 决定，测试会把它换成临时目录。"""

    kind = LOCAL_BACKEND

    def __init__(self, base: Path):
        self.base = Path(base)
        self.root = self.base / "users" / _safe(current_uid())

    def path(self, rel: str) -> Path:
        return self.root / _norm(rel)

    def get(self, rel: str) -> bytes:
        p = self.path(rel)
        return p.read_bytes() if p.exists() else b""

    def put(self, rel: str, data: bytes) -> None:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp{os.getpid()}")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, p)  # 原子替换，避免写一半断电留下半截文件
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def list(self, prefix: str) -> list:
        """列出前缀下的所有文件（相对路径，POSIX 风格）。"""
        target = self.path(prefix) if prefix else self.root
        if not target.exists():
            return []
        out = []
        for p in target.rglob("*"):
            if p.is_file():
                out.append(p.relative_to(self.root).as_posix())
        return sorted(out)

    def delete(self, rel: str) -> None:
        p = self.path(rel)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()

    def delete_prefix(self, prefix: str) -> int:
        """删掉整个前缀（本地后端直接删目录，比逐文件删快得多）。"""
        target = self.path(prefix) if prefix else self.root
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            return 1
        if target.exists():
            target.unlink()
            return 1
        return 0


# --------------------------------------------------------------------------- #
# MongoDB 后端
# --------------------------------------------------------------------------- #
class MongoBackend:
    """MongoDB 后端。每个文件 = 一个文档：{uid, key, data}。

    ``data`` 直接存原始字节（BSON 的 binary 类型），所以 JSON 与图片一视同仁，
    上层读到的内容和文件后端一模一样。
    """

    kind = MONGO_BACKEND

    def __init__(self, client, uid: str):
        self.collection = client[MONGO_DB_NAME][MONGO_COLLECTION]
        self.uid = _safe(uid)
        self.collection.create_index([("uid", 1), ("key", 1)], unique=True)

    def _key(self, rel: str) -> str:
        return f"users/{self.uid}/" + _norm(rel)

    def get(self, rel: str) -> bytes:
        doc = self.collection.find_one({"uid": self.uid, "key": self._key(rel)})
        return bytes(doc["data"]) if doc else b""

    def put(self, rel: str, data: bytes) -> None:
        self.collection.update_one(
            {"uid": self.uid, "key": self._key(rel)},
            {"$set": {"data": bytes(data)}},
            upsert=True,
        )

    def exists(self, rel: str) -> bool:
        return self.collection.count_documents(
            {"uid": self.uid, "key": self._key(rel)}, limit=1) > 0

    def list(self, prefix: str) -> list:
        root = f"users/{self.uid}/"
        full = self._key(prefix) if prefix else root.rstrip("/")
        # 用正则做前缀匹配：MongoDB 里没有目录，只有键名
        import re

        pattern = "^" + re.escape(full) + "/"
        out = []
        for doc in self.collection.find({"uid": self.uid, "key": {"$regex": pattern}},
                                        {"key": 1}):
            out.append(doc["key"][len(root):])
        return sorted(out)

    def delete(self, rel: str) -> None:
        self.collection.delete_many({"uid": self.uid, "key": self._key(rel)})

    def delete_prefix(self, prefix: str) -> int:
        import re

        pattern = "^" + re.escape(self._key(prefix)) + "(/|$)"
        result = self.collection.delete_many({"uid": self.uid, "key": {"$regex": pattern}})
        return result.deleted_count


# --------------------------------------------------------------------------- #
# 选择后端
# --------------------------------------------------------------------------- #
def mongo_uri() -> str:
    """连接串：优先运行时覆盖（测试用），其次 secrets，最后环境变量。"""
    return _mongo_lookup()[0]


def _mongo_lookup():
    """返回 ``(连接串, 来源, 读配置时的报错)``。

    为什么要连"来源"和"报错"一起返回：线上最常见的问题是 **secrets 读不到**
    （比如 TOML 语法不合法、键名写错、忘了填 MONGO_URI），这时应用会静默退回
    本地文件模式 —— 而 Community Cloud 的本地文件每次重启都会被清空，
    表现就是"历史记录都不见了"，却完全看不出原因。
    """
    import sys

    entry = sys.modules.get("AI_partner")
    override = getattr(entry, "MONGO_URI", None) if entry is not None else None
    if override is not None:
        return str(override).strip(), "运行时覆盖", ""
    try:
        import streamlit as st

        if MONGO_ENV_VAR in st.secrets:
            return str(st.secrets[MONGO_ENV_VAR]).strip(), "secrets", ""
        secrets_error = f"secrets 里没有 `{MONGO_ENV_VAR}`"
    except Exception as exc:  # noqa: BLE001 - secrets 缺失/解析失败都走这里
        secrets_error = f"读 secrets 失败（{type(exc).__name__}: {str(exc)[:120]}）"
    value = (os.environ.get(MONGO_ENV_VAR) or "").strip()
    if value:
        return value, "环境变量", secrets_error
    return "", "", secrets_error


def mask_uri(uri: str) -> str:
    """把连接串里的密码遮掉，方便安全地显示/贴日志。"""
    import re as _re

    if not uri:
        return ""
    return _re.sub(r"://([^:/@]+):([^@/]+)@", r"://\1:***@", uri)


def using_mongo() -> bool:
    return bool(mongo_uri())


def status() -> dict:
    """当前存储状态（给界面显示与排查用）。

    返回 ``mode`` / ``uri`` / ``uri_source`` / ``secrets_error`` / ``ok`` / ``error``
    / ``documents``。``ok`` 为 False 时 ``error`` 里是连不上的原因。
    """
    uri, source, secrets_error = _mongo_lookup()
    info = {
        "mode": MONGO_BACKEND if uri else LOCAL_BACKEND,
        "uri": mask_uri(uri),
        "uri_source": source,
        "secrets_error": secrets_error,
        "ok": True,
        "error": "",
        "documents": None,
    }
    if not uri:
        return info
    try:
        collection = _client()[MONGO_DB_NAME][MONGO_COLLECTION]
        info["documents"] = collection.count_documents({"uid": current_uid()})
    except Exception as exc:  # noqa: BLE001 - 连不上要把原因显示出来，而不是静默降级
        info["ok"] = False
        info["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return info


def _client():
    """pymongo 客户端（按连接串缓存，避免每次重跑都重新握手）。"""
    uri = mongo_uri()
    cached = getattr(_state, "client", None)
    cached_uri = getattr(_state, "client_uri", None)
    if cached is not None and cached_uri == uri:
        return cached
    from pymongo import MongoClient

    # 连接是懒建立的；给一个不短的超时，免得网络不通时页面卡死
    client = MongoClient(uri, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000)
    _state.client = client
    _state.client_uri = uri
    return client


def active():
    """当前生效的后端。

    配了连接串就用 MongoDB（线上），否则用文件系统（本地开发 / 测试）。

    **配了却连不上时直接报错，不退回本地文件**：Community Cloud 的本地文件每次
    重启都会被清空，静默降级会把数据写进一个注定会丢的地方 —— 用户只会看到
    "历史记录不见了"，完全查不出原因。宁可明确报错。
    """
    if using_mongo():
        try:
            backend = MongoBackend(_client(), current_uid())
            # 构造后端时就会建索引，连不上会在这里抛；再 ping 一次确认真的通
            backend.collection.database.client.admin.command("ping")
            _state.mongo_error = None
            return backend
        except Exception as exc:  # noqa: BLE001
            message = (
                f"MongoDB 连接失败（{type(exc).__name__}: {str(exc)[:180]}）。\n\n"
                "会话**不会**写进本地文件——那在 Streamlit Cloud 上随时会被重置，写了也会丢。"
                "请依次检查：\n"
                "① Atlas → Network Access 是否允许 `0.0.0.0/0`"
                "（Streamlit Cloud 的出口 IP 不固定，只放行你自己的 IP 会导致线上连不上）；\n"
                "② App 的 Secrets 里是否有 `MONGO_URI`（注意 TOML 语法，写错会导致整个 "
                "secrets 文件读不到）；\n"
                "③ 用户名/密码是否正确，密码里的 `@ : / ?` 等字符要做 URL 编码；\n"
                "④ 集群是否已暂停/删除。"
            )
            if getattr(_state, "mongo_error", None) != message:
                _state.mongo_error = message
                print(f"[storage] {message}", file=sys.stderr, flush=True)
            raise RuntimeError(message) from exc
    return LocalBackend(config.archive_dir())


def backend_kind() -> str:
    return MONGO_BACKEND if using_mongo() else LOCAL_BACKEND


# --------------------------------------------------------------------------- #
# 给上层用的便捷读写
# --------------------------------------------------------------------------- #
def read_json(rel: str, default=None):
    raw = active().get(rel)
    if not raw:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return default


def write_json(rel: str, data) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    active().put(rel, payload)


def read_bytes(rel: str) -> bytes:
    return active().get(rel)


def write_bytes(rel: str, data: bytes) -> None:
    active().put(rel, bytes(data))


def exists(rel: str) -> bool:
    return active().exists(rel)


def delete(rel: str) -> None:
    active().delete(rel)


def delete_prefix(prefix: str) -> int:
    """删掉整个前缀（会话目录/草稿等）。只作用于当前用户自己的子树。"""
    return active().delete_prefix(prefix)


def list_files(prefix: str) -> list:
    return active().list(prefix)


def list_children(prefix: str) -> list:
    """前缀下的直接子目录名（用来列会话）。"""
    depth = 0 if not prefix else len(_norm(prefix).split("/"))
    names = set()
    for rel in active().list(prefix):
        parts = rel.split("/")
        if len(parts) > depth + 1:
            names.add(parts[depth])
    return sorted(names)


def session_ids() -> list:
    """当前用户的会话 ID 列表（排除 drafts 这类不是会话的子目录）。"""
    return [name for name in list_children("sessions") if name != "drafts"]
