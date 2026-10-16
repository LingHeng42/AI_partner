"""回调里"当前用户"丢了 → 数据写进 users/local，用户永远看不到。

用户报的现象：**点击修改留言、保存并重新生成，界面上还是只有 main、也没有箭头**。
而「排查：最近动作」里明明白白写着：

    11:06:32 回调：编辑第 1 条 → 新建 b2（分叉点 main／第 1 条，assistant）

即：回调跑了、fork_branch 也返回了新分支 ID，可下一次渲染读出来只有 main。

根因：`storage._state` 是 `threading.local()`，用户名由 `set_current_uid()` 在
`ui.main()` 里设置。而**控件回调跑在脚本主体之前** —— 如果这个回调运行在一个还没
跑过本会话脚本的线程上，`_state.uid` 就是空的，`current_uid()` 于是退回默认值
`"local"`，整套存储路径变成 `users/local/...`：

  · 写：往 users/local 里建了一条 b2（日志当然说成功）
  · 读：下一次渲染在自己的分区里读，只有 main
  · 表现：点了编辑/重新生成，"一点变化都没有"

修法：`current_uid()` 在 `_state.uid` 为空时，从 `st.session_state["_uid"]`
（每次渲染都会记下来）兜底；再加一道"新建分支后回读校验"，把这类静默失败
变成界面上的明确报错。

用法：python tests/test_uid_partition.py
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


ERRORS = []


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


# 真 streamlit 模块的属性直接替换：sessions.st 与 branches.st 是同一个模块对象，
# 所以两个模块都拿到同一份假 session_state（既有套件也是这么做的）。
sessions.st.rerun = lambda: None
sessions.st.error = lambda text, *a, **k: ERRORS.append(str(text))
sessions.st.warning = lambda text, *a, **k: None

MSG = [{"role": "user", "content": "问题"},
       {"role": "assistant", "content": "回答1"}]


def forget_uid():
    """模拟"回调跑在一个还没跑过本会话脚本的线程上"：threading.local 里没有 uid。"""
    if hasattr(storage._state, "uid"):
        del storage._state.uid
    storage.reset_cache()


# --------------------------------------------------------------------------- #
# 1. current_uid() 的三级取值
# --------------------------------------------------------------------------- #
sessions.st.session_state = fresh(_uid="alice")
storage.set_current_uid("bob")
check("set_current_uid 设过的值优先（脚本主体的正常路径）",
      storage.current_uid() == "bob", storage.current_uid())

forget_uid()
check("**线程里没有 uid 时从 session_state 兜底**（回调路径）",
      storage.current_uid() == "alice", storage.current_uid())

sessions.st.session_state = fresh()          # 没有 _uid
forget_uid()
check("两处都没有才退回 local", storage.current_uid() == "local", storage.current_uid())

sessions.st.session_state = fresh(_uid="   ")
forget_uid()
check("session_state 里是空白也算没有", storage.current_uid() == "local",
      storage.current_uid())

# --------------------------------------------------------------------------- #
# 2. 真正要防的后果：回调里的写入必须落在**自己的分区**里
# --------------------------------------------------------------------------- #
SID = "2094-01-01_000000_000"
sessions.st.session_state = fresh(_uid="alice", current_session=SID,
                                  current_branch="main", message=[dict(m) for m in MSG])
storage.set_current_uid("alice")
sessions.save_session()

# 回调场景：uid 在线程里丢了，但 session_state 里还记着 alice
forget_uid()
new_id = branches.fork_branch(SID, [dict(m) for m in MSG[:1]], seed=None,
                             parent="main", fork_index=1, fork_point="main")
check("回调里新建分支成功", new_id == "b2", new_id)

keys = [doc["key"] for doc in C["lingheng"]["files"].find({"uid": "alice"})]
check("**新建的分支写进了 alice 的分区**（不是 users/local）",
      any(k.endswith(f"sessions/{SID}/branches/b2.json") for k in keys),
      sorted(keys))
local_keys = [doc["key"] for doc in C["lingheng"]["files"].find({"uid": "local"})]
check("users/local 分区里没有任何东西", not local_keys, local_keys)

# 回读校验必须通过（正常写入不该报错）
check("正常写入时回读校验不报错", not ERRORS, ERRORS)
check("回读能看到新分支", "b2" in sessions.branch_ids(SID), sessions.branch_ids(SID))

# --------------------------------------------------------------------------- #
# 3. 回读校验：写入真的丢了必须当场喊出来（不许静默）
# --------------------------------------------------------------------------- #
SID2 = "2094-01-01_000000_001"
sessions.st.session_state = fresh(_uid="alice", current_session=SID2,
                                  current_branch="main", message=[dict(m) for m in MSG])
storage.set_current_uid("alice")
sessions.save_session()
ERRORS.clear()
sessions.RECENT_ACTIONS.clear()

real_write_meta = sessions.write_session_meta
sessions.write_session_meta = lambda name, meta: None      # 模拟"写没生效"
try:
    lost = branches.fork_branch(SID2, [dict(m) for m in MSG[:1]], seed=None,
                               parent="main", fork_index=1, fork_point="main")
finally:
    sessions.write_session_meta = real_write_meta

check("写不进去时仍然返回了分支 ID（调用方不用改）", lost == "b2", lost)
check("**回读校验失败时报错**（以前是完全静默的）", bool(ERRORS), ERRORS)
check("错误信息里带上当前分区，方便排查", ERRORS and "alice" in ERRORS[-1], ERRORS[-1:])
check("动作日志里记下「回读不到」",
      any("回读不到" in line for line in sessions.RECENT_ACTIONS),
      sessions.RECENT_ACTIONS[-3:])

# --------------------------------------------------------------------------- #
# 3b. 更阴的一路：线程里残留的是**另一个**用户名（线程池复用线程）
# --------------------------------------------------------------------------- #
SID3 = "2094-01-01_000000_002"
sessions.st.session_state = fresh(_uid="alice", current_session=SID3,
                                  current_branch="main", message=[dict(m) for m in MSG])
storage.set_current_uid("alice")
sessions.save_session()

storage.set_current_uid("someone_else")   # 复用的线程里残留着别人的用户名
sessions.st.session_state = fresh(_uid="alice", current_session=SID3,
                                  current_branch="main", message=[dict(m) for m in MSG])
ERRORS.clear()
sessions.RECENT_ACTIONS.clear()
branches.fork_branch(SID3, [dict(m) for m in MSG[:1]], seed=None,
                     parent="main", fork_index=1, fork_point="main")
check("**线程里残留别人的用户名时也会报错**（回读能过，只能靠身份比对抓）",
      any("写错了分区" in e and "someone_else" in e and "alice" in e for e in ERRORS),
      ERRORS)
check("身份不一致时动作日志点名两边的分区",
      any("someone_else" in line and "alice" in line for line in sessions.RECENT_ACTIONS),
      sessions.RECENT_ACTIONS[-2:])
storage.set_current_uid("alice")

# --------------------------------------------------------------------------- #
# 5. 真正修用户那个 bug：回调里身份丢了/串了，也要写进自己的分区
# --------------------------------------------------------------------------- #
from lingheng import ai  # noqa: E402


def _callback_session(sid, uid="alice"):
    sessions.st.session_state = fresh(_uid=uid, current_session=sid,
                                      current_branch="main",
                                      message=[dict(m) for m in MSG])
    storage.set_current_uid(uid)
    sessions.save_session()
    return sessions.load_session_meta(sid)


def _branch_ids(sid, uid="alice"):
    storage.set_current_uid(uid)
    return [str(b["id"]) for b in sessions.load_session_meta(sid)["branches"]]


SID4 = "2094-01-01_000000_003"
_callback_session(SID4)
forget_uid()                                  # 回调线程：线程局部量里没有用户名
sessions.RECENT_ACTIONS.clear()
ai.regenerate(1)                              # 用户在界面上点「重新生成」
check("**回调里身份为空时，分支写进自己的分区**（这正是用户报的那个 bug）",
      "b2" in _branch_ids(SID4), _branch_ids(SID4))
check("身份是补上的，日志里有记录",
      any("修正为 alice" in line for line in sessions.RECENT_ACTIONS),
      sessions.RECENT_ACTIONS[:2])

SID5 = "2094-01-01_000000_004"
_callback_session(SID5)
storage.set_current_uid("someone_else")       # 回调线程：残留着别人的用户名
sessions.RECENT_ACTIONS.clear()
ai.regenerate(1)
check("**回调里身份串了时，分支仍然写进自己的分区**",
      "b2" in _branch_ids(SID5), _branch_ids(SID5))
check("串了的情况也记进日志",
      any("someone_else" in line and "alice" in line for line in sessions.RECENT_ACTIONS),
      sessions.RECENT_ACTIONS[:2])

SID6 = "2094-01-01_000000_005"
_callback_session(SID6)
sessions.RECENT_ACTIONS.clear()
sessions.st.session_state["_edit_text"] = "改写过的问题"
ERRORS.clear()
ai.submit_edit(0, "_edit_text")
check("编辑用户消息的回调同样先定身份（不报错、正常建分支）",
      "b2" in _branch_ids(SID6) and not ERRORS, (_branch_ids(SID6), ERRORS))
check("身份本来就对时不写多余的日志",
      not any("修正为" in line for line in sessions.RECENT_ACTIONS),
      sessions.RECENT_ACTIONS[:3])

# --------------------------------------------------------------------------- #
# 4. describe()：把分区与"磁盘上有、meta 里没有"的分支摊出来
# --------------------------------------------------------------------------- #
sessions.st.session_state = fresh(_uid="alice", current_session=SID2,
                                  current_branch="main", message=[dict(m) for m in MSG])
storage.set_current_uid("alice")
text = branches.describe(SID2, "main")
check("describe() 显示用户分区与后端", "用户分区：alice" in text, text)
check("describe() 列出磁盘上的分支文件", "磁盘分支文件" in text, text)

# 造一个"磁盘上有、meta 里没有"的分支文件（正是故障现场的样子）
storage.write_json(f"sessions/{SID2}/branches/b9.json", {"message": []})
text = branches.describe(SID2, "main")
check("**磁盘多出来的分支文件会被点名**（这就是「分支建了但看不见」的现场）",
      "⚠" in text and "b9" in text, text)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
