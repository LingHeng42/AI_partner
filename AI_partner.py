"""凌恒的酒馆 —— 基于 Streamlit + DeepSeek 的角色扮演聊天应用。

运行方式：
    streamlit run AI_partner.py

需要环境变量 DEEPSEEK_API_KEY（可在系统环境变量或 .env 中配置）。
"""

import datetime
import json
import os
import shutil
import time
from pathlib import Path

import streamlit as st
from openai import OpenAI

# --------------------------------------------------------------------------- #
# 路径：全部基于脚本自身位置解析，从任何工作目录启动都能找到 sessions/ 与 resources/
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
# 存档目录：默认 <项目>/sessions。测试用 AI_PARTNER_SESSIONS_DIR 指到临时目录，
# 这样测试永远不会碰到真实存档（曾经因为测试清空真实目录而丢过用户数据）。
ARCHIVE_DIR = Path(
    os.environ.get("AI_PARTNER_SESSIONS_DIR") or (BASE_DIR / "sessions")
).expanduser()
# 每个会话一个子目录：<ARCHIVE_DIR>/<会话ID>/
#   meta.json         会话级信息 + 当前分支指针
#   branches/<分支ID>.json   一条分支 = 一条线性消息列表
META_FILENAME = "meta.json"
BRANCHES_SUBDIR = "branches"
# 草稿目录：给"还没产生对话"的会话存人设/高级参数，不进会话历史
DRAFTS_DIR = ARCHIVE_DIR / "drafts"
RESOURCES_DIR = BASE_DIR / "resources"
LOGO_PATH = RESOURCES_DIR / "logo.png"

# --------------------------------------------------------------------------- #
# 默认人设：只在这里定义一次，初始化 / 加载 / 删除会话统一复用
# --------------------------------------------------------------------------- #
DEFAULT_PROFILE = {
    "nickname": "溟月",
    "nature": "聪明但很懒，傲娇嘴甜，酷爱白米饭",
    "role_description": (
        "溟月是一位拥有蓝色长发和蓝色眼睛的少女，身穿深蓝白色长裙女仆装，"
        "长着鲸鱼尾巴和耳侧的薄片状鳍"
    ),
    "output_rules": "请以第一人称的口吻回答问题，尽量简短，避免使用过于复杂的词汇和句子结构。",
}
PROFILE_KEYS = tuple(DEFAULT_PROFILE)

SYSTEM_PROMPT = """你叫{nickname}，
性格：{nature}。
角色简介：{role_description}。
按照你{output_rules}的输出规则回答用户的问题，请完全代入角色，遵循性格和角色简介。"""
# 待办：未来可以用检索增强算法外挂用户原创世界观，保证用户沉浸感。

MODEL_NAME = "deepseek-flash"
# 上下文窗口：保留最近 N 条消息，同时限制总字符数，避免长对话超长报错
MAX_CONTEXT_MESSAGES = 40
MAX_CONTEXT_CHARS = 12000

# --------------------------------------------------------------------------- #
# 高级生成参数（侧边栏「高级配置」里可调）
# 默认值与不传参时的服务端默认行为保持一致
# --------------------------------------------------------------------------- #
DEFAULT_ADVANCED = {
    "temperature": 1.0,
    "top_p": 1.0,
    "limit_tokens": False,
    "max_tokens": 4096,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
}
ADVANCED_KEYS = tuple(DEFAULT_ADVANCED)


