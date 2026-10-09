"""会话的读写与生命周期：元信息、分支文件、草稿、切换、新建、删除。

一条会话 = 一个目录；会话级信息（名称/置顶/当前分支/分支列表/人设/高级参数/头像）
放在 meta.json，消息按分支放在 branches/<分支>.json。
"""

import datetime
import json
import shutil
from pathlib import Path

import streamlit as st

from . import config, images, profile, store



def session_dir_exists(session_name: str) -> bool:
    """会话是否已经以新结构（目录 + meta.json）存过盘。"""
    try:
        return (store._session_dir(session_name) / config.META_FILENAME).exists()
    except ValueError:
        return False


def load_session_meta(session_name: str) -> dict:
    """读会话级元信息：名称、置顶、当前分支、分支列表。

    - 新结构：<会话目录>/meta.json
    - 旧结构（扁平 <会话ID>.json）：现场合成一份等价元信息，首次保存时自动迁移
    - 什么都没有：给出默认值（一个空会话，只有 main 分支）
    """
    try:
        path = store._session_dir(session_name) / config.META_FILENAME
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
    flat = store._session_dir(session_name).with_suffix(".json")  # 兼容旧扁平存档
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
    """把 branches 字段规整成 [{id, parent, fork_index, created_at}]，并按创建时间排序。

    为什么按创建时间排而不是按 ID 排：分支 ID 可能比创建顺序"忽大忽小"
    （删掉中间一条后，新分支会补上那个空缺的 ID），只按 ID 排会显得很乱。
    创建时间相同或缺失的（例如从旧存档迁移过来的）保持原有顺序（稳定排序）。
    """
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
    if not out:
        return [_branch("main")]
    known = [b for b in out if b["created_at"]]
    unknown = [b for b in out if not b["created_at"]]
    return sorted(known, key=lambda b: b["created_at"]) + unknown

def branch_ids(session_name: str) -> list:
    """该会话已有哪些分支（顺序即创建顺序，main 在最前）。"""
    return [b["id"] for b in normalize_branches(load_session_meta(session_name).get("branches"))]

def own_meta(meta: dict) -> dict:
    """去掉内部字段，只留要写盘的部分（`_legacy*` 是只读兼容用的临时键）。"""
    return {k: v for k, v in meta.items() if not k.startswith("_")}


def write_session_meta(session_name: str, meta: dict) -> None:
    """原子写 meta.json（调用方保证已 setdefault 出 current_branch）。"""
    store._write_json_atomic(store._session_dir(session_name) / config.META_FILENAME, own_meta(meta))


def read_branch(session_name: str, branch_id: str) -> dict:
    """读一条分支的内容；没有文件时返回空消息列表。"""
    try:
        path = store.branch_path(session_name, branch_id)
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
    store._write_json_atomic(store.branch_path(session_name, branch_id), data)


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
        directory = store._session_dir(session_name)
    except ValueError:
        return
    flat = directory.with_suffix(".json")
    if not flat.exists() or (directory / config.META_FILENAME).exists():
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
    for key in set(config.PROFILE_KEYS) | set(config.ADVANCED_KEYS):
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
                store._write_json_atomic(draft, data)
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
    title = (new_title or "").strip() or store.DEFAULT_SESSION_TITLE
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
    for key, value in config.DEFAULT_PROFILE.items():
        st.session_state[key] = value
    st.session_state.message = []


def reset_avatar() -> None:
    """把对话头像恢复成默认（新建/删除会话时用；图片文件留在附件目录里不删）。

    注意：**不能**给 `_uploader_*` 这类 file_uploader 的控件 key 赋值——
    Streamlit 把控件 key 视为只读，赋值会抛
    StreamlitValueAssignmentNotAllowedError（点"新建会话"就崩在这里）。
    上传控件里残留的文件靠 `_avatar_uploaded_for` / `_avatar_applied` 两个
    标记忽略掉：前者让"别的会话选的文件"不生效，后者让同一张图不重复处理。
    """
    for key in config.AVATAR_KEYS:
        st.session_state[key] = None
    st.session_state._avatar_uploaded_for = None
    st.session_state._avatar_applied = None


def draft_path(session_name: str = None):
    """草稿文件路径：给"还没产生对话"的会话存人设与高级参数。"""
    name = session_name or st.session_state.get("current_session")
    if not name or Path(name).name != name:
        return None
    return config.drafts_dir() / f"{name}.draft"


