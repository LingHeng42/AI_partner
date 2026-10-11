"""会话的读写与生命周期：元信息、分支文件、草稿、切换、新建、删除。

一条会话 = 一个目录；会话级信息（名称/置顶/当前分支/分支列表/人设/高级参数/头像）
放在 meta.json，消息按分支放在 branches/<分支>.json。
"""

import datetime
from pathlib import Path

import streamlit as st

from . import config, profile, storage, store



def _meta_key(session_name: str) -> str:
    """meta.json 的键名。"""
    return f"{store._session_dir(session_name)}/{config.META_FILENAME}"


def _draft_key(session_name: str = None):
    """草稿键名（还没产生对话的会话）。名字不合法时返回 None。"""
    name = session_name or st.session_state.get("current_session")
    if not name or Path(name).name != name:
        return None
    return f"{store.SESSIONS_ROOT}/drafts/{name}.draft"


def _legacy_key(session_name: str) -> str:
    """旧扁平存档（sessions/<会话ID>.json）的键名，只读兼容用。"""
    return f"{store.SESSIONS_ROOT}/{Path(session_name).name}.json"


# --------------------------------------------------------------------------- #
# 内存消息的归属标记
#
# 必须同时包含**会话 ID 与分支 ID**：只比分支名是不够的——每个会话的分支都叫
# main，切会话时会被误判成"已经加载过"，于是主区域继续显示上一个会话的内容
# （表现为"点了历史会话、按钮变红但对话内容没变"）。
# --------------------------------------------------------------------------- #
OWNER_STATE = "_message_owner"


def message_owner(session_name: str = None, branch_id: str = None) -> str:
    """内存里的 message 属于哪个会话的哪条分支。"""
    sid = session_name if session_name is not None else st.session_state.get("current_session")
    bid = branch_id if branch_id is not None else st.session_state.get("current_branch")
    return f"{sid or ''}::{bid or 'main'}"


def mark_message_owner(session_name: str = None, branch_id: str = None) -> str:
    """记下当前内存消息的归属，返回这个标记。"""
    owner = message_owner(session_name, branch_id)
    st.session_state[OWNER_STATE] = owner
    return owner


def session_dir_exists(session_name: str) -> bool:
    """会话是否已经以新结构（目录 + meta.json）存过盘。"""
    try:
        return storage.exists(_meta_key(session_name))
    except ValueError:
        return False


def load_session_meta(session_name: str) -> dict:
    """读会话级元信息：名称、置顶、当前分支、分支列表。

    - 新结构：<会话目录>/meta.json
    - 旧结构（扁平 <会话ID>.json）：现场合成一份等价元信息，首次保存时自动迁移
    - 确实没有：给出默认值（一个空会话，只有 main 分支）

    **读失败会抛异常**，不会静默变成"空会话"：静默降级会让上层把默认值写回磁盘，
    等于用一次网络抖动抹掉用户的会话信息。
    """
    try:
        key = _meta_key(session_name)
    except ValueError:
        return default_session_meta(session_name)
    data = storage.read_json(key, None)
    if isinstance(data, dict):
        data["_legacy"] = False
        return data
    if data is not None:
        raise ValueError(f"meta.json 结构不对：{key}")
    flat = _legacy_key(session_name)  # 兼容旧扁平存档
    if storage.exists(flat):
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


def _legacy_meta(session_name: str, flat: str) -> dict:
    """把旧扁平存档合成新结构需要的元信息（先只读，落盘时再迁移）。

    读不出来时**抛异常**：调用方（例如"改名"）会把 meta 写回去，静默给默认值
    等于把旧存档的标题/人设一起抹掉。
    """
    meta = default_session_meta(session_name)
    data = storage.read_json(flat, None)
    if not isinstance(data, dict):
        raise ValueError(f"旧存档内容读不出来：{flat}")
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
    storage.write_json(_meta_key(session_name), own_meta(meta))


def read_branch(session_name: str, branch_id: str) -> dict:
    """读一条分支的内容。

    - 分支文件**不存在**：合法的"空分支"，返回 {"message": []}
    - 文件存在但内容坏了 / 存储层报错：**抛异常**，绝不返回空

    第二点是血泪教训：以前任何异常都返回空列表，而"切换分支"会把读到的内容原样
    写回去 —— 于是一次读取失败就把这条分支的历史永久覆盖成空的（表现为"切换会话后
    历史变空、AI 也不再参考上下文"）。宁可让调用方看到错误，也不能静默丢数据。
    """
    try:
        key = store.branch_path(session_name, branch_id)
    except ValueError:
        return {"message": []}
    data = storage.read_json(key, None)
    if data is None:
        return {"message": []}
    if not isinstance(data, dict):
        raise ValueError(f"分支文件结构不对：{key}")
    return data


