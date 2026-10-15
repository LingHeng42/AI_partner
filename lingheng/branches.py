"""分支：新建、切换、改名、删除，以及兄弟分支与下一个分支 ID。

一条分支 = 一条线性消息列表；"分叉"不是真树，而是两条分支共享分叉点之前的前缀。
"""

import os

import streamlit as st

from . import config, sessions, storage, store



def _fork_point_id(items: list, branch: dict) -> str:
    """一条分支的**分叉点**：它是从哪条分支上分出来的。

    优先用记录里的 ``fork_point`` 字段（创建时写入，无歧义）。老存档没有这个字段，
    才按 parent 往上推断 —— 因为连续重新生成时分支是链式创建的：

        b2.parent = main, fork_point = main    （在 main 的第 1 条处重新生成）
        b3.parent = b2,   fork_point = main    （又从 b2 的第 1 条处重新生成）

    b3 的 parent 是 b2（链），但真正分叉点是 main。只看 parent 会把 b2/b3 拆散。
    """
    explicit = branch.get("fork_point")
    if explicit:
        return str(explicit)
    # ---- 老存档兜底：按 parent 往上推断 ----
    seen = {str(branch["id"])}
    current = branch
    while True:
        parent_id = current.get("parent")
        if parent_id is None or str(parent_id) in seen:
            return str(current["id"])
        parent = next((b for b in items if str(b["id"]) == str(parent_id)), None)
        if parent is None:
            return str(parent_id)
        # parent 也是"在同一位置分叉"的分支 → 它只是同组的新成员，不是分叉点
        if parent.get("fork_index") == current.get("fork_index") is not None:
            seen.add(str(parent["id"]))
            current = parent
            continue
        return str(parent_id)


def group_members(session_name: str, branch_id: str) -> list:
    """同一条分支所在的"兄弟组"（同一个分叉点分出来的所有分支，按创建顺序）。

    分组依据是**分叉点**而不是 parent —— 原因见 _fork_point_id。
    """
    items = sessions.normalize_branches(
        sessions.load_session_meta(session_name).get("branches"))
    me = next((b for b in items if str(b["id"]) == str(branch_id)), None)
    if me is None:
        return [branch_id]
    point = _fork_point_id(items, me)
    same = [str(b["id"]) for b in items
            if b.get("fork_index") == me.get("fork_index")
            and _fork_point_id(items, b) == point]
    return same or [str(branch_id)]


def fork_point_for(session_name: str, current_branch: str, fork_index: int,
                   reuse_chain: bool = True) -> str:
    """在 ``fork_index`` 处分叉时，新分支的**分叉点**该是谁。

    这是修复"分支似乎只能存在两个"的关键：分叉点必须显式沿用，不能靠 parent 推
    （连续重新生成时 b3.parent=b2，但真正分叉点是 main）。

    两种操作的分叉点含义不同，必须分开：

    **重新生成（reuse_chain=True，"这次回答换个样子"）**
        链式替代 —— 用户看着同一条回答反复点重新生成，这些结果应当并列成一排：

            在 main 上重新生成 → 分叉点 = main
            在 b2 上再重新生成 → b2 的分叉点就是 main → 分叉点 = main（b2/b3 并列）

    **编辑（reuse_chain=False，"从这条分支出发改一条"）**
        嵌套分叉 —— 用户在某条分支上改内容，新分支应当挂在**那条分支**下面：

            在 b3 上编辑它的第 0 条 → 分叉点 = b3（不会因为"索引也是 0"
                                      就并进 main 那一组）

    这条区分是刻意的：编辑的语义是"以这条分支为起点分叉"，而重新生成的语义是
    "这个位置的另一种可能"。
    """
    if not reuse_chain:
        # 编辑：当前分支就是分叉点
        return current_branch or "main"
    items = sessions.normalize_branches(
        sessions.load_session_meta(session_name).get("branches"))
    me = next((b for b in items if str(b["id"]) == str(current_branch)), None)
    if me is None:
        return current_branch or "main"
    point = _fork_point_id(items, me)
    if point != str(me["id"]):
        # 当前分支本身就是某个分叉点的产物 → 归到同一组（链式替代）
        return point
    return current_branch or "main"


