"""验证"清理草稿不影响正常使用"。

用户的疑问很合理："草稿的清除会影响使用吗？"
这个测试把各种情况的**实际后果**跑出来，而不是靠推理：

  1. 已经聊过的会话：根本没有草稿 → 清理不可能影响它
  2. 只有草稿、从没发过消息的会话：草稿没了会怎样？（期望：退化成全新会话，
     仍然能正常工作，只是那套没保存过的设置回到默认值）
  3. 正在使用的会话：草稿必须被保留（清理不能碰它）
  4. 清理之后继续聊天：消息、人设、头像是否正常落盘

用法：python tests/test_draft_safety.py
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


def sid(days_ago=0, seq=100):
    moment = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%d_%H%M%S_") + str(seq)


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


def fresh_state(**kw):
    state = _State(kw)
    for key, value in config.DEFAULT_PROFILE.items():
        state.setdefault(key, value)
    for key, value in config.DEFAULT_ADVANCED.items():
        state.setdefault(key, value)
    for key in config.AVATAR_KEYS:
        state.setdefault(key, None)
    state.setdefault("message", [])
    return state


sessions.st.session_state = fresh_state()
sessions.st.rerun = lambda: None

# --------------------------------------------------------------------------- #
# 1. 已经聊过的会话：本来就没有草稿
# --------------------------------------------------------------------------- #
CHATTED = sid(0, 111)
sessions.st.session_state = fresh_state(current_session=CHATTED, current_branch="main")
sessions.st.session_state["session_title"] = "聊过的会话"
sessions.st.session_state["message"] = [{"role": "user", "content": "你好"}]
sessions.save_session()

check("聊过之后没有草稿（设置已存进 meta.json）", CHATTED not in sessions.draft_ids(),
      sessions.draft_ids())
check("消息已落盘", [m["content"] for m in sessions.read_branch(CHATTED, "main")["message"]]
      == ["你好"])
check("清理草稿后，聊过的会话完全不受影响",
      (sessions.purge_stale_drafts(keep_days=0, current=None) >= 0)
      and storage.exists(f"sessions/{CHATTED}/meta.json")
      and [m["content"] for m in sessions.read_branch(CHATTED, "main")["message"]] == ["你好"])

# --------------------------------------------------------------------------- #
# 2. 只有草稿、从没发过消息的旧会话：草稿被清后要能优雅降级
# --------------------------------------------------------------------------- #
LONELY = sid(5, 222)
storage.write_json(sessions.draft_path(LONELY), {
    "title": "改过名字的草稿会话",
    "message": [],
    "nature": "自定义性格",
    "user_avatar": None,
})
check("旧草稿已写入", LONELY in sessions.draft_ids(), sessions.draft_ids())

removed = sessions.purge_stale_drafts(keep_days=1, current=CHATTED)
check("过期草稿被清理", removed >= 1 and LONELY not in sessions.draft_ids(),
      (removed, sessions.draft_ids()))

# 清掉之后仍然可以正常"切到"这条会话：退化成全新会话，不报错。
# 注意：既没有存档、也没有草稿时，load_selected_session 会走"当成全新会话"的
# 分支（重置人设与头像后直接返回，**不设置 current_session** —— 这是对的，
# 因为真正切换 current_session 的职责在 pending 路由里）。
sessions.st.session_state = fresh_state()
sessions.load_selected_session(LONELY)
check("草稿被清后加载它不报错（走全新会话分支）",
      sessions.st.session_state.get("nature") == config.DEFAULT_PROFILE["nature"],
      sessions.st.session_state.get("nature"))
check("它仍然被当作「新建会话」状态（可以接着用）", sessions.is_fresh_session())

# 走真实的挂起路由（会设置 current_session 并重跑）
sessions.st.session_state = fresh_state()
sessions.request_load_session(LONELY)
sessions.run_pending_actions()
check("草稿被清后切过去仍然正常", sessions.st.session_state["current_session"] == LONELY,
      sessions.st.session_state["current_session"])

# 继续聊天：应当正常落盘、正常进历史
sessions.st.session_state["message"] = [{"role": "user", "content": "重新开始"}]
sessions.save_session()
check("草稿被清后继续聊天照样正常落盘",
      [m["content"] for m in sessions.read_branch(LONELY, "main")["message"]] == ["重新开始"])
check("之后它出现在会话历史里", LONELY in sessions.load_session_list(),
      sessions.load_session_list())

# --------------------------------------------------------------------------- #
# 3. 正在使用的会话：清理绝不允许碰它
# --------------------------------------------------------------------------- #
ACTIVE = sid(30, 333)          # 故意用一个"30 天前"的 ID
storage.write_json(sessions.draft_path(ACTIVE), {"title": "正在调的会话", "message": [],
                                                 "nature": "我不希望丢"})
check("当前会话的旧草稿已写入", ACTIVE in sessions.draft_ids(), sessions.draft_ids())
sessions.purge_stale_drafts(keep_days=1, current=ACTIVE)
check("**正在使用的会话草稿被保留**（无论多旧）", ACTIVE in sessions.draft_ids(),
      sessions.draft_ids())
check("它的人设还在", storage.read_json(sessions.draft_path(ACTIVE), {}).get("nature")
      == "我不希望丢")

# --------------------------------------------------------------------------- #
# 4. 用 AppTest 跑一遍真实入口：确认没有草稿也能正常渲染
# --------------------------------------------------------------------------- #
for name in sessions.draft_ids():
    sessions.discard_draft(name)
check("草稿已全部清空（模拟最坏情况）", sessions.draft_ids() == [], sessions.draft_ids())

from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(PROJECT / "AI_partner.py"), default_timeout=90).run()
check("没有草稿时真实入口正常渲染", not at.exception,
      [e.message[:200] for e in at.exception])
check("没有草稿时仍然画出页面（不是空白）", len(at.main) >= 1, len(at.main))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