def write_branch(session_name: str, branch_id: str, data: dict, force: bool = False) -> None:
    """写一条分支。

    **防覆盖保护**：如果要把"零条消息"写到一个**已有内容**的分支上，默认拒绝。

    为什么把保护放在这个最底层：任何写入路径（保存会话、切换分支、编辑消息、
    重新生成）最终都会走到这里，放在这里就不可能被新代码绕过。它挡住的是
    "静默失败导致内存里没有消息"这类事故 —— 例如读超时被当成空分支，随后保存
    又把空内容写回磁盘，用户的对话就永久没了。

    判断依据是**原始内容是否存在**（而不是"里面有没有消息"）：分支文件常见内容
    ``{"message": []}`` 是合法的空分支，第一条消息必须能写进去。

    真的需要清空某条分支时，显式传 ``force=True``。
    """
    messages = data.get("message")
    if not force and isinstance(messages, list) and not messages:
        try:
            key = store.branch_path(session_name, branch_id)
            existing_raw = storage.read_bytes(key) if storage.exists(key) else b""
        except Exception:  # noqa: BLE001 - 连"确认现有内容"都做不到时，宁可不写
            st.warning("暂时读不到这条分支的现有内容，为避免误覆盖，本次不保存。")
            return
        if existing_raw:
            st.warning(
                "内存里没有消息，但存档里还有内容 —— 为避免用空内容覆盖历史，本次不保存。\n\n"
                "如果确实想清空这个会话，请新建一个会话。"
            )
            return
    storage.write_json(store.branch_path(session_name, branch_id), data)


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
        key = _meta_key(session_name)
    except ValueError:
        return
    flat = _legacy_key(session_name)
    if not storage.exists(flat) or storage.exists(key):
        return
    data = storage.read_json(flat)
    if not isinstance(data, dict):
        st.error("迁移旧存档失败（保留原文件）: 内容不是合法 JSON")
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
        storage.delete(flat)
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
        if draft is not None and storage.exists(draft):
            try:
                data = storage.read_json(draft, {}) or {}
                data.update(changes)
                storage.write_json(draft, data)
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
    """草稿的存储键名：给"还没产生对话"的会话存人设与高级参数。"""
    return _draft_key(session_name)


def append_draft(data: dict) -> None:
    """把内容合并进草稿（保留草稿里已有的字段，比如刚设的头像）。"""
    path = draft_path()
    if path is None:
        return
    merged = storage.read_json(path, {}) or {}
    merged.update(data)
    storage.write_json(path, merged)


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
    if path is not None and storage.exists(path):
        try:
            storage.delete(path)
        except OSError:
            pass


def draft_ids() -> list:
    """当前用户下所有草稿的会话 ID。"""
    prefix = f"{store.SESSIONS_ROOT}/{config.DRAFTS_SUBDIR}/"
    out = []
    for rel in storage.list_files(f"{store.SESSIONS_ROOT}/{config.DRAFTS_SUBDIR}"):
        if rel.startswith(prefix) and rel.endswith(".draft"):
            out.append(Path(rel).name[: -len(".draft")])
    return sorted(out)


def _session_id_age_days(session_id: str) -> float:
    """会话 ID 里带时间戳（如 ``2026-10-11_155423_140``），算出它离现在多少天。

    注意：时间戳部分是**前 17 个字符**（``%Y-%m-%d_%H%M%S`` = 10 + 1 + 6），
    后面还有 ``_`` 加 3 位毫秒。这里曾经写成 ``[:19]``，多切了一个字符导致
    strptime 永远失败、函数永远返回 0 —— 结果是草稿 GC 一个都清不掉（白写）。
    解析不出来时返回 0（当作"很新"）：宁可留一个陈旧草稿，也不误删用户的设置。
    """
    import datetime

    try:
        stamp = datetime.datetime.strptime(session_id[:17], "%Y-%m-%d_%H%M%S")
    except (ValueError, TypeError):
        return 0.0
    return (datetime.datetime.now() - stamp).total_seconds() / 86400.0


