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
        # 老数据里 parent 也可能是"同一位置分叉出来的另一条分支"（链式创建），
        # 那它就不是真正的分叉点，继续往上走。
        #
        # 判据刻意保守：只有当 parent **自己也是分叉产物**（它的 fork_index 非空，
        # 说明它也是从别处按索引分出来的）且索引与当前一致时才并上去。
        # 不能用"parent 恰好和自己 fork_index 相同"——嵌套分叉那条链上
        # （b3.parent=b2、两者 fork_index 都是 0）会误判，把嵌套层并成兄弟。
        fold = (parent.get("fork_index") is not None
                and parent.get("fork_index") == current.get("fork_index"))
        if fold:
            seen.add(str(parent["id"]))
            current = parent
            continue
        return str(parent_id)


def group_members(session_name: str, branch_id: str) -> list:
    """同一次分叉产生的"兄弟组"（能互相切换的那些版本，按创建顺序）。

    分组依据是**分叉点 + 分叉位置**（见 _fork_point_id），不是 parent ——
    连续重新生成时分支是链式创建的（b2.parent=main、b3.parent=b2、b4.parent=b3），
    按 parent 分组会把它们拆散，箭头只显示 1/1、切不过去。

    **分叉点本身（源分支）也算这个组的成员**，这是"编辑后看起来没建分支"的关键：
    在 main 的第 1 条编辑产生 b2，b2 的分叉点是 main，但 main 不是分叉产生的
    （自己 fork_index 为空），以前按 ``fork_index`` 相等筛选时它进不了组 ——
    于是从 b2 切回 main 之后，main 那边算出来只有它自己，箭头因为"只有 1 条"
    整个不渲染，用户就再也切不回改写版本了，看起来像"编辑没创建分支"。

    反向也要成立：站在源分支（main）上时，取"从它分出去的最早那个位置"上的
    子分支一起成组。两边的结果必须完全一致，否则会出现"切过去就切不回来"。

    嵌套分叉不会被误并：在 b2 上编辑产生的 b3，分叉点是 b2 而不是 main，
    它的组是 {b2, b3}，与 {main, ...} 无关（见 fork_point_for 里两种语义的区分）。
    """
    items = sessions.normalize_branches(
        sessions.load_session_meta(session_name).get("branches"))
    me = next((b for b in items if str(b["id"]) == str(branch_id)), None)
    if me is None:
        return [branch_id]
    my_id = str(me["id"])
    point = _fork_point_id(items, me)
    if str(point) != my_id:
        # 我是分叉产物：分叉点（源分支）+ 同一分叉点、同一位置上的所有并列版本。
        anchor, anchor_index = str(point), me.get("fork_index")
    else:
        # 我自己就是源分支（不是分叉产生的，没有 fork_index）：那就取"从我分出去的
        # 最早那个位置"上的所有子分支，跟它们互相能切。
        kids = [b for b in items
                if _fork_point_id(items, b) == my_id and b.get("fork_index") is not None]
        anchor = my_id
        anchor_index = min((b["fork_index"] for b in kids), default=None)
    same = [str(b["id"]) for b in items
            if str(b["id"]) == anchor
            or (_fork_point_id(items, b) == anchor
                and b.get("fork_index") == anchor_index)]
    return same or [branch_id]


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
    """同一次分叉产生的兄弟分支（同一个分叉点、同一个位置分出来的那些），按创建顺序。

    分组依据是**分叉点 + 分叉位置**而不是 parent —— 连续重新生成时分支是链式创建的
    （b2.parent=main、b3.parent=b2、b4.parent=b3），按 parent 分组会把它们拆散，
    箭头就只显示 1/1、切不过去（用户报的"分支似乎只能存在两个"）。

    组里**包含分叉点本身**（源分支），所以在 main 的第 0 条编辑出来的 b2 与 main
    互为兄弟：‹ 1/2 › 能在原文和新版本之间来回切。具体见 group_members。
    """
    return group_members(session_name, branch_id)