def load_dotenv_file(path: Path = BASE_DIR / ".env") -> None:
    """极简 .env 读取：KEY=VALUE 逐行，已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# 先尝试补全环境变量，后面的 require_api_key() / OpenAI 客户端都依赖它
load_dotenv_file()


# --------------------------------------------------------------------------- #
# 会话存档
# --------------------------------------------------------------------------- #
DEFAULT_SESSION_TITLE = "新会话"


def new_session_id() -> str:
    """生成会话 ID（= 存档文件名）：精确到毫秒，避免同一秒内连续新建会话互相覆盖。"""
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]


def profile_from_state() -> dict:
    return {key: st.session_state[key] for key in PROFILE_KEYS}


def _session_dir(session_name: str) -> Path:
    """某个会话的存档目录（一个会话一个目录，分支文件放在 branches/ 下）。"""
    if not session_name or Path(session_name).name != session_name:
        raise ValueError(f"非法会话名：{session_name!r}")
    return ARCHIVE_DIR / session_name


def branches_dir(session_name: str) -> Path:
    return _session_dir(session_name) / BRANCHES_SUBDIR


def branch_path(session_name: str, branch_id: str) -> Path:
    """某条分支的文件路径（分支 ID 同样只允许纯文件名，挡掉路径穿越）。"""
    if not branch_id or Path(branch_id).name != branch_id:
        raise ValueError(f"非法分支名：{branch_id!r}")
    return branches_dir(session_name) / f"{branch_id}.json"


def session_dir_exists(session_name: str) -> bool:
    """会话是否已经以新结构（目录 + meta.json）存过盘。"""
    try:
        return (_session_dir(session_name) / META_FILENAME).exists()
    except ValueError:
        return False


def load_session_meta(session_name: str) -> dict:
    """读会话级元信息：名称、置顶、当前分支、分支列表。

    - 新结构：<会话目录>/meta.json
    - 旧结构（扁平 <会话ID>.json）：现场合成一份等价元信息，首次保存时自动迁移
    - 什么都没有：给出默认值（一个空会话，只有 main 分支）
    """
    try:
        path = _session_dir(session_name) / META_FILENAME
    except ValueError:
        return default_session_meta(session_name)
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            data["_legacy"] = False
            return data
        except Exception:  # noqa: BLE001 - 元信息损坏时退化为默认值
            return default_session_meta(session_name)
    flat = _session_dir(session_name).with_suffix(".json")  # 兼容旧扁平存档
    if flat.exists():
        return _legacy_meta(session_name, flat)
    return default_session_meta(session_name)


def _branch(self_id: str, parent=None, fork_index=None) -> dict:
    return {
        "id": self_id,
        "parent": parent,
        "fork_index": fork_index,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def default_session_meta(session_name: str) -> dict:
    """默认元信息：一个空会话，只有 main 分支。"""
    return {
        "title": session_name,
        "pinned": False,
        "current_branch": "main",
        "branches": [_branch("main")],
        "_legacy": False,
    }


def _legacy_meta(session_name: str, flat: Path) -> dict:
    """把旧扁平存档合成新结构需要的元信息（先只读，落盘时再迁移）。"""
    meta = default_session_meta(session_name)
    try:
        with flat.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return meta
    meta["title"] = (data.get("title") or "").strip() or session_name
    meta["pinned"] = bool(data.get("pinned"))
    meta["_legacy"] = True
    meta["_legacy_data"] = data  # 首次迁移时连同人设/高级参数一起搬过去
    if isinstance(data.get("branches"), list):
        meta["branches"] = normalize_branches(data["branches"])
    return meta


def normalize_branches(raw) -> list:
    """把 branches 字段规整成 [{id, parent, fork_index, created_at}]。"""
    out = []
    for item in raw or []:
        if isinstance(item, str):
            out.append(_branch(item))
        elif isinstance(item, dict) and item.get("id"):
            out.append({
                "id": str(item["id"]),
                "parent": item.get("parent"),
                "fork_index": item.get("fork_index"),
                "created_at": item.get("created_at") or "",
            })
    return out or [_branch("main")]


def branch_ids(session_name: str) -> list:
    """该会话已有哪些分支（顺序即创建顺序，main 在最前）。"""
    return [b["id"] for b in normalize_branches(load_session_meta(session_name).get("branches"))]


def own_meta(meta: dict) -> dict:
    """去掉内部字段，只留要写盘的部分（`_legacy*` 是只读兼容用的临时键）。"""
    return {k: v for k, v in meta.items() if not k.startswith("_")}


def write_session_meta(session_name: str, meta: dict) -> None:
    """原子写 meta.json（调用方保证已 setdefault 出 current_branch）。"""
    _write_json_atomic(_session_dir(session_name) / META_FILENAME, own_meta(meta))


def read_branch(session_name: str, branch_id: str) -> dict:
    """读一条分支的内容；没有文件时返回空消息列表。"""
    try:
        path = branch_path(session_name, branch_id)
    except ValueError:
        return {"message": []}
    if not path.exists():
        return {"message": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"message": []}
    except Exception:  # noqa: BLE001 - 单个分支文件损坏不应影响其它分支
        return {"message": []}


def write_branch(session_name: str, branch_id: str, data: dict) -> None:
    _write_json_atomic(branch_path(session_name, branch_id), data)


def load_current_branch(session_name: str) -> dict:
    """读"当前分支"指向的那条分支（旧扁平存档则读取它自己的 message）。"""
    meta = load_session_meta(session_name)
    data = meta.get("_legacy_data") if meta.get("_legacy") else None
    if data is not None:
        return {"message": data.get("message", [])}
    return read_branch(session_name, meta.get("current_branch") or "main")


def migrate_session(session_name: str) -> None:
    """把旧扁平存档迁移成新结构：写 meta.json + branches/<分支>.json，然后删旧文件。

    迁移只发生一次；任何"会写盘"的操作都会先调用它，所以旧会话一旦被改动
    就自动升级，读操作则一直是只读兼容、不改动磁盘。
    """
    try:
        directory = _session_dir(session_name)
    except ValueError:
        return
    flat = directory.with_suffix(".json")
    if not flat.exists() or (directory / META_FILENAME).exists():
        return
    try:
        with flat.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        st.error(f"迁移旧存档失败（保留原文件）: {e}")
        return
    meta = default_session_meta(session_name)
    meta["title"] = (data.get("title") or "").strip() or session_name
    meta["pinned"] = bool(data.get("pinned"))
    if isinstance(data.get("branches"), list):
        meta["branches"] = normalize_branches(data["branches"])
    branch_id = meta["current_branch"] = meta["branches"][0]["id"]
    # 旧存档的人设与高级参数是会话级的，继续保留在 meta.json 里
    for key in set(PROFILE_KEYS) | set(ADVANCED_KEYS):
        if key in data:
            meta[key] = data[key]
    write_session_meta(session_name, meta)
    write_branch(session_name, branch_id, {"message": data.get("message", [])})
    try:
        flat.unlink()
    except OSError:
        pass  # 删不掉也不影响新结构（读的时候优先看 meta.json）


def session_title(session_name: str) -> str:
    """读某个存档的自定义会话名称；没设置过就回退成会话 ID（时间戳）。"""
    meta = load_session_meta(session_name)
    return (meta.get("title") or "").strip() or session_name


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


def _update_session_meta(session_name: str, **changes) -> None:
    """更新会话级元信息（title / pinned 等），保留分支与人设。

    还没产生对话的会话只有草稿，这里就改草稿。
    """
    if not session_dir_exists(session_name) and not session_file_exists(session_name):
        draft = draft_path(session_name)
        if draft is not None and draft.exists():
            try:
                with draft.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                data.update(changes)
                _write_json_atomic(draft, data)
            except Exception as e:  # noqa: BLE001
                st.error(f"更新失败: {e}")
            return
        # 既没有存档也没有草稿：无事可做
        return
    migrate_session(session_name)
    meta = load_session_meta(session_name)
    meta.update(changes)
    write_session_meta(session_name, meta)


def rename_session(session_name: str, new_title: str = None, input_key: str = None) -> None:
    """重命名会话：只改存档里的 title，不重命名文件。

    作为 st.text_input 的 on_change 回调执行（回调先于控件实例化）：
    不传 new_title 时从 input_key 指向的输入框里取值；重命名当前会话时同步
    st.session_state['session_title']，让侧边栏的「会话名称」和主区域标题跟着变。
    这个同步**只能**在回调里做，否则会撞上
    StreamlitWidgetAlreadyInstantiatedError。
    """
    if new_title is None:
        new_title = st.session_state.get(input_key, "")
    title = (new_title or "").strip() or DEFAULT_SESSION_TITLE
    try:
        _update_session_meta(session_name, title=title)
        if session_name == st.session_state.get("current_session"):
            st.session_state.session_title = title
    except Exception as e:  # noqa: BLE001
        st.error(f"重命名失败: {e}")


def set_pinned(session_name: str, pinned: bool) -> None:
    """置顶 / 取消置顶会话（写入存档的 pinned 字段），同样走回调。"""
    try:
        _update_session_meta(session_name, pinned=bool(pinned))
    except Exception as e:  # noqa: BLE001
        st.error(f"置顶失败: {e}")
        return
    st.rerun()


def reset_profile() -> None:
    """把角色人设恢复为默认值（同时重置对话内容）。"""
    for key, value in DEFAULT_PROFILE.items():
        st.session_state[key] = value
    st.session_state.message = []


def draft_path(session_name: str = None):
    """草稿文件路径：给"还没产生对话"的会话存人设与高级参数。"""
    name = session_name or st.session_state.get("current_session")
    if not name or Path(name).name != name:
        return None
    return DRAFTS_DIR / f"{name}.draft"


def save_draft(data: dict) -> None:
    """把人设/高级参数立刻写进草稿文件（不进会话历史）。"""
    path = draft_path()
    if path is None:
        return
    if not data.get("message"):
        _write_json_atomic(path, data)


def discard_draft(session_name: str = None) -> None:
    """删掉草稿（会话正式落盘或会话被删除时调用）。"""
    path = draft_path(session_name)
    if path is not None and path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _file_pinned() -> bool:
    """当前会话存档里的置顶状态。

    置顶是单独由 set_pinned() 写的；save_session() 只负责其它字段，
    不能拿内存里的值去覆盖它（否则改一次人设就会把置顶弄丢）。
    """
    name = st.session_state.get("current_session")
    if not name:
        return False
    try:
        return bool(load_session_meta(name).get("pinned"))
    except Exception:  # noqa: BLE001
        return False


def save_session() -> None:
    """把当前会话落盘。

    结构：会话级信息（名称/置顶/当前分支/分支列表/人设/高级参数）写 meta.json，
    消息写 branches/<当前分支>.json。
    - 已经产生过对话：写正式存档（首次会把旧扁平存档自动迁移），并清掉同名草稿
    - 还没有对话（比如刚新建就改了人设/参数）：只写 drafts/<会话ID>.draft，
      这样修改立刻落盘、又不会在会话历史里留下空条目
    """
    session_name = st.session_state.get("current_session")
    if not session_name:
        return
    messages = st.session_state.get("message", [])
    if not messages:
        save_draft({
            "title": st.session_state.get("session_title", ""),
            "message": [],
            **profile_from_state(),
            **advanced_from_state(),
        })
        return
    migrate_session(session_name)
    branch_id = st.session_state.get("current_branch") or "main"
    meta = load_session_meta(session_name)
    meta["title"] = st.session_state.get("session_title", "")
    # 置顶由 set_pinned() 单独维护，这里不能覆盖
    meta["pinned"] = bool(meta.get("pinned"))
    ids = [b["id"] for b in normalize_branches(meta.get("branches"))]
    if branch_id not in ids:
        existing = meta.get("branches") or []
        meta["branches"] = normalize_branches([*existing, _branch(branch_id)])
        ids.append(branch_id)
    meta["current_branch"] = branch_id
    # 人设与高级参数是会话级的一套，跟着 meta 走
    meta.update(profile_from_state())
    meta.update(advanced_from_state())
    write_branch(session_name, branch_id, {"message": messages})
    write_session_meta(session_name, meta)
    discard_draft()


def load_session_list() -> list:
    """会话历史：置顶的排在最前，其余按时间从新到旧。

    兼容两种结构：新结构（<会话ID>/meta.json 目录）与旧扁平存档（<会话ID>.json）。
    空对话不落盘，所以这里列出来的都是聊过的会话。
    """
    if not ARCHIVE_DIR.exists():
        return []
    names = set()
    for entry in ARCHIVE_DIR.iterdir():
        if entry.is_dir():
            # 会话目录（drafts 等内部目录会被 session_dir_exists 过滤掉）
            if (entry / META_FILENAME).exists() or entry.with_suffix(".json").exists():
                names.add(entry.name)
        elif entry.suffix == ".json":
            names.add(entry.stem)  # 旧扁平存档
    # key 用字符串而非 datetime，省一次解析；会话 ID 是定长时间戳，字典序即时间序
    return sorted(names, key=lambda n: (load_session_meta(n).get("pinned", False), n), reverse=True)


def _session_exists(session_name: str = None) -> bool:
    """会话是否有实物落盘（新结构目录 或 旧扁平文件）。"""
    name = session_name or st.session_state.get("current_session")
    if not name or Path(name).name != name:
        return False
    try:
        directory = ARCHIVE_DIR / name
    except Exception:  # noqa: BLE001
        return False
    return (directory / META_FILENAME).exists() or directory.with_suffix(".json").exists()


def session_file_exists(session_name: str = None) -> bool:
    """当前会话是否已有存档（= 侧边栏会话历史里能否列出它）。"""
    return _session_exists(session_name)


def is_fresh_session() -> bool:
    """当前是否处于"新建会话"状态。

    判定标准：这个会话还没有落盘（= 还没有产生任何对话）。一旦发出第一条消息，
    存档建立、会话进入历史，就不再是"新建会话"状态了。
    """
    return not session_file_exists()


# --------------------------------------------------------------------------- #
# 分支
#
# 一条分支 = 一条线性消息列表。所谓"分叉"不是真树，而是：两条分支共享同一个
# 分叉点（parent 分支的第 fork_index 条之前的前缀），此后各写各的。
# 编辑消息 / 重新生成都通过 fork_branch() 新建一条分支，原分支保留可切回。
# --------------------------------------------------------------------------- #
def next_branch_id(session_name: str) -> str:
    """给新分支取一个不冲突的 ID：b2、b3……（main 永远是第一条）。"""
    taken = set(branch_ids(session_name))
    index = 2
    while f"b{index}" in taken:
        index += 1
    return f"b{index}"


def branch_siblings(session_name: str, branch_id: str) -> list:
    """同一次分叉产生的兄弟分支（parent 与 fork_index 都相同的那些）。"""
    items = normalize_branches(load_session_meta(session_name).get("branches"))
    me = next((b for b in items if b["id"] == branch_id), None)
    if me is None:
        return [branch_id]
    same = [b["id"] for b in items
            if b.get("parent") == me.get("parent") and b.get("fork_index") == me.get("fork_index")]
    return same or [branch_id]


def fork_branch(session_name: str, prefix: list, seed=None, parent=None, fork_index=None) -> str:
    """新建一条分支并把 prefix(+seed) 写进去，返回新分支 ID。

    prefix 是新分支继承的消息前缀；seed 是分叉点那条被改写/重新生成的消息
    （编辑消息时传入，重新生成时传 None）。
    """
    migrate_session(session_name)
    branch_id = next_branch_id(session_name)
    meta = load_session_meta(session_name)
    meta["branches"] = normalize_branches(
        [*normalize_branches(meta.get("branches")), _branch(branch_id, parent, fork_index)]
    )
    meta["current_branch"] = branch_id
    messages = [dict(m) for m in prefix]
    if seed is not None:
        messages.append(dict(seed))
    write_branch(session_name, branch_id, {"message": messages})
    write_session_meta(session_name, meta)
    return branch_id


def switch_branch(session_name: str, branch_id: str) -> None:
    """切换当前分支：先确保目标分支文件存在，最后才改 meta 里的指针。

    顺序很重要：反过来可能出现"指针指向一条还不存在的分支"。
    这是按钮回调，Streamlit 在回调之后本来就会重跑一次，所以这里不调用
    st.rerun()：多调一次会让 st.rerun 之后的代码被整体跳过，而且快速重跑
    在部分环境下（如 AppTest）不会被执行，反而看不清真实行为。
    """
    if branch_id not in branch_ids(session_name):
        st.error(f"分支不存在：{branch_id}")
        return
    data = read_branch(session_name, branch_id)
    write_branch(session_name, branch_id, data)  # 不存在时补一个空分支文件
    meta = load_session_meta(session_name)
    meta["current_branch"] = branch_id
    write_session_meta(session_name, meta)
    st.session_state.current_branch = branch_id


def rename_branch(session_name: str, branch_id: str, new_id: str = None, input_key: str = None) -> None:
    """分支改名：改名等于改 ID，因此分支文件要一起重命名（走回调）。"""
    if new_id is None:
        new_id = st.session_state.get(input_key, "")
    new_id = (new_id or "").strip()
    if not new_id or new_id == branch_id:
        return
    try:
        branch_path(session_name, new_id)  # 借它做非法名校验
    except ValueError as e:
        st.error(str(e))
        return
    meta = load_session_meta(session_name)
    items = normalize_branches(meta.get("branches"))
    if any(b["id"] == new_id for b in items):
        st.error(f"分支名已存在：{new_id}")
        return
    try:
        old_path, new_path = branch_path(session_name, branch_id), branch_path(session_name, new_id)
        if old_path.exists():
            if new_path.exists():
                st.error(f"分支文件已存在：{new_path.name}")
                return
            os.replace(old_path, new_path)
        for item in items:
            if item["id"] == branch_id:
                item["id"] = new_id
        meta["branches"] = items
        if meta.get("current_branch") == branch_id:
            meta["current_branch"] = new_id
        write_session_meta(session_name, meta)
        if st.session_state.get("current_branch") == branch_id and session_name == st.session_state.get("current_session"):
            st.session_state.current_branch = new_id
            st.session_state._message_branch = new_id
    except Exception as e:  # noqa: BLE001
        st.error(f"分支改名失败: {e}")


def delete_branch(session_name: str, branch_id: str) -> None:
    """删除一条分支；只剩一条时不允许删。

    删掉的分支如果有子分支，把子分支的 parent 改挂到它的 parent 上
    （fork_index 不变），这样分支树不会断。
    """
    try:
        meta = load_session_meta(session_name)
        items = normalize_branches(meta.get("branches"))
        if len(items) <= 1:
            st.error("至少要保留一条分支")
            return
        me = next((b for b in items if b["id"] == branch_id), None)
        if me is None:
            return
        remaining = [b for b in items if b["id"] != branch_id]
        for item in remaining:
            if item.get("parent") == branch_id:
                item["parent"] = me.get("parent")
        current = meta.get("current_branch")
        if current == branch_id:
            # 优先跳回父分支，没有父就跳第一条
            current = me.get("parent") if any(b["id"] == me.get("parent") for b in remaining) else remaining[0]["id"]
        meta["branches"] = remaining
        meta["current_branch"] = current
        write_session_meta(session_name, meta)
        path = branch_path(session_name, branch_id)
        if path.exists():
            path.unlink()
        if session_name == st.session_state.get("current_session"):
            previous = st.session_state.get("current_branch")
            st.session_state.current_branch = current
            if current != previous:
                st.session_state.message = read_branch(session_name, current).get("message", [])
    except Exception as e:  # noqa: BLE001
        st.error(f"删除分支失败: {e}")


def load_selected_session(session_name: str) -> None:
    try:
        has_archive = _session_exists(session_name)
        draft = draft_path(session_name)
        has_draft = draft is not None and draft.exists()
        if not has_archive and not has_draft:
            return
        if has_archive:
            meta = load_session_meta(session_name)
            st.session_state.message = load_current_branch(session_name).get("message", [])
            # 会话级人设与参数：老存档把它们存在顶层，新结构存在 meta.json
            restore = dict(meta)
            restore.update({k: v for k, v in (meta.get("_legacy_data") or {}).items()})
        else:
            # 只有草稿（还没产生对话的会话）：恢复人设与参数，对话仍是空的
            with draft.open("r", encoding="utf-8") as f:
                restore = json.load(f)
            meta = default_session_meta(session_name)
            meta["title"] = restore.get("title") or session_name
            st.session_state.message = restore.get("message", [])
        for key, default in DEFAULT_PROFILE.items():
            st.session_state[key] = restore.get(key) or default
        # 高级参数跟着会话走；老存档没这些字段就回退成默认值
        apply_advanced(restore)
        # 会话名称与置顶状态；老存档没这些字段就回退成默认值
        st.session_state.session_title = (restore.get("title") or "").strip() or session_name
        st.session_state.session_pinned = bool(restore.get("pinned"))
        st.session_state.current_branch = meta.get("current_branch") or "main"
        # 标记"内存里的消息属于哪条分支"，渲染时据此判断要不要从磁盘重载
        st.session_state._message_branch = st.session_state.current_branch
        st.session_state.current_session = session_name
    except Exception as e:  # noqa: BLE001 - 单个存档损坏不应中断整个页面
        st.error(f"加载会话失败: {e}")
        return
    st.rerun()  # 刷新页面


def _reset_advanced() -> None:
    """把高级生成参数恢复成默认值（新建/删除会话时用）。"""
    for key, value in DEFAULT_ADVANCED.items():
        st.session_state[key] = value


def reset_advanced_and_save() -> None:
    """「恢复默认值」按钮的回调：还原参数并立刻落盘。"""
    _reset_advanced()
    save_session()


def new_session() -> None:
    """新建会话：先把当前会话落盘，再清空人设与对话、换新 ID 和默认名称。

    必须作为按钮的 on_click 回调执行：回调在控件实例化之前运行，
    否则修改 st.session_state['session_title']（已被输入框占用）会报
    StreamlitWidgetAlreadyInstantiatedError。
    """
    save_session()
    reset_profile()
    _reset_advanced()
    st.session_state.current_session = new_session_id()
    st.session_state.session_title = DEFAULT_SESSION_TITLE
    st.session_state.session_pinned = False
    st.session_state.current_branch = "main"
    save_session()


def delete_session(session_name: str) -> None:
    """删除会话：整目录（含所有分支）与草稿一起清掉。"""
    try:
        directory = _session_dir(session_name)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        flat = directory.with_suffix(".json")  # 兼容还没迁移的旧扁平存档
        if flat.exists():
            flat.unlink()
        discard_draft(session_name)
        if session_name == st.session_state.get("current_session"):
            reset_profile()
            _reset_advanced()
            st.session_state.current_session = new_session_id()
            st.session_state.session_title = DEFAULT_SESSION_TITLE
            st.session_state.session_pinned = False
            st.session_state.current_branch = "main"
    except Exception as e:  # noqa: BLE001
        st.error(f"删除会话失败: {e}")
        return
    st.rerun()  # 刷新页面


# --------------------------------------------------------------------------- #
# OpenAI 客户端：API Key 缺失时给出清晰提示，而不是抛 SDK 的堆栈
# --------------------------------------------------------------------------- #
def require_api_key() -> str:
    """每次脚本运行都校验一次 API Key，缺失就停在这一步并给出可操作的提示。"""
    load_dotenv_file()
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        st.error(
            "未读取到环境变量 `DEEPSEEK_API_KEY`，无法调用 DeepSeek 接口。\n\n"
            "请在系统环境变量或项目根目录的 `.env` 中配置后重启应用。"
        )
        st.stop()
    return api_key


@st.cache_resource(show_spinner=False)
def get_client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def trim_context(messages: list) -> list:
    """从最新消息往前取，同时受条数与字符数限制（system prompt 不在此列表内）。"""
    kept, used, dropped = [], 0, False
    for msg in reversed(messages):
        cost = len(str(msg.get("content", ""))) + 8
        if kept and (len(kept) >= MAX_CONTEXT_MESSAGES or used + cost > MAX_CONTEXT_CHARS):
            dropped = True
            break
        kept.append(msg)
        used += cost
    if dropped:
        st.caption("⚠️ 对话较长，本次请求只发送了最近的部分历史记录。")
    return list(reversed(kept))


def build_messages() -> list:
    system_content = SYSTEM_PROMPT.format(**profile_from_state())
    history = []
    for msg in trim_context(st.session_state.message):
        # 官方示例会把 reasoning_content 一起回传，API 会忽略它、也不计入上下文
        if msg.get("role") == "assistant" and msg.get("reasoning_content"):
            history.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "reasoning_content": msg["reasoning_content"],
            })
        else:
            history.append({"role": msg["role"], "content": msg.get("content", "")})
    return [{"role": "system", "content": system_content}, *history]


def advanced_value(name: str):
    return st.session_state.get(name, DEFAULT_ADVANCED[name])


def advanced_from_state() -> dict:
    """当前的高级生成参数快照（存进会话存档用）。"""
    return {key: st.session_state.get(key, DEFAULT_ADVANCED[key]) for key in ADVANCED_KEYS}


def apply_advanced(data: dict) -> None:
    """用存档里的高级参数覆盖当前设置；缺失的键回退成默认值。

    只能从回调里调用（回调先于控件实例化），否则改这些已被滑块占用的 key 会报
    StreamlitWidgetAlreadyInstantiatedError。
    """
    for key, default in DEFAULT_ADVANCED.items():
        value = data.get(key, default)
        st.session_state[key] = default if value is None else value


def build_request_payload() -> dict:
    """按侧边栏的高级配置拼装请求参数，未启用的项不发送。"""
    payload = {
        "model": MODEL_NAME,
        "messages": build_messages(),
        "stream": True,
        "temperature": advanced_value("temperature"),
        "top_p": advanced_value("top_p"),
        "extra_body": {
            "thinking": {"type": "enabled" if st.session_state.thinking else "disabled"},
        },
    }
    if st.session_state.thinking:
        payload["reasoning_effort"] = "low"
    if advanced_value("limit_tokens"):
        payload["max_tokens"] = int(advanced_value("max_tokens"))
    if advanced_value("frequency_penalty"):
        payload["frequency_penalty"] = advanced_value("frequency_penalty")
    if advanced_value("presence_penalty"):
        payload["presence_penalty"] = advanced_value("presence_penalty")
    return payload


def _first_choice(chunk):
    choices = getattr(chunk, "choices", None)
    if not choices:
        return None
    return choices[0]


def _delta_text(chunk) -> tuple:
    """返回本片段里的 (推理内容, 正文内容)，两者都可能为空字符串。"""
    choice = _first_choice(chunk)
    delta = getattr(choice, "delta", None) if choice is not None else None
    if delta is None:
        return "", ""
    reasoning = getattr(delta, "reasoning_content", None) or ""
    content = getattr(delta, "content", None) or ""
    return reasoning, content


def _remember_expander(widget_key: str, state_key: str) -> None:
    """把用户手动展开/折叠的状态镜像到状态键，避免每次重跑都被重置。"""
    st.session_state[state_key] = st.session_state[widget_key]


def reasoning_expander(key: str, state_key: str, expanded: bool):
    """带记忆的「思考过程」折叠面板：用户手动开合后不会被流式重跑重置。

    type="compact" 是 Streamlit 官方推荐的思考块样式（无边框内联展开），
    与 ChatGPT / DeepSeek 的「思考过程」观感一致。
    """
    st.session_state.setdefault(state_key, expanded)
    return st.expander(
        "思考过程",
        expanded=st.session_state[state_key],
        key=key,
        type="compact",
        icon=":material/neurology:",
        on_change=_remember_expander,
        args=(key, state_key),
    )


def render_reply(placeholder) -> None:
    """请求模型，把流式推理渲染进折叠面板、正文用 st.write_stream 渲染。

    正文交给 st.write_stream（官方推荐的流式渲染方式）；推理片段在同一趟迭代里
    顺带写进「思考过程」折叠面板，所以两种内容仍然是边收边显示。
    异常在这里被消化成 st.error，调用方（页面脚本）不再需要 try/except。
    """
    reasoning = ""
    full_response = ""  # 异常路径下也要有定义，后面才能安全判断
    thinking_enabled = bool(st.session_state.thinking)
    try:
        response = client.chat.completions.create(**build_request_payload())

        def stream_body():
            """边消费模型流边渲染：先出推理，再流式吐正文。"""
            nonlocal reasoning
            for chunk in response:
                reasoning_piece, content_piece = _delta_text(chunk)
                if reasoning_piece:
                    reasoning += reasoning_piece
                    if thinking_enabled:
                        # 推理内容只更新占位符，不重建整条消息
                        reasoning_text.markdown(
                            format_reasoning_html(reasoning, streaming=True),
                            unsafe_allow_html=True,
                        )
                if content_piece:
                    if thinking_enabled and reasoning_text is not None:
                        # 思考结束：撤掉"思考中"的微光提示，把完整推理定格
                        if reasoning:
                            reasoning_text.markdown(
                                format_reasoning_html(reasoning),
                                unsafe_allow_html=True,
                            )
                        else:
                            reasoning_text.markdown("_（本次没有输出推理内容）_")
                    yield content_piece

        with placeholder.chat_message("assistant"):
            reasoning_placeholder, reasoning_text = None, None
            if thinking_enabled:
                reasoning_placeholder = reasoning_expander("reasoning_panel", "show_reasoning", True)
                with reasoning_placeholder:
                    reasoning_text = st.empty()
                    # 官方推荐的进行中提示：微光文字本身就是状态指示
                    reasoning_text.markdown(":shimmer[思考中…]")
            # 正文渲染交给 st.write_stream（占位容器保留在聊天气泡里）。
            # 没有产出时它返回空列表，统一成空字符串便于后续判断
            full_response = st.write_stream(stream_body()) or ""
    except Exception as e:  # noqa: BLE001 - 网络/限流/余额等异常不该把页面打成 traceback
        placeholder.empty()
        st.error(f"请求模型失败：{e}")
        st.caption("本条消息已保留在对话中，可直接重试或继续输入。")

    if full_response:
        reply = {"role": "assistant", "content": full_response}
        if reasoning:
            reply["reasoning_content"] = reasoning
        st.session_state.message.append(reply)
    else:
        st.warning("模型没有返回任何内容，本次回答未记录。")

    existed = session_file_exists()
    save_session()
    if not existed and session_file_exists():
        # 本次是这条会话的第一个存档：侧边栏是在本轮脚本顶部渲染的，那时文件还
        # 不存在，所以必须主动重跑一次，会话才会立刻出现在会话历史里。
        # （不能只依赖 chat_input 提交后浏览器的隐式重跑，实测它不保证发生。）
        st.rerun()


def render_history() -> None:
    """按存储顺序回放历史：带推理的回答把思考过程放进折叠面板。"""
    for index, msg in enumerate(st.session_state.message):
        with st.chat_message(msg["role"]):
            if msg.get("reasoning_content"):
                with reasoning_expander(f"history_reasoning_{index}", f"history_reasoning_open_{index}", False):
                    # 引用块，原生md，不存在html懒渲染问题
                    st.markdown(
                        format_reasoning_html(msg["reasoning_content"]),
                        unsafe_allow_html=True,
                    )

            if msg.get("content"):
                st.markdown(msg["content"])

def format_reasoning_html(text: str, streaming: bool = False) -> str:
    """把推理内容包装成紧凑样式的 HTML，流式时带光标。"""
    cursor = "▌" if streaming else ""
    escaped = (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
    return (
        '<div style="font-size: 0.875rem; line-height: 1.6; '
        'color: var(--text-color); opacity: 0.75; '
        'white-space: pre-wrap; font-family: inherit;">'
        f'{escaped}{cursor}</div>'
    )

# --------------------------------------------------------------------------- #
# 页面配置（必须是第一个 Streamlit 命令）
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="凌恒的酒馆",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else ":material/local_bar:",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Report a bug": "https://github.com/LingHeng42",
        "About": "凌恒的酒馆是一个基于人工智能的聊天平台，旨在为用户提供一个有趣、互动和智能的聊天体验。",
    },
)

# 会话状态初始化
for _key, _value in DEFAULT_PROFILE.items():
    st.session_state.setdefault(_key, _value)
for _key, _value in DEFAULT_ADVANCED.items():
    st.session_state.setdefault(_key, _value)
st.session_state.setdefault("message", [])
st.session_state.setdefault("thinking", False)
st.session_state.setdefault("session_title", DEFAULT_SESSION_TITLE)
# 当前所在分支（一条分支 = 一条线性消息列表；切换分支就是整体换掉 message）
st.session_state.setdefault("current_branch", "main")
if "current_session" not in st.session_state:
    st.session_state.current_session = new_session_id()

# 每次脚本运行都校验 Key（函数本身不缓存），缓存只作用在客户端构造上
client = get_client(require_api_key())

# logo（文件缺失时不影响页面）
# if LOGO_PATH.exists():
#     try:
#         st.logo(str(LOGO_PATH), size="large")
#     except Exception:  # noqa: BLE001 - 老版本 streamlit 没有 st.logo
#         pass

# 展示历史对话（开启过深度思考的回答会带上可折叠的思考过程）
st.header(st.session_state.session_title)
render_history()

# --------------------------------------------------------------------------- #
# 侧边栏
# --------------------------------------------------------------------------- #
with st.sidebar:
    # 标题：小号字放在 logo 右侧
    logo_col, title_col = st.columns([1, 6], vertical_alignment="center")
    with logo_col:
        if LOGO_PATH.exists():
            try:
                st.image(str(LOGO_PATH), width=64)
            except Exception:  # noqa: BLE001 - 图片渲染失败不影响功能
                pass
    with title_col:
        st.markdown("### 凌恒的酒馆")
        st.caption("AI 角色扮演聊天")

    st.divider()

    # 新建会话：状态改动全部放在 on_click 回调里（回调先于控件实例化执行）。
    # 处于"新建会话"状态（还没有产生对话）时用主色高亮，产生对话、会话进入历史后
    # 自动恢复成普通按钮
    st.button(
        "新建会话",
        width="stretch",
        icon=":material/add_comment:",
        type="primary" if is_fresh_session() else "secondary",
        on_click=new_session,
    )

    # 会话名称：与存档文件名（会话 ID）解耦，改名不会新建/移动文件
    # 注意：控件 key 直接占用 "session_title"，所以只能在回调里改这个键
    # st.text_input(
    #     "会话名称",
    #     value=st.session_state.session_title,
    #     key="session_title",
    #     placeholder=DEFAULT_SESSION_TITLE,
    #     help="只影响显示，不会改变存档文件名（文件名始终是会话 ID 时间戳）。",
    # )

    st.subheader("会话历史")
    session_list = load_session_list()
    if not session_list:
        st.caption("还没有会话。发出第一条消息后，这里会出现本次会话。")
    for session in session_list:
        current = session == st.session_state.current_session
        meta = load_session_meta(session)
        # 当前会话的名称/昵称直接取内存里的，避免与输入框内容不一致。
        # 「是不是当前会话」由按钮颜色（type=primary）表示，名称里不再加"（当前）"
        label = st.session_state.session_title if current else meta["title"]
        nickname = st.session_state.nickname if current else (meta.get("nickname") or DEFAULT_PROFILE["nickname"])
        pin_mark = ":material/push_pin: " if meta["pinned"] else ""
        col1, col2 = st.columns([4, 1])
        with col1:
            st.button(
                f"{pin_mark}{nickname}-{label}",
                width="stretch",
                key=f"session_{session}",
                help=f"会话 ID：{session}",
                type="primary" if current else "secondary",
                on_click=lambda s=session: load_selected_session(s),
            )
        with col2:
            # 一个入口搞定：重命名 / 置顶 / 删除。
            # key 一律只用会话 ID，不带列表序号：删掉上一条会整体上移，
            # 带序号的话弹层展开状态和输入框内容会串到下一条会话上。
            with st.popover("⋯", width="stretch", help="重命名、置顶或删除这个会话", key=f"menu_{session}"):
                # 重命名：输入框里改完按回车即生效。
                # 必须用 on_change 回调：回调在控件实例化之前执行，这样改
                # st.session_state['session_title'] 才是合法操作。
                st.text_input(
                    "重命名",
                    value=label,
                    key=f"rename_input_{session}",
                    on_change=rename_session,
                    args=(session,),
                    kwargs={"new_title": None, "input_key": f"rename_input_{session}"},
                )
                if meta["pinned"]:
                    st.button(
                        "取消置顶",
                        key=f"unpin_{session}",
                        width="stretch",
                        icon=":material/push_pin:",
                        on_click=lambda s=session: set_pinned(s, False),
                    )
                else:
                    st.button(
                        "置顶",
                        key=f"pin_{session}",
                        width="stretch",
                        icon=":material/keep:",
                        on_click=lambda s=session: set_pinned(s, True),
                    )
                with st.popover(
                    "删除会话",
                    icon=":material/delete:",
                    width="stretch",
                    type="primary",
                    key=f"delete_menu_{session}",
                ):
                    st.button(
                        "确认删除",
                        key=f"confirm_delete_{session}",
                        width="stretch",
                        type="primary",
                        icon=":material/delete_forever:",
                        on_click=lambda s=session: delete_session(s),
                    )

    # 分支管理：一个会话可以有多条分支（编辑消息 / 重新生成都会新建分支）
    sid = st.session_state.current_session
    if session_file_exists(sid):
        meta = load_session_meta(sid)
        disk_branch = meta.get("current_branch") or "main"
        st.session_state.current_branch = disk_branch
        # 以磁盘为准：如果内存里的消息还是另一条分支的，就把当前分支的消息读回来。
        # 这样"点击切换分支"只依赖回调写盘，不依赖回调改内存状态
        # （AppTest 等场景下，控件回调结束后内存状态可能被回滚到点击之前）。
        if st.session_state.get("_message_branch") != disk_branch:
            st.session_state.message = read_branch(sid, disk_branch).get("message", [])
            st.session_state._message_branch = disk_branch
        current_branch = disk_branch
        branch_list = branch_ids(sid)
        st.subheader(f"分支（{len(branch_list)}）")
        if len(branch_list) == 1:
            st.caption("编辑消息或重新生成时会自动新建分支，旧分支会保留。")
        for branch in branch_list:
            active = branch == current_branch
            bcol1, bcol2 = st.columns([4, 1])
            with bcol1:
                st.button(
                    branch,
                    key=f"branch_{sid}_{branch}",
                    width="stretch",
                    type="primary" if active else "secondary",
                    help=f"分支 {branch}",
                    on_click=switch_branch,
                    args=(sid, branch),
                )
            with bcol2:
                with st.popover("⋯", width="stretch", help="重命名或删除这条分支",
                                key=f"branch_menu_{sid}_{branch}"):
                    st.text_input(
                        "重命名分支",
                        value=branch,
                        key=f"branch_rename_{sid}_{branch}",
                        on_change=rename_branch,
                        args=(sid, branch),
                        kwargs={"new_id": None, "input_key": f"branch_rename_{sid}_{branch}"},
                    )
                    st.button(
                        "删除分支",
                        key=f"branch_delete_{sid}_{branch}",
                        width="stretch",
                        icon=":material/delete:",
                        disabled=len(branch_list) <= 1,
                        on_click=delete_branch,
                        args=(sid, branch),
                    )

    st.divider()

    # 角色管理：控件 key 直接就是状态键；on_change 里立刻落盘，
    # 不需要手动回写（回写会在控件实例化后改同名 session_state 而报错）
    st.subheader("管理角色")
    st.text_input("昵称", key="nickname", placeholder="请输入昵称", on_change=save_session)
    st.text_area("性格", key="nature", placeholder="请输入性格描述", on_change=save_session)
    st.text_area("角色简介", key="role_description", placeholder="请输入角色简介", on_change=save_session)
    st.text_area("输出规则", key="output_rules", placeholder="请输入输出规则", on_change=save_session)

    st.divider()

    # 生成参数
    st.subheader("生成参数")
    st.toggle(
        "深度思考",
        key="thinking",
        help="开启后模型会先思考再回答；关闭则直接作答。",
    )

    # 高级配置：默认折叠，点开才展开（展开状态会被记住）。
    # 每个控件都挂 on_change=save_session：改完立刻写进存档/草稿
    with st.expander("⚙️ 高级配置", expanded=False, key="advanced_panel"):
        st.slider(
            "温度 (temperature)",
            min_value=0.0,
            max_value=2.0,
            step=0.05,
            key="temperature",
            on_change=save_session,
            help="越高越天马行空、越低越稳定保守。角色扮演想有个性可以调高，想稳定复现就调低。",
        )
        st.slider(
            "核采样 (top_p)",
            min_value=0.0,
            max_value=1.0,
            step=0.05,
            key="top_p",
            on_change=save_session,
            help="top_p=1：全部词汇都参与采样，随机性最大；top_p 越小：候选词越少，输出越确定、保守",
        )
        st.slider(
            "重复惩罚 (frequency_penalty)",
            min_value=-2.0,
            max_value=2.0,
            step=0.1,
            key="frequency_penalty",
            on_change=save_session,
            help="正值会降低重复用词的概率，负值鼓励复读。",
        )
        st.slider(
            "话题新鲜度 (presence_penalty)",
            min_value=-2.0,
            max_value=2.0,
            step=0.1,
            key="presence_penalty",
            on_change=save_session,
            help="正值鼓励聊新话题，负值鼓励围绕已有内容展开。",
        )
        st.checkbox("限制单次回复长度", key="limit_tokens", on_change=save_session,
                    help="不勾选则由服务端决定上限。")
        if st.session_state.limit_tokens:
            st.number_input(
                "最大 tokens",
                min_value=256,
                max_value=32768,
                step=256,
                key="max_tokens",
                on_change=save_session,
                help="单次回答（不含思考过程）的长度上限。",
            )
        st.button("恢复默认值", width="stretch", on_click=reset_advanced_and_save)

# --------------------------------------------------------------------------- #
# 对话框
# --------------------------------------------------------------------------- #
prompt = st.chat_input(
    "请输入您的想法...",
    # 回答流式生成中禁用输入，避免误触打断（官方推荐的 chat 用法）
    submit_mode="disable",
)

if prompt:
    st.session_state.message.append({"role": "user", "content": prompt})  # 用户输入入列
    st.chat_message("user").write(prompt)
    # 复用同一个占位符，避免每个 chunk 都重建组件
    render_reply(st.empty())
