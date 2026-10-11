"""迁移"写错分区"的数据（users/local → 目标用户）。

背景（线上真实故障）：早期 ui.main() 把"待办动作"排在设置用户名**之前**，而
storage 里的当前用户名默认是 "local"，于是首次渲染时那批写入（新建会话、草稿）
落进了 users/local/ —— 一个不属于任何人的分区。用户的观察完全正确：

    "local 是程序写出来的，现在只有我一个账号在用"

表现：界面里有会话，但在自己账号下查不到，删除还报告 0 个键。

这里验证 migrate_sessions.move_partition 能把它们搬到正确分区，并且
**不覆盖**目标分区已有的同名键（那可能是新数据，不能拿旧的盖掉）。

用法：python tests/test_partition_migrate.py
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
sys.path.insert(0, str(TESTS_DIR))
os.environ["AI_PARTNER_SESSIONS_DIR"] = str(_harness.SESSIONS_DIR)

import mongomock  # noqa: E402

from lingheng import sessions, storage  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


C = mongomock.MongoClient()
storage._client = lambda: C
storage._mongo_lookup = lambda: ("mongodb://fake", "lingheng", "")
storage._mongo_ready.clear()
storage.reset_cache()
COLL = C["lingheng"]["files"]


def put(uid, key, payload):
    COLL.insert_one({"uid": uid, "key": key, "data": json.dumps(payload).encode()})


def meta(title="新会话"):
    return {"title": title, "pinned": False, "current_branch": "main",
            "branches": [{"id": "main", "parent": None, "fork_index": None, "created_at": ""}]}


# 目标分区：一条真会话
put("lingheng42", "users/lingheng42/sessions/real/meta.json", meta("真会话"))
put("lingheng42", "users/lingheng42/sessions/real/branches/main.json",
    {"message": [{"role": "user", "content": "原本就在"}]})
# 错误分区（local）：一条"走错门"的完整会话 + 一个草稿
put("local", "users/local/sessions/stray/meta.json", meta("走错门的会话"))
put("local", "users/local/sessions/stray/branches/main.json",
    {"message": [{"role": "user", "content": "这条救得回来"}]})
put("local", "users/local/sessions/drafts/x.draft", {"title": "新会话", "message": []})
# 冲突场景：目标分区里已经有同名键（不能被旧数据覆盖）
put("lingheng42", "users/lingheng42/sessions/clash/meta.json", meta("新的"))
put("local", "users/local/sessions/clash/meta.json", meta("旧的"))


def keys(uid):
    return sorted(d["key"] for d in COLL.find({"uid": uid}))


check("初始：目标分区有 3 个键", len(keys("lingheng42")) == 3, keys("lingheng42"))
check("初始：局部区有 4 个键", len(keys("local")) == 4, keys("local"))

# 让 lingheng42 能看到自己分区的会话
storage.set_current_uid("lingheng42")
storage.reset_cache()
before_list = sessions.load_session_list()
check("迁移前：列表里只有本分区的会话", before_list == ["real"], before_list)

# --------------------------------------------------------------------------- #
# 执行迁移
# --------------------------------------------------------------------------- #
from migrate_sessions import move_partition  # noqa: E402

moved = move_partition("local", "lingheng42", delete=False, dry_run=False)
check("迁移了 3 个文档（冲突那个被跳过）", moved == 3, moved)
# 只应剩下"因为目标已存在而跳过"的那一条；其余都搬走了
check("局部区只剩冲突那一条（被安全跳过）",
      keys("local") == ["users/local/sessions/clash/meta.json"], keys("local"))
check("走错门的会话已到目标分区",
      "users/lingheng42/sessions/stray/meta.json" in keys("lingheng42"), keys("lingheng42"))

storage.set_current_uid("lingheng42")
storage.reset_cache()
after_list = sessions.load_session_list()
check("**迁移后能在自己账号下看到那条会话**", "stray" in after_list, after_list)
check("原本的会话还在", "real" in after_list, after_list)
check("救回来的会话内容完好",
      [m["content"] for m in sessions.read_branch("stray", "main")["message"]]
      == ["这条救得回来"])
check("冲突时保留目标分区的新数据（不被旧数据覆盖）",
      sessions.load_session_meta("clash")["title"] == "新的",
      sessions.load_session_meta("clash")["title"])

# 预演模式不得改动任何东西
put("local", "users/local/sessions/again/meta.json", meta("再来一条"))
before = keys("local")
move_partition("local", "lingheng42", delete=False, dry_run=True)
check("--dry-run 不改动数据", keys("local") == before, keys("local"))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