def purge_stale_drafts(keep_days: float = 1.0, current: str = None) -> int:
    """清掉**过期**的草稿，返回清掉的个数。

    为什么需要：每条"新建会话"都会立刻写一个草稿（这样改人设能即时落盘）。
    如果这条会话一直没发过消息，它就不进会话历史 —— 用户在界面上看不到、也删不掉
    它，草稿就永远留在库里，用几次就攒一堆 `.draft` 白占空间（用户就是这么发现的）。

    **判断标准必须是年龄，不能是"是不是当前会话"**：草稿里存着头像/人设等设置，
    用户切走再切回来时那些设置必须还在（这是明确的功能，有测试守着）。所以只清理
    明显过期的：默认保留最近 1 天内的草稿。

    current 指定要保留的会话（通常是当前会话，即使它很旧也留着）。
    """
    keep = current if current is not None else st.session_state.get("current_session")
    removed = 0
    for name in draft_ids():
        if name == keep:
            continue
        if _session_id_age_days(name) < keep_days:
            continue
        discard_draft(name)
        removed += 1
    return removed


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

    性能：这是最热的路径（每次渲染都要跑），所以 meta 用**一次批量读**拿回来，
    而不是每个会话单独读一次 —— 在 MongoDB 上那就是 N 次网络往返（实测 8 个会话
    会因此多花 1.6 秒）。
    """
    # 两类候选：新结构的会话目录，以及旧扁平存档 sessions/<会话ID>.json。
    # 注意**不能**只看"目录存在"就当会话存在：还没产生对话的会话也会建目录
    # （草稿放在里面），它们不该出现在历史里。
    dir_names = set(storage.session_ids())
    prefix = f"{store.SESSIONS_ROOT}/"
    flat_names = {
        Path(rel).stem
        for rel in storage.list_files(store.SESSIONS_ROOT)
        if rel.startswith(prefix) and rel.count("/") == 1 and rel.endswith(".json")
    }
    names = dir_names | flat_names
    if not names:
        return []
    # 一次把两类文件都读回来（同一批请求，不额外增加网络往返）
    wanted = [_meta_key(name) for name in names]
    wanted += [_legacy_key(name) for name in flat_names]
    blobs = storage.read_json_many(wanted)
    kept = {}
    branch_of = {}
    for name in names:
        data = blobs.get(_meta_key(name))
        if isinstance(data, dict):
            kept[name] = bool(data.get("pinned"))
            branch_of[name] = data.get("current_branch") or "main"
            continue
        # 旧扁平存档：只读兼容，**不做空会话过滤**（老文件可能没有 message 字段，
        # 那不代表它该被隐藏）。它会在下次保存时自动迁移成新结构。
        legacy = blobs.get(_legacy_key(name))
        if isinstance(legacy, dict):
            kept[name] = bool(legacy.get("pinned"))
            branch_of[name] = None
    if not kept:
        return []
    # **过滤掉"空会话"**：只剩新结构会话参与判断 —— meta.json 在、但当前分支里
    # 一条消息都没有的，不进历史。这样"被写空的会话"不会再以空白条目出现。
    # 一次批量读回所有分支文件，不额外增加网络往返。
    branch_names = [name for name in kept if branch_of.get(name)]
    branch_blobs = storage.read_json_many(
        [store.branch_path(name, branch_of[name]) for name in branch_names]
    )
    for name in branch_names:
        payload = branch_blobs.get(store.branch_path(name, branch_of[name]))
        messages = payload.get("message") if isinstance(payload, dict) else None
        if not messages:
            del kept[name]
    if not kept:
        return []
    # key 用字符串而非 datetime，省一次解析；会话 ID 是定长时间戳，字典序即时间序
    return sorted(kept, key=lambda n: (kept[n], n), reverse=True)


def list_empty_sessions() -> list:
    """列出"没有任何消息"的空壳会话（用来清理历史遗留）。

    这些通常是被早期 bug 写空的历史，或者是异常路径留下的垃圾。
    它们已经不在会话历史里显示了，但仍然占着存储。
    """
    empty = []
    for name in storage.session_ids():
        meta = storage.read_json(_meta_key(name), None)
        if not isinstance(meta, dict):
            empty.append(name)
            continue
        branch = meta.get("current_branch") or "main"
        payload = storage.read_json(store.branch_path(name, branch), None)
        messages = payload.get("message") if isinstance(payload, dict) else None
        if not messages:
            empty.append(name)
    return sorted(empty)


def _session_exists(session_name: str = None) -> bool:
    """会话是否有实物落盘（新结构目录 或 旧扁平文件）。"""
    name = session_name or st.session_state.get("current_session")
    if not name or Path(name).name != name:
        return False
    try:
        return storage.exists(_meta_key(name)) or storage.exists(_legacy_key(name))
    except Exception:  # noqa: BLE001
        return False


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
    """切换到某条会话：把人设、参数、头像、消息、分支指针全部换成它的。

    **本函数不调用 st.rerun()**（由 run_pending_actions 统一发起），原因和
    delete_session 一样：st.rerun() 靠抛异常中断脚本，放在主脚本开头调用的下层
    会把后面的渲染全部掐掉。

    更要紧的是**必须走两段式**（按钮回调只登记、主脚本体里执行）：
    本函数会改 current_session / message 等 8 处状态，而 Streamlit 会回滚回调里的
    session_state 变更 —— 直接当回调用的话，重跑后 current_session 会变回旧的，
    主区域于是继续显示上一个会话的内容（表现：点了历史会话，按钮没变化、
    或者变了但对话内容不对，接着发消息还会写进错误的会话）。
    """
    try:
        has_archive = _session_exists(session_name)
        draft = draft_path(session_name)
        has_draft = draft is not None and storage.exists(draft)
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
            restore = storage.read_json(draft, {}) or {}
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
        # 记下"这批消息属于这个会话的这条分支"
        mark_message_owner(session_name, st.session_state.current_branch)
        st.session_state.current_session = session_name
        # 顺手清掉**过期**的草稿（默认保留最近 1 天）。
        # 不能清"不是当前会话"的草稿 —— 草稿里存着头像/人设，用户切走再切回来时
        # 必须还在（有测试守着）。只清理明显没人会再回去的陈旧草稿，避免库里
        # 攒一堆 `.draft` 白占空间（用户发现的问题）。
        try:
            purge_stale_drafts(current=session_name)
        except Exception:  # noqa: BLE001 - 清理失败绝不能影响切换
            pass
    except Exception as e:  # noqa: BLE001 - 单个存档损坏不应中断整个页面
        st.error(f"加载会话失败: {e}")
        return


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
    """删除会话：整目录（含所有分支）与草稿一起清掉。

    注意：这是**真正执行删除**的函数。按钮不要直接调它，而是调
    ``request_delete_session`` —— 因为"删除数据"和"切换当前会话"必须分开做：
    Streamlit 会回滚控件回调里的 session_state 变更，如果在回调里把
    current_session 换成新 ID，重跑后它会变回被删除的那条，于是应用又把它
    当成一个空会话显示出来（表现：删除了但按钮不消失、点进去是空的）。

    **本函数不调用 st.rerun()**，这是刻意的：``st.rerun()`` 是靠**抛异常**中断
    脚本的。而本函数是在主脚本**一开头**（run_pending_actions）被调用的，
    一旦在这里 rerun，后面的渲染全部来不及执行 —— 表现就是"每次删除/切换会话后
    页面空白"。同样的做法还会被下面自己的 ``except Exception`` 抓住、串成多层
    rerun。交给调用方决定重跑时机（pending 路由里在函数返回后再 rerun）。
    """
    try:
        removed = storage.delete_prefix(store._session_dir(session_name))
        note_action(f"删除 {session_name}：存储层报告删除 {removed} 个键")
        flat = _legacy_key(session_name)  # 兼容还没迁移的旧扁平存档
        if storage.exists(flat):
            storage.delete(flat)
            note_action(f"删除 {session_name}：另删掉了旧扁平存档")
        discard_draft(session_name)
        if not removed:
            # 一个键都没删掉 = "要删的东西"和"存储里实际存在的"对不上。
            # 这曾经是静默发生的（日志写"已执行"，数据一条没少），必须显式提示。
            note_action(f"⚠ 删除 {session_name} 没有匹配到任何键")
            st.warning(f"没有找到这个会话的存档（{session_name}）：可能已经被删掉了。")
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


# --------------------------------------------------------------------------- #
# 待执行的动作：回调里只登记，真正执行放在主脚本体里
#
# 为什么不能直接在回调里做：Streamlit 会在回调结束后把 session_state 回滚到
# 点击前的快照（本项目里反复踩到）。所以"删除会话并切到新会话""新建会话"这类
# 需要同时改数据的动作，必须拆成两步：
#   回调 → 只写一个待办标记 → 重跑 → 主脚本体里执行（此时状态变更能留住）
# --------------------------------------------------------------------------- #
PENDING_DELETE = "_pending_delete"
PENDING_NEW = "_pending_new_session"
PENDING_LOAD = "_pending_load_session"
PENDING_BRANCH = "_pending_switch_branch"
PENDING_BRANCH_DELETE = "_pending_delete_branch"
PENDING_BRANCH_RENAME = "_pending_rename_branch"

# 最近发生的动作，用来在界面上排查"点了没反应"这类问题。
# 存在模块级（不是 session_state）里，所以不会受回调状态回滚影响。
RECENT_ACTIONS = []


def note_action(text: str) -> None:
    """记一条动作日志（只保留最近若干条）。"""
    import datetime

    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    RECENT_ACTIONS.append(f"{stamp} {text}")
    del RECENT_ACTIONS[:-12]


def request_delete_session(session_name: str) -> None:
    """删除按钮的回调：只登记"要删哪条"，真正的删除交给主脚本体。

    这里**不调用 st.rerun()**：Streamlit 在回调结束后本来就会自动重跑一次，
    多调一次没有好处，反而在 AppTest 等环境下不会被执行，导致待办永远不处理。
    """
    st.session_state[PENDING_DELETE] = session_name
    note_action(f"回调：登记删除 {session_name}")


def request_new_session() -> None:
    """「新建会话」按钮的回调：只登记，实际切换交给主脚本体。"""
    st.session_state[PENDING_NEW] = True
    note_action("回调：登记新建会话")


def request_load_session(session_name: str) -> None:
    """「切换到这条会话」按钮的回调：只登记，实际加载交给主脚本体。

    为什么不能直接调 load_selected_session：它会改 current_session / message 等
    8 处状态，而回调里的 session_state 变更会被 Streamlit 回滚 —— 重跑后
    current_session 变回旧值，主区域继续显示上一个会话（真踩过）。
    """
    st.session_state[PENDING_LOAD] = session_name
    note_action(f"回调：登记切换会话 {session_name}")


def request_switch_branch(session_name: str, branch_id: str) -> None:
    """「切换到这条分支」按钮的回调：只登记，实际切换交给主脚本体。

    同样是因为 branches.switch_branch 会改 st.session_state.current_branch，
    回调里改会被回滚 —— 表现是点了分支没反应、内容不对。
    连当前会话也要记下来：分支必须切到"当时那一条会话"的分支上。
    """
    st.session_state[PENDING_BRANCH] = (session_name, branch_id)
    note_action(f"回调：登记切换分支 {session_name} / {branch_id}")


def request_delete_branch(session_name: str, branch_id: str) -> None:
    """「删除分支」按钮的回调：只登记（delete_branch 会改 current_branch）。"""
    st.session_state[PENDING_BRANCH_DELETE] = (session_name, branch_id)
    note_action(f"回调：登记删除分支 {session_name} / {branch_id}")


def request_rename_branch(session_name: str, branch_id: str, input_key: str) -> None:
    """「重命名分支」输入框的 on_change 回调：只登记（改名会改 current_branch）。"""
    new_id = (st.session_state.get(input_key) or "").strip()
    if not new_id or new_id == branch_id:
        return
    st.session_state[PENDING_BRANCH_RENAME] = (session_name, branch_id, new_id)
    note_action(f"回调：登记重命名分支 {branch_id} -> {new_id}")


def run_pending_actions() -> None:
    """在主脚本体开头执行待办动作（此时状态变更不会被回滚）。

    执行完**一定要 rerun 一次**：此时 current_session 已经换了（或消息已重置），
    而本函数在渲染之前运行，必须重新跑一遍脚本才能按新状态画页面。

    重跑必须由**这里**显式发起，不要下沉进 delete_session / new_session /
    load_selected_session —— ``st.rerun()`` 是靠**抛异常**中断脚本的，放在被本函数
    调用的下层，就会在主脚本一开头把后面的渲染全部掐掉，表现是"每次操作后页面
    空白"（线上真踩过：面板只记到"发现待办"，永远没有"已执行"）。

    异常在这里吞掉并显示 st.error，而不是往上抛：本函数在渲染之前运行，
    抛出会让整页（包括排查面板）都不渲染，恰好把最有用的诊断信息藏掉。
    """
    target = st.session_state.get(PENDING_DELETE)
    pending_new = st.session_state.get(PENDING_NEW)
    load_target = st.session_state.get(PENDING_LOAD)
    branch_target = st.session_state.get(PENDING_BRANCH)
    branch_delete = st.session_state.get(PENDING_BRANCH_DELETE)
    branch_rename = st.session_state.get(PENDING_BRANCH_RENAME)
    if not (target or pending_new or load_target or branch_target
            or branch_delete or branch_rename):
        return
    note_action(
        f"主脚本：发现待办 delete={target!r} new={bool(pending_new)} "
        f"load={load_target!r} branch={branch_target!r} "
        f"rm_branch={branch_delete!r} rename={branch_rename!r}"
    )
    if target:
        st.session_state[PENDING_DELETE] = None
        try:
            delete_session(target)
        except Exception as exc:  # noqa: BLE001
            note_action(f"主脚本：删除 {target} 失败 {type(exc).__name__}: {exc}")
            st.error(f"删除会话失败：{type(exc).__name__}: {exc}")
        else:
            note_action(f"主脚本：删除 {target} 已执行")
    elif pending_new:
        st.session_state[PENDING_NEW] = None
        try:
            new_session()
        except Exception as exc:  # noqa: BLE001
            note_action(f"主脚本：新建会话失败 {type(exc).__name__}: {exc}")
            st.error(f"新建会话失败：{type(exc).__name__}: {exc}")
        else:
            note_action("主脚本：新建会话已执行")
    elif load_target:
        st.session_state[PENDING_LOAD] = None
        try:
            load_selected_session(load_target)
        except Exception as exc:  # noqa: BLE001
            note_action(f"主脚本：切换会话 {load_target} 失败 {type(exc).__name__}: {exc}")
            st.error(f"切换会话失败：{type(exc).__name__}: {exc}")
        else:
            note_action(f"主脚本：已切换到会话 {load_target}")
    elif branch_target:
        st.session_state[PENDING_BRANCH] = None
        branch_session, branch_id = branch_target
        try:
            # 局部导入：branches 依赖 sessions，模块顶层互相导入会成环
            from . import branches as branches_module

            branches_module.switch_branch(branch_session, branch_id)
            # switch_branch 只改指针不读内容，这里显式把消息换成分支内容，
            # 否则主区域还是上一条分支的对话（切分支"没反应"就是这么来的）
            if branch_session == st.session_state.get("current_session"):
                st.session_state.message = read_branch(branch_session, branch_id).get("message", [])
                mark_message_owner(branch_session, branch_id)
        except Exception as exc:  # noqa: BLE001
            note_action(f"主脚本：切换分支失败 {type(exc).__name__}: {exc}")
            st.error(f"切换分支失败：{type(exc).__name__}: {exc}")
        else:
            note_action(f"主脚本：已切换到分支 {branch_id}")
    elif branch_delete:
        st.session_state[PENDING_BRANCH_DELETE] = None
        branch_session, branch_id = branch_delete
        try:
            from . import branches as branches_module

            branches_module.delete_branch(branch_session, branch_id)
            if branch_session == st.session_state.get("current_session"):
                current = st.session_state.get("current_branch") or "main"
                st.session_state.message = read_branch(branch_session, current).get("message", [])
                mark_message_owner(branch_session, current)
        except Exception as exc:  # noqa: BLE001
            note_action(f"主脚本：删除分支失败 {type(exc).__name__}: {exc}")
            st.error(f"删除分支失败：{type(exc).__name__}: {exc}")
        else:
            note_action(f"主脚本：已删除分支 {branch_id}")
    elif branch_rename:
        st.session_state[PENDING_BRANCH_RENAME] = None
        branch_session, branch_id, new_id = branch_rename
        try:
            from . import branches as branches_module

            branches_module.rename_branch(branch_session, branch_id, new_id=new_id)
        except Exception as exc:  # noqa: BLE001
            note_action(f"主脚本：重命名分支失败 {type(exc).__name__}: {exc}")
            st.error(f"重命名分支失败：{type(exc).__name__}: {exc}")
        else:
            note_action(f"主脚本：分支已重命名为 {new_id}")
    # 用新状态重新画一遍页面（这里是发起重跑的唯一位置）
    st.rerun()
