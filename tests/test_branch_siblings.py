"""同一个分叉点重复重新生成 → 必须互为兄弟分支（‹ 2/3 › 箭头依赖它）。

用户报的现象：**分支似乎只能存在两条**。

定位：分支**创建**一直是好的（能建到 b6），坏的是**兄弟关系**。
`branch_siblings()` 按 (parent, fork_index) 判断兄弟，而以前所有分叉都用"当前分支"
当 parent，于是连续重新生成形成链：

    b2.parent = main
    b3.parent = b2      ← 每条的 parent 都不同
    b4.parent = b3

结果每条分支只跟自己一组 —— 界面上的 ‹ 2/3 › 永远 1/1、切不过去。

修法：给分支记录加上显式的 **fork_point**（从哪条分支分出来的），并让调用方
用 `branches.fork_point_for()` 取：

  · 重新生成 → reuse_chain=True：沿用它所属的那个分叉点（b2/b3/b4 都归 main）
  · 编辑     → reuse_chain=False：分叉点就是当前分支（嵌套分叉，不并进上一组）

用法：python tests/test_branch_siblings.py
"""
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _harness  # noqa: E402

PROJECT = _harness.PROJECT
sys.path.insert(0, str(PROJECT))
os.environ["AI_PARTNER_SESSIONS_DIR"] = str(_harness.SESSIONS_DIR)
os.environ.pop("DEEPSEEK_API_KEY", None)

import mongomock  # noqa: E402

from lingheng import branches, config, sessions, storage  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


C = mongomock.MongoClient()
storage._client = lambda: C
storage._mongo_lookup = lambda: ("mongodb://fake", "probe", "")
storage._mongo_ready.clear()
storage.reset_cache()
storage.set_current_uid("lingheng42")

MSG = [{"role": "user", "content": "问题"},
       {"role": "assistant", "content": "回答1"},
       {"role": "user", "content": "追问"},
       {"role": "assistant", "content": "回答2"}]


