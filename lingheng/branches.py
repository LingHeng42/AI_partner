"""分支：新建、切换、改名、删除，以及兄弟分支与下一个分支 ID。

一条分支 = 一条线性消息列表；"分叉"不是真树，而是两条分支共享分叉点之前的前缀。
"""

import os

import streamlit as st

from . import config, sessions, storage, store



def next_branch_id(session_name: str) -> str:
    """给新分支取新 ID：b2、b3……，只增不减（不复用被删掉的编号）。

    不复用是为了让"分支 ID 的大小顺序"和"创建先后顺序"一致：
    否则删掉中间一条后，新分支会顶替那个编号，列表里看起来像是乱序。
    """
    highest = 1  # main 视作第 1 条
    for branch in sessions.branch_ids(session_name):
        suffix = branch[1:]
        if branch.startswith("b") and suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"b{highest + 1}"


def branch_siblings(session_name: str, branch_id: str) -> list:
    """同一次分叉产生的兄弟分支（parent 与 fork_index 都相同的那些）。"""
    items = sessions.normalize_branches(sessions.load_session_meta(session_name).get("branches"))
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
    sessions.migrate_session(session_name)
    branch_id = next_branch_id(session_name)
    meta = sessions.load_session_meta(session_name)
    meta["branches"] = sessions.normalize_branches(
        [*sessions.normalize_branches(meta.get("branches")), sessions._branch(branch_id, parent, fork_index)]
    )
    meta["current_branch"] = branch_id
    messages = [dict(m) for m in prefix]
    if seed is not None:
        messages.append(dict(seed))
    sessions.write_branch(session_name, branch_id, {"message": messages})
    sessions.write_session_meta(session_name, meta)
    return branch_id


def switch_branch(session_name: str, branch_id: str) -> None:
    """切换当前分支：先确保目标分支文件存在，最后才改 meta 里的指针。

    顺序很重要：反过来可能出现"指针指向一条还不存在的分支"。
    这是按钮回调，Streamlit 在回调之后本来就会重跑一次，所以这里不调用
    st.rerun()：多调一次会让 st.rerun 之后的代码被整体跳过，而且快速重跑
    在部分环境下（如 AppTest）不会被执行，反而看不清真实行为。
    """
    if branch_id not in sessions.branch_ids(session_name):
        st.error(f"分支不存在：{branch_id}")
        return
    data = sessions.read_branch(session_name, branch_id)
    sessions.write_branch(session_name, branch_id, data)  # 不存在时补一个空分支文件
    meta = sessions.load_session_meta(session_name)
    meta["current_branch"] = branch_id
    sessions.write_session_meta(session_name, meta)
    st.session_state.current_branch = branch_id


def rename_branch(session_name: str, branch_id: str, new_id: str = None, input_key: str = None) -> None:
    """分支改名：改名等于改 ID，因此分支文件要一起重命名（走回调）。"""
    if new_id is None:
        new_id = st.session_state.get(input_key, "")
    new_id = (new_id or "").strip()
    if not new_id or new_id == branch_id:
        return
    try:
        store.branch_path(session_name, new_id)  # 借它做非法名校验
    except ValueError as e:
        st.error(str(e))
        return
    meta = sessions.load_session_meta(session_name)
    items = sessions.normalize_branches(meta.get("branches"))
    if any(b["id"] == new_id for b in items):
        st.error(f"分支名已存在：{new_id}")
        return
    try:
        old_path, new_path = store.branch_path(session_name, branch_id), store.branch_path(session_name, new_id)
        if storage.exists(old_path):
            if storage.exists(new_path):
                st.error(f"分支文件已存在：{new_id}.json")
                return
            # 本地后端可以直接改名；远程后端（MongoDB）只能复制一份再删旧的
            if storage.backend_kind() == "local":
                os.replace(storage.active().path(old_path), storage.active().path(new_path))
            else:
                storage.write_json(new_path, storage.read_json(old_path, {}) or {})
                storage.delete(old_path)
        for item in items:
            if item["id"] == branch_id:
                item["id"] = new_id
        meta["branches"] = items
        if meta.get("current_branch") == branch_id:
            meta["current_branch"] = new_id
        sessions.write_session_meta(session_name, meta)
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
        meta = sessions.load_session_meta(session_name)
        items = sessions.normalize_branches(meta.get("branches"))
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
        sessions.write_session_meta(session_name, meta)
        path = store.branch_path(session_name, branch_id)
        if storage.exists(path):
            storage.delete(path)
        if session_name == st.session_state.get("current_session"):
            previous = st.session_state.get("current_branch")
            st.session_state.current_branch = current
            if current != previous:
                st.session_state.message = sessions.read_branch(session_name, current).get("message", [])
    except Exception as e:  # noqa: BLE001
        st.error(f"删除分支失败: {e}")