def group_fork_index(session_name: str, branch_id: str) -> int | None:
    """这个兄弟组在**第几条消息**处分叉（箭头画在那一条下面）。

    必须是"定义这一组的那个位置"，也就是 ``group_members`` 里用的 anchor_index，
    规则分两种（与分组规则严格一致）：

    - 我是分叉产物 → 就是我自己的 ``fork_index``
      （连着重新生成时 b2/b3/b4 的 fork_index 都相同，谁都是同一个值）
    - 我是源分支（main）→ 取"从我分出去的最早那个位置"
      （main 自己不是分叉产生的、没有 fork_index，箭头得画在子分支分出来的地方）

    ⚠️ 以前写成"组内 fork_index 的最小值"，嵌套分叉时会画错位置：
    在 b2 的第 3 条编辑出 b3 时，组是 {b2, b3}，而 b2 自己的 fork_index 是它当年
    从 main 分出来的位置（比如 1）—— min 取到 1，箭头就跑到第 1 条下面去了，
    而那一条正是 b2/b3 **共有**的前缀（它们从第 3 条才开始不同）。
    """
    items = sessions.normalize_branches(
        sessions.load_session_meta(session_name).get("branches"))
    me = next((b for b in items if str(b["id"]) == str(branch_id)), None)
    if me is None:
        return None
    my_id = str(me["id"])
    if str(_fork_point_id(items, me)) != my_id:
        index = me.get("fork_index")
        return int(index) if index is not None else None
    kids = [b for b in items
            if _fork_point_id(items, b) == my_id and b.get("fork_index") is not None]
    return min((int(b["fork_index"]) for b in kids), default=None)


def describe(session_name: str = None, branch_id: str = None) -> str:
    """把当前会话的分支状态摊平成一屏文本（排查"编辑后没有分支"用）。

    用户看到的只是"编辑了却没有分支/没有箭头"，但至少有四种完全不同的原因，在
    界面上长得一模一样：

    1. 分支根本没写进 meta（回调没执行 / 写入失败）
    2. ``fork_index`` 丢了 → 兄弟组算出来了，但不知道画在第几条，箭头整个不渲染
    3. 兄弟组算出来只有它自己（分叉点对不上）→ ``len(siblings) <= 1`` 直接不画
    4. 箭头画在了别的序号上（用户看着那一条，以为没有）

    猜是猜不出来的（这三种现象一模一样），所以把这些中间值直接摊出来：用户复制
    这一屏，原因当场就定了。
    """
    from ._deploy_stamp import BUILD, VERSION

    lines = [f"构建 {VERSION} · {BUILD}"]
    try:
        sid = session_name or st.session_state.get("current_session")
    except Exception:  # noqa: BLE001 - 极端情况下没有 session_state
        sid = session_name
    lines.append(f"会话：{sid}")
    if not sid:
        return "\n".join(lines + ["（还没有会话）"])
    try:
        meta = sessions.load_session_meta(sid)
        items = sessions.normalize_branches(meta.get("branches"))
        disk_branch = meta.get("current_branch") or "main"
    except Exception as exc:  # noqa: BLE001
        return "\n".join(lines + [f"读 meta 失败：{type(exc).__name__}: {exc}"])
    try:
        mem_branch = st.session_state.get("current_branch")
    except Exception:  # noqa: BLE001
        mem_branch = None
    lines.append(f"磁盘当前分支：{disk_branch}／内存当前分支：{mem_branch}")
    now = str(branch_id or mem_branch or disk_branch)
    for b in items:
        bid = str(b["id"])
        try:
            count = len(sessions.read_branch(sid, bid).get("message", []))
        except Exception as exc:  # noqa: BLE001 - 读不出来就照实说
            count = f"读不出来（{type(exc).__name__}）"
        lines.append(
            f"  {bid}{' ←当前' if bid == now else ''}："
            f"parent={b.get('parent')} fork_index={b.get('fork_index')} "
            f"fork_point={b.get('fork_point')}（算作 {_fork_point_id(items, b)}）"
            f" 消息 {count} 条")
    try:
        group = group_members(sid, now)
        index = group_fork_index(sid, now)
    except Exception as exc:  # noqa: BLE001
        return "\n".join(lines + [f"算兄弟组失败：{type(exc).__name__}: {exc}"])
    lines.append(f"兄弟组（{len(group)}）：{group}")
    if len(group) <= 1:
        lines.append("→ 只有一条，按规则不画箭头（这就是「没有分支」的直接原因）")
    else:
        lines.append(f"→ 箭头画在第 {index} 条消息下面"
                     + ("（**位置算不出来**，箭头不会渲染）" if index is None else ""))
    return "\n".join(lines)


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
