"""端到端：切换会话 / 切换分支必须真正生效（不留下"异常会话"）。

背景（线上真实故障）：切换会话和切换分支的按钮回调直接调用了
``load_selected_session`` / ``branches.switch_branch``，而这两个函数会修改
``st.session_state``（current_session、message、current_branch……）。Streamlit 会
**回滚回调期间的 session_state 变更**，于是重跑后 current_session 变回旧的，
主区域继续显示上一个会话 —— 用户看到的就是"切换会话产生异常会话"。

修法与"删除会话"一致：回调只登记待办，实际切换放在主脚本体
（run_pending_actions）里执行。

用法：python tests/test_switch_flow.py
"""
import json
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

UID = "lingheng42"
A = "2050-01-01_000000_000"
B = "2050-02-02_000000_000"
storage.set_current_uid(UID)


def seed(sid, text, branches_extra=None):
    items = [{"id": "main", "parent": None, "fork_index": None, "created_at": ""}]
    items += branches_extra or []
    storage.write_json(f"sessions/{sid}/meta.json", {
        "title": f"会话{sid[-3:]}", "pinned": False, "current_branch": "main",
        "branches": items, "nickname": "溟月", "nature": "n",
        "role_description": "r", "output_rules": "o",
    })
    storage.write_json(f"sessions/{sid}/branches/main.json",
                       {"message": [{"role": "user", "content": f"{text}-{i}"} for i in range(2)]})
    for extra in (branches_extra or []):
        storage.write_json(f"sessions/{sid}/branches/{extra['id']}.json",
                           {"message": [{"role": "user", "content": f"{text}-分支{extra['id']}"}]})


seed(A, "甲")
seed(B, "乙", branches_extra=[{"id": "b2", "parent": "main", "fork_index": 0, "created_at": ""}])


class _State(dict):
    """模拟真实 session_state：属性读写都走下标（和 Streamlit 的代理一致）。"""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as exc:
            raise AttributeError(k) from exc

    def __setattr__(self, k, v):
        self[k] = v

    def get(self, k, default=None):
        return dict.get(self, k, default)

    def pop(self, k, default=None):
        return dict.pop(self, k, default)


def fresh_state(current=A):
    state = _State({"current_session": current, "current_branch": "main", "message": []})
    for key, value in config.DEFAULT_PROFILE.items():
        state.setdefault(key, value)
    for key, value in config.DEFAULT_ADVANCED.items():
        state.setdefault(key, value)
    for key in config.AVATAR_KEYS:
        state.setdefault(key, None)
    return state


sessions.st.session_state = fresh_state(A)
sessions.st.rerun = lambda: None


def contents():
    return [m["content"] for m in (sessions.st.session_state.get("message") or [])]


# --------------------------------------------------------------------------- #
# 1. 切换会话：回调只登记；执行后必须真的换过去
# --------------------------------------------------------------------------- #
sessions.request_load_session(B)
check("切换会话的回调只登记待办，不直接改 current_session",
      sessions.st.session_state.get(sessions.PENDING_LOAD) == B
      and sessions.st.session_state["current_session"] == A,
      (sessions.st.session_state.get(sessions.PENDING_LOAD),
       sessions.st.session_state["current_session"]))

sessions.run_pending_actions()

check("**切换后 current_session 真的变了**",
      sessions.st.session_state["current_session"] == B,
      sessions.st.session_state["current_session"])
check("**切换后消息换成了目标会话的**",
      contents() == ["乙-0", "乙-1"], contents())
check("切换后待办标记被清掉",
      sessions.st.session_state.get(sessions.PENDING_LOAD) is None,
      sessions.st.session_state.get(sessions.PENDING_LOAD))
check("切换后消息归属标记指向新会话",
      sessions.message_owner(B, "main") == sessions.st.session_state.get(sessions.OWNER_STATE),
      sessions.st.session_state.get(sessions.OWNER_STATE))
check("**源会话的内容没被改动**（不产生异常会话）",
      [m["content"] for m in sessions.read_branch(A, "main")["message"]] == ["甲-0", "甲-1"])

# 再切回去，确认双向都正常
sessions.request_load_session(A)
sessions.run_pending_actions()
check("切回原会话同样生效",
      sessions.st.session_state["current_session"] == A and contents() == ["甲-0", "甲-1"],
      (sessions.st.session_state["current_session"], contents()))

# --------------------------------------------------------------------------- #
# 2. 切换分支：回调只登记；执行后消息必须换成分支内容
# --------------------------------------------------------------------------- #
sessions.st.session_state = fresh_state(B)
sessions.request_switch_branch(B, "b2")
check("切换分支的回调只登记，不直接改 current_branch",
      sessions.st.session_state.get(sessions.PENDING_BRANCH) == (B, "b2")
      and sessions.st.session_state["current_branch"] == "main",
      (sessions.st.session_state.get(sessions.PENDING_BRANCH),
       sessions.st.session_state["current_branch"]))

sessions.run_pending_actions()

check("**切换后 current_branch 真的变了**",
      sessions.st.session_state["current_branch"] == "b2",
      sessions.st.session_state["current_branch"])
check("**切换分支后消息换成了该分支的内容**",
      contents() == ["乙-分支b2"], contents())
check("meta 里的分支指针也更新了",
      sessions.load_session_meta(B)["current_branch"] == "b2",
      sessions.load_session_meta(B)["current_branch"])
check("切分支不会动另一条分支的内容",
      [m["content"] for m in sessions.read_branch(B, "main")["message"]] == ["乙-0", "乙-1"])

# --------------------------------------------------------------------------- #
# 3. 删除分支：回调只登记；执行后消息跟着换到新分支
# --------------------------------------------------------------------------- #
sessions.request_delete_branch(B, "b2")
sessions.run_pending_actions()
check("删除分支已执行（分支文件没了）", not storage.exists(
    __import__("lingheng.store", fromlist=["x"]).branch_path(B, "b2")))
check("删除当前分支后自动切到剩余分支",
      sessions.st.session_state["current_branch"] == "main",
      sessions.st.session_state["current_branch"])
check("删除分支后消息换成了剩余分支的内容", contents() == ["乙-0", "乙-1"], contents())

# --------------------------------------------------------------------------- #
# 4. 动作日志要能完整反映链路
# --------------------------------------------------------------------------- #
log = "\n".join(sessions.RECENT_ACTIONS)
check("日志里有切换会话的登记与执行",
      "登记切换会话" in log and "已切换到会话" in log, sessions.RECENT_ACTIONS[-4:])
check("日志里有切换分支的登记与执行",
      "登记切换分支" in log and "已切换到分支" in log, sessions.RECENT_ACTIONS[-4:])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
