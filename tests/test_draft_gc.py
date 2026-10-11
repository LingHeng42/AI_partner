"""草稿不会随会话删除而清理 —— 这条泄漏必须堵住（用户发现的问题）。

背景：每条「新建会话」都会立刻写一个草稿（`sessions/drafts/<会话ID>.draft`），
这样改人设/头像能即时落盘。如果这条会话**一直没发过消息**，它就不进会话历史 ——
用户在界面上看不到它、也就删不掉它，草稿于是永远留在库里，用几次就攒一堆。

**关键约束（有测试守着，不能违反）**：草稿里存着头像/人设等设置，用户切走再切回来
时那些设置必须还在。所以清理标准是**年龄**（明显没人会再回去的才清），
绝不能是"不是当前会话就清"。

用法：python tests/test_draft_gc.py
"""
import datetime
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

from lingheng import config, sessions, storage, store  # noqa: E402

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


def sid(days_ago: float, seq: int = 0) -> str:
    """造一个"days_ago 天前"的会话 ID。

    格式必须和 store.new_session_id() 一致：``%Y-%m-%d_%H%M%S_%f`` 截到毫秒，
    也就是最后一段是 **3 位、可以有前导零** 的毫秒（例如 _003）。
    这里用 "100 + seq" 避免歧义，保证长度固定。
    """
    moment = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%d_%H%M%S_") + f"{100 + seq}"


FRESH = sid(0, 1)     # 今天的新会话（草稿必须留）
OLD1 = sid(3, 1)      # 3 天前（过期）
OLD2 = sid(30, 1)     # 30 天前（过期）


def write_draft(session_id, title="新会话"):
    storage.write_json(f"sessions/{store.BRANCHES_SUBDIR and 'drafts'}/{session_id}.draft",
                       {"title": title, "message": [], "user_avatar": "a/b.png"})


for name in (FRESH, OLD1, OLD2):
    write_draft(name)

check("三个草稿都已写入", sessions.draft_ids() == sorted([FRESH, OLD1, OLD2]),
      sessions.draft_ids())


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


sessions.st.session_state = _State({})

# --------------------------------------------------------------------------- #
# 1. 年龄判断
# --------------------------------------------------------------------------- #
check("今天的草稿年龄 < 1 天", sessions._session_id_age_days(FRESH) < 1.0,
      sessions._session_id_age_days(FRESH))
check("3 天前的草稿年龄 >= 3 天", sessions._session_id_age_days(OLD1) >= 3.0,
      sessions._session_id_age_days(OLD1))
check("解析不出来的 ID 当作很新（宁可不删）",
      sessions._session_id_age_days("这不是时间戳") == 0.0)

# --------------------------------------------------------------------------- #
# 2. 只清过期草稿；新鲜的必须留下
# --------------------------------------------------------------------------- #
removed = sessions.purge_stale_drafts(keep_days=1.0)
check("清掉了 2 个过期草稿", removed == 2, removed)
check("**今天的草稿被保留**（用户可能切回去，草稿里存着头像/人设）",
      FRESH in sessions.draft_ids(), sessions.draft_ids())
check("过期草稿已删除", OLD1 not in sessions.draft_ids() and OLD2 not in sessions.draft_ids(),
      sessions.draft_ids())

# 当前会话即使是旧的也保留
write_draft(OLD2)
removed = sessions.purge_stale_drafts(keep_days=1.0, current=OLD2)
check("当前会话的草稿即使过期也保留", OLD2 in sessions.draft_ids(), sessions.draft_ids())
check("其余过期草稿仍被清", removed == 0, removed)

# --------------------------------------------------------------------------- #
# 3. 正式落盘 / 删除会话时，草稿要立刻清掉（原有行为不能退化）
# --------------------------------------------------------------------------- #
LIVE = sid(0, 2)
write_draft(LIVE)
sessions.st.session_state = _State({"current_session": LIVE, "current_branch": "main"})
for key, value in config.DEFAULT_PROFILE.items():
    sessions.st.session_state.setdefault(key, value)
sessions.st.session_state["message"] = [{"role": "user", "content": "第一条"}]
sessions.save_session()
check("产生对话后草稿立刻被清掉（正式落盘）", LIVE not in sessions.draft_ids(),
      sessions.draft_ids())

write_draft(OLD2)
sessions.delete_session(OLD2)
check("删除会话时草稿一起清掉", OLD2 not in sessions.draft_ids(), sessions.draft_ids())

# --------------------------------------------------------------------------- #
# 4. draft_ids 不是会话来路（不能把 drafts 目录当会话列出来）
# --------------------------------------------------------------------------- #
write_draft(sid(0, 9))
check("drafts 目录不会被当成会话列出来",
      all("draft" not in name for name in storage.session_ids()), storage.session_ids())

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