def sibling_parent(session_name: str, current_branch: str, fork_index: int) -> str:
    """在 ``fork_index`` 处分叉时，新分支的 parent 该填谁（= 分叉点）。

    parent 与分叉点在这里取同一个值：新分支的"父"就是它分出来的那条分支。
    """
    return fork_point_for(session_name, current_branch, fork_index)


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
    """同一次分叉产生的兄弟分支（同一个分叉点分出来的那些），按创建顺序。

    分组依据是**分叉点**而不是 parent —— 连续重新生成时分支是链式创建的
    （b2.parent=main、b3.parent=b2、b4.parent=b3），按 parent 分组会把它们拆散，
    箭头就只显示 1/1、切不过去（用户报的"分支似乎只能存在两个"）。
    """
    return group_members(session_name, branch_id)


def fork_branch(session_name: str, prefix: list, seed=None, parent=None, fork_index=None,
                fork_point=None) -> str:
    """新建一条分支并把 prefix(+seed) 写进去，返回新分支 ID。

    prefix 是新分支继承的消息前缀；seed 是分叉点那条被改写/重新生成的消息
    （编辑消息时传入，重新生成时传 None）。

    fork_point 是"从哪条分支分出来的"。不传时退回 parent（老行为）——
    调用方应当传 branches.fork_point_for(...) 的结果，否则连续重新生成会形成
    链式 parent，兄弟关系就散了（箭头显示 1/1）。
    """
    sessions.migrate_session(session_name)
    branch_id = next_branch_id(session_name)
    meta = sessions.load_session_meta(session_name)
    meta["branches"] = sessions.normalize_branches(
        [*sessions.normalize_branches(meta.get("branches")),
         sessions._branch(branch_id, parent, fork_index, fork_point)]
    )
    meta["current_branch"] = branch_id
    messages = [dict(m) for m in prefix]
    if seed is not None:
        messages.append(dict(seed))
    sessions.write_branch(session_name, branch_id, {"message": messages})
    sessions.write_session_meta(session_name, meta)
    return branch_id


def switch_branch(session_name: str, branch_id: str) -> None:
    """切换当前分支：只改 meta 里的指针，**绝不写分支内容**。

    这里以前会"读出来再原样写回去"，目的是给缺失的分支补一个空文件。但那等于
    把读到的内容当成了权威值 —— 一旦读取失败（网络抖动 / 超时），就会把空内容
    写回磁盘，永久覆盖掉这条分支的历史。现在只在文件**确实不存在**时才补空文件，
    已存在的一律不碰。

    顺序也很重要：先确保目标分支可用，最后才改 meta 指针，避免"指针指向不存在
    的分支"。这是按钮回调，Streamlit 在回调之后本来就会重跑一次，所以这里不调用
    st.rerun()（多调一次会让 st.rerun 之后的代码被整体跳过）。
    """
    if branch_id not in sessions.branch_ids(session_name):
        st.error(f"分支不存在：{branch_id}")
        return
    key = store.branch_path(session_name, branch_id)
    if not storage.exists(key):
        # 该分支在 meta 里登记过但文件还没落盘：补一个空文件，内容由后续保存填充
        sessions.write_branch(session_name, branch_id, {"message": []})
    elif storage.read_json(key, None) is None:
        # 文件在、但内容读不出来：宁可报错也不要猜（猜错就是丢数据）
        st.error(f"分支内容读不出来，已取消切换：{branch_id}")
        return
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
            # 统一走存储层"复制 + 删旧的"：两个后端同一条路径，而且会让存储层的
            # 读缓存正确失效。曾经为了省一次拷贝对本地后端特判 os.replace() 直接改
            # 磁盘——那会绕过缓存，导致紧接着的 storage.exists(new_path) 拿到改名前的
            # False，"删除分支"就被静默跳过了。
            payload = storage.read_json(old_path, None)
            if payload is None:
                st.error(f"分支文件读不出来，改名已取消：{new_id}.json")
                return
            storage.write_json(new_path, payload)
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
            sessions.mark_message_owner(session_name, new_id)
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
