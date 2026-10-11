"""端到端（MongoDB 后端）：登记删除 → 执行待办 → **数据库里的键真的没了**。

为什么必须有这个测试：用户报的是"删除会话时数据库里的数据其实并没有被删除"。
之前只测了 storage.delete_prefix 这个函数本身（它是好的），但真实链路上还隔着
回调登记、待办执行、重跑。任何一环断了，函数级测试都发现不了 —— 而这次断的正是
delete_session 内部的 st.rerun() 把脚本掐断（主脚本开头就中断，后面的渲染、
甚至后续待办都不会执行）。

用法：python tests/test_delete_mongo.py
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

from lingheng import sessions, storage, store  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


CLIENT = mongomock.MongoClient()
storage._client = lambda: CLIENT
storage._mongo_lookup = lambda: ("mongodb://fake", "probe", "")
storage._mongo_ready.clear()
storage.reset_cache()

UID = "lingheng42"
storage.set_current_uid(UID)
KEEP = "2040-01-01_000000_000"
VICTIM = "2040-02-02_000000_000"


def seed(sid, text):
    storage.write_json(f"sessions/{sid}/meta.json", {
        "title": sid, "pinned": False, "current_branch": "main",
        "branches": [{"id": "main", "parent": None, "fork_index": None, "created_at": ""}],
    })
    storage.write_json(f"sessions/{sid}/branches/main.json",
                       {"message": [{"role": "user", "content": f"{text}-{i}"} for i in range(3)]})


def db_keys():
    return sorted(d["key"] for d in CLIENT["lingheng"]["files"].find({"uid": UID}))


seed(KEEP, "保留")
seed(VICTIM, "待删")

check("后端确实是 MongoDB 模式", storage.backend_kind() == storage.MONGO_BACKEND,
      storage.backend_kind())
check("两条会话都已写入数据库", len(db_keys()) == 4, db_keys())
check("会话列表能读到两条", set(sessions.load_session_list()) == {KEEP, VICTIM},
      sessions.load_session_list())

# --------------------------------------------------------------------------- #
# 走真实链路：回调登记 → 主脚本体执行待办
# --------------------------------------------------------------------------- #
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

    def pop(self, k, default=None):
        return dict.pop(self, k, default)


state = _State({
    "current_session": VICTIM,          # 正在删除"当前会话"——最容易出问题的那种
    "current_branch": "main",
    "session_title": VICTIM,
    "message": [{"role": "user", "content": "hi"}],
})
for key, value in __import__("lingheng.config", fromlist=["x"]).DEFAULT_PROFILE.items():
    state.setdefault(key, value)
sessions.st.session_state = state
sessions.st.rerun = lambda: None         # 假 st：记录而不是真的中断脚本

sessions.request_delete_session(VICTIM)
check("回调只登记待办，不动 current_session",
      state.get("_pending_delete") == VICTIM and state["current_session"] == VICTIM,
      (state.get("_pending_delete"), state["current_session"]))

sessions.run_pending_actions()

check("**数据库里待删会话的键真的没了**",
      not [k for k in db_keys() if VICTIM in k], db_keys())
check("保留的会话没被波及", len([k for k in db_keys() if KEEP in k]) == 2, db_keys())
check("保留的会话内容完好",
      [m["content"] for m in sessions.read_branch(KEEP, "main")["message"]]
      == ["保留-0", "保留-1", "保留-2"])
check("待办标记被清掉", state.get("_pending_delete") is None, state.get("_pending_delete"))
check("自动切到了新会话", state.get("current_session") != VICTIM, state.get("current_session"))

# --------------------------------------------------------------------------- #
# 前缀边界：删 sessions/<ID> 不能误伤 sessions/<ID>xyz 这种"前缀相似"的会话。
# 之前的实现用 $regex 前缀匹配，对转义/边界很敏感；现在改成服务端范围查询
# （key > prefix/ 且 key < prefix0），这里把边界钉住。
# --------------------------------------------------------------------------- #
SIMILAR = VICTIM + "xyz"
seed(SIMILAR, "相似")
before_keys = db_keys()
check("前缀相似的会话已写入", any(f"/{SIMILAR}/" in k for k in before_keys), before_keys)
removed = storage.delete_prefix(store._session_dir(SIMILAR))
check("删除前缀相似的会话返回 2（精确匹配，不多删）", removed == 2, removed)
check("前缀相似的会话确实被删掉了", not [k for k in db_keys() if f"/{SIMILAR}/" in k], db_keys())
check("保留的会话没被这次删除波及", len([k for k in db_keys() if f"/{KEEP}/" in k]) == 2, db_keys())

# 删一个不存在的会话应当返回 0（而不是"假装成功"）
check("删除不存在的会话返回 0", storage.delete_prefix(store._session_dir("2099-99-99_000000_000")) == 0)
check("待删会话已从列表消失", VICTIM not in sessions.load_session_list(),
      sessions.load_session_list())
check("保留的会话仍在列表里", KEEP in sessions.load_session_list(),
      sessions.load_session_list())

# 动作日志应当完整出现三行（曾经缺第三行，说明没返回）
log = "\n".join(sessions.RECENT_ACTIONS)
check("日志里有「回调：登记删除」", "回调：登记删除" in log, sessions.RECENT_ACTIONS[-3:])
check("日志里有「发现待办」", "发现待办" in log, sessions.RECENT_ACTIONS[-3:])
check("日志里有「已执行」（曾经永远不出现，说明被 st.rerun 掐断）",
      "已执行" in log, sessions.RECENT_ACTIONS[-3:])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