def append_draft(data: dict) -> None:
    """把内容合并进草稿（保留草稿里已有的字段，比如刚设的头像）。"""
    path = draft_path()
    if path is None:
        return
    merged = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                merged = json.load(f)
        except Exception:  # noqa: BLE001 - 草稿损坏时按空处理
            merged = {}
    merged.update(data)
    store._write_json_atomic(path, merged)


def save_draft(data: dict) -> None:
    """把人设/高级参数立刻写进草稿文件（不进会话历史）。"""
    path = draft_path()
    if path is None:
        return
    if not data.get("message"):
        append_draft(data)


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
            **profile.profile_from_state(),
            **profile.advanced_from_state(),
            **profile.avatar_from_state(),
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
    # 人设、高级参数与头像是会话级的一套，跟着 meta 走
    meta.update(profile.profile_from_state())
    meta.update(profile.advanced_from_state())
    meta.update(profile.avatar_from_state())
    write_branch(session_name, branch_id, {"message": messages})
    write_session_meta(session_name, meta)
    discard_draft()


def load_session_list() -> list:
    """会话历史：置顶的排在最前，其余按时间从新到旧。

    兼容两种结构：新结构（<会话ID>/meta.json 目录）与旧扁平存档（<会话ID>.json）。
    空对话不落盘，所以这里列出来的都是聊过的会话。
    """
    if not config.archive_dir().exists():
        return []
    names = set()
    for entry in config.archive_dir().iterdir():
        if entry.is_dir():
            # 会话目录（drafts 等内部目录会被 session_dir_exists 过滤掉）
            if (entry / config.META_FILENAME).exists() or entry.with_suffix(".json").exists():
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
        directory = config.archive_dir() / name
    except Exception:  # noqa: BLE001
        return False
    return (directory / config.META_FILENAME).exists() or directory.with_suffix(".json").exists()


def session_file_exists(session_name: str = None) -> bool:
    """当前会话是否已有存档（= 侧边栏会话历史里能否列出它）。"""
    return _session_exists(session_name)


def is_fresh_session() -> bool:
    """当前是否处于"新建会话"状态。

    判定标准：这个会话还没有落盘（= 还没有产生任何对话）。一旦发出第一条消息，
    存档建立、会话进入历史，就不再是"新建会话"状态了。
    """
    return not session_file_exists()

def load_selected_session(session_name: str) -> None:
    try:
        has_archive = _session_exists(session_name)
        draft = draft_path(session_name)
        has_draft = draft is not None and draft.exists()
        if not has_archive and not has_draft:
            # 还没有任何存档的会话（比如刚新建的 ID）：当成全新会话，
            # 显式重置人设与头像，避免上一个会话的设置"漏"过来
            reset_profile()
            reset_avatar()
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
        for key, default in config.DEFAULT_PROFILE.items():
            st.session_state[key] = restore.get(key) or default
        # 高级参数跟着会话走；老存档没这些字段就回退成默认值
        profile.apply_advanced(restore)
        # 头像同样是会话级的一套；老存档没这个字段就回落成默认头像
        for key in config.AVATAR_KEYS:
            st.session_state[key] = restore.get(key) or None
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
    for key, value in config.DEFAULT_ADVANCED.items():
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
    st.session_state.current_session = store.new_session_id()
    st.session_state.session_title = store.DEFAULT_SESSION_TITLE
    st.session_state.session_pinned = False
    st.session_state.current_branch = "main"
    # 头像是每个会话独立设置的：新会话从默认头像开始
    reset_avatar()
    save_session()


def delete_session(session_name: str) -> None:
    """删除会话：整目录（含所有分支）与草稿一起清掉。"""
    try:
        directory = store._session_dir(session_name)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        flat = directory.with_suffix(".json")  # 兼容还没迁移的旧扁平存档
        if flat.exists():
            flat.unlink()
        discard_draft(session_name)
        if session_name == st.session_state.get("current_session"):
            reset_profile()
            _reset_advanced()
            reset_avatar()
            st.session_state.current_session = store.new_session_id()
            st.session_state.session_title = store.DEFAULT_SESSION_TITLE
            st.session_state.session_pinned = False
            st.session_state.current_branch = "main"
    except Exception as e:  # noqa: BLE001
        st.error(f"删除会话失败: {e}")
        return
    st.rerun()  # 刷新页面