class _State(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as exc:
            raise AttributeError(k) from exc

    def __setattr__(self, k, v):
        self[k] = v

    def get(self, k, default=None):
        return dict.get(self, k, default)


def fresh(**kw):
    st = _State(kw)
    for key, value in config.DEFAULT_PROFILE.items():
        st.setdefault(key, value)
    for key, value in config.DEFAULT_ADVANCED.items():
        st.setdefault(key, value)
    for key in config.AVATAR_KEYS:
        st.setdefault(key, None)
    st.setdefault("message", [])
    return st


sessions.st.rerun = lambda: None


def new_session(sid):
    sessions.st.session_state = fresh(current_session=sid, current_branch="main")
    sessions.st.session_state["message"] = [dict(m) for m in MSG]
    sessions.save_session()


def fork(sid, index, chain=True):
    """复刻 ai.regenerate()/submit_edit() 的分叉动作（当前分支随之切换）。"""
    current = sessions.st.session_state.get("current_branch") or "main"
    point = branches.fork_point_for(sid, current, index, reuse_chain=chain)
    bid = branches.fork_branch(sid, [dict(m) for m in MSG[:index]],
                               seed=None, parent=point, fork_index=index, fork_point=point)
    sessions.st.session_state["current_branch"] = bid
    return bid


def meta_of(sid):
    return {b["id"]: (b["parent"], b["fork_index"], b["fork_point"])
            for b in sessions.normalize_branches(sessions.load_session_meta(sid)["branches"])}


# --------------------------------------------------------------------------- #
# 1. 重新生成：同一分叉点连续分叉 → 互为兄弟
# --------------------------------------------------------------------------- #
SID = "2090-01-01_000000_000"
new_session(SID)
ids = [fork(SID, 1) for _ in range(4)]
check("连续重新生成创建了 4 条新分支", ids == ["b2", "b3", "b4", "b5"], ids)

sibs = branches.branch_siblings(SID, "b2")
check("**同一个分叉点的分支互为兄弟**（箭头靠它工作）",
      sibs == ["main", "b2", "b3", "b4", "b5"], sibs)
# 源分支（main）必须在组里：否则从 b2 切回 main 之后，main 那边算出来只有自己，
# 箭头因为"只有 1 条"不渲染，用户就再也切不回改写版本了 —— 这正是用户报的
# 「修改留言内容根本不能创建分支」。见 group_members 的文档。
for bid in ids:
    check(f"{bid} 的兄弟集合一致", branches.branch_siblings(SID, bid) == sibs,
          branches.branch_siblings(SID, bid))
check("站在源分支 main 上看到的也是同一组（能切回去）",
      branches.branch_siblings(SID, "main") == sibs,
      branches.branch_siblings(SID, "main"))

meta = meta_of(SID)
check("**所有兄弟共用同一个分叉点**（不再是 b2←b3←b4 链条）",
      len({meta[b][2] for b in ids}) == 1 and meta["b2"][2] == "main",
      {b: meta[b] for b in ids})
check("fork_index 一致",
      len({meta[b][1] for b in ids}) == 1, {b: meta[b] for b in ids})

# 不同分叉点 → 单独的兄弟组，不能混进来
other = fork(SID, 3)
check("另一个分叉点创建了新分支", other == "b6", other)
check("不同分叉点的分支**不**算兄弟",
      branches.branch_siblings(SID, other) == ["main", "b6"],
      branches.branch_siblings(SID, other))
check("原兄弟组不受影响（b6 那个位置的不许混进来）",
      branches.branch_siblings(SID, "b2") == ["main", "b2", "b3", "b4", "b5"],
      branches.branch_siblings(SID, "b2"))
# 在第一条兄弟上再重新生成 → 仍进同一组
sessions.st.session_state["current_branch"] = "b2"
again = fork(SID, 1)
check("从兄弟分支再重新生成仍归到同一组",
      again in branches.branch_siblings(SID, "b2"),
      (again, branches.branch_siblings(SID, "b2")))

# --------------------------------------------------------------------------- #
# 2. 编辑：嵌套分叉（分叉点 = 当前分支，不并进上一组）
# --------------------------------------------------------------------------- #
SID2 = "2091-01-01_000000_000"
new_session(SID2)
e2 = fork(SID2, 0, chain=False)          # 在 main 上编辑第 0 条
e3 = fork(SID2, 0, chain=False)          # 在 e2 上再编辑第 0 条
e4 = fork(SID2, 0, chain=False)
check("编辑逐层嵌套创建了 3 层", [e2, e3, e4] == ["b2", "b3", "b4"], [e2, e3, e4])

meta2 = meta_of(SID2)
check("**编辑是嵌套分叉**：b3 的父/分叉点都是 b2",
      meta2["b3"][0] == "b2" and meta2["b3"][2] == "b2", meta2["b3"])
check("b4 的父/分叉点都是 b3",
      meta2["b4"][0] == "b3" and meta2["b4"][2] == "b3", meta2["b4"])
check("**嵌套层不会被误判成兄弟**（fork_index 相同也不并组）",
      branches.branch_siblings(SID2, "b3") == ["b2", "b3"],
      branches.branch_siblings(SID2, "b3"))
check("b4 的组也只跟它的分叉点 b3 在一起",
      branches.branch_siblings(SID2, "b4") == ["b3", "b4"],
      branches.branch_siblings(SID2, "b4"))
check("第一层的分支只跟 main 成组（不把嵌套层拉进来）",
      branches.branch_siblings(SID2, "b2") == ["main", "b2"],
      branches.branch_siblings(SID2, "b2"))
check("嵌套最深处不会跟 main 混在一起",
      "main" not in branches.branch_siblings(SID2, "b3"),
      branches.branch_siblings(SID2, "b3"))

# --------------------------------------------------------------------------- #
# 3. 老存档兼容：没有 fork_point 字段时按 parent 兜底推断
# --------------------------------------------------------------------------- #
SID3 = "2092-01-01_000000_000"
new_session(SID3)
bare = branches.fork_branch(SID3, [dict(m) for m in MSG[:1]],
                            seed=None, parent="main", fork_index=1)
# 手工抹掉 fork_point，模拟旧版本写下的记录
raw = sessions.load_session_meta(SID3)
raw["branches"] = [{"id": b["id"], "parent": b["parent"], "fork_index": b["fork_index"],
                    "created_at": b.get("created_at")} for b in
                   sessions.normalize_branches(raw["branches"])]
sessions.write_session_meta(SID3, raw)
sibs_old = branches.branch_siblings(SID3, bare)
check("老存档（无 fork_point）仍能算出兄弟（兜底到 parent + 源分支）",
      sibs_old == ["main", bare], sibs_old)

# 老存档上继续分叉：第二个分支的分叉点应推断为 main（链式替代）
sessions.st.session_state["current_branch"] = bare
inferred = branches.fork_point_for(SID3, bare, 1)
check("老存档上再分叉 → 分叉点推断为 main", inferred == "main", inferred)
third = branches.fork_branch(SID3, [dict(m) for m in MSG[:1]],
                             seed=None, parent=inferred, fork_index=1, fork_point=inferred)
check("老存档上分出的第二个分支与第一个互为兄弟",
      # 不能再用 sorted()：现在组里含源分支 main，字母序会把 main 排到最后，
      # 而实际顺序是**创建顺序**（main 最先）。
      branches.branch_siblings(SID3, bare) == ["main", bare, third],
      branches.branch_siblings(SID3, bare))

# --------------------------------------------------------------------------- #
# 4. 兜底：状态不一致时不能崩、不能返回空
# --------------------------------------------------------------------------- #
check("当前分支不存在时退回自身（不崩）",
      branches.fork_point_for(SID3, "不存在的分支", 1) == "不存在的分支",
      branches.fork_point_for(SID3, "不存在的分支", 1))
check("空会话也不崩", isinstance(branches.fork_point_for("空的", "main", 0), str))
check("未知分支的兄弟集合是它自己",
      branches.branch_siblings(SID3, "不存在的分支") == ["不存在的分支"])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
