"""防"用空内容覆盖历史"的回归测试。

背景（真实事故）：切换会话/分支后历史变空，而且 AI 也不再有上下文。
根因是**读失败被当成了"空分支"**，随后又被写回磁盘：

    read_branch() 里 except Exception: return {"message": []}
    switch_branch() 接着把读到的内容原样写回去   ← 一次网络抖动就永久覆盖历史

这条路径在数据库延迟高的时候极易触发（读超时很常见），所以用户"越慢越容易丢"。

三层防护，本测试逐个钉住：
  1. 读失败必须抛异常，不能静默变成空
  2. write_branch 拒绝用空内容覆盖非空分支
  3. 切换分支只改指针，不写分支内容

用法：python tests/test_no_data_loss.py
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

from lingheng import sessions, storage, store  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


SID = "2026-05-05_000000_000"
storage.set_current_uid("local")


def seed(sid: str, contents: list) -> None:
    storage.write_json(f"sessions/{sid}/meta.json", {
        "title": sid, "pinned": False, "current_branch": "main",
        "branches": [{"id": "main", "parent": None, "fork_index": None, "created_at": ""}],
    })
    storage.write_json(f"sessions/{sid}/branches/main.json", {
        "message": [{"role": "user", "content": c} for c in contents]
    })


seed(SID, ["一", "二", "三"])


def on_disk() -> list:
    return [m["content"] for m in storage.read_json(f"sessions/{SID}/branches/main.json",
                                                    {}).get("message", [])]


# --------------------------------------------------------------------------- #
# 1. 读失败必须抛异常（不能静默变成空）
# --------------------------------------------------------------------------- #
check("正常情况下读得到内容", [m["content"] for m in sessions.read_branch(SID, "main")["message"]]
      == ["一", "二", "三"])

# 模拟"存储层读取失败"（数据库超时 / 网络抖动）
_orig_get = storage._CachedBackend.get


def failing_get(self, rel):
    if rel.endswith("main.json"):
        raise TimeoutError("模拟数据库读取超时")
    return _orig_get(self, rel)


storage._CachedBackend.get = failing_get
try:
    raised = False
    try:
        sessions.read_branch(SID, "main")
    except TimeoutError:
        raised = True
    check("读失败时抛异常，而不是返回空分支（这是丢数据的根源）", raised)

    # 关键断言：读失败之后，磁盘内容必须还在
    storage._CachedBackend.get = _orig_get
    check("读失败不会改动磁盘内容", on_disk() == ["一", "二", "三"], on_disk())
finally:
    storage._CachedBackend.get = _orig_get


# --------------------------------------------------------------------------- #
# 2. write_branch 拒绝用空内容覆盖非空分支
# --------------------------------------------------------------------------- #
before = on_disk()
sessions.write_branch(SID, "main", {"message": []})          # 默认不允许
check("空内容不会覆盖已有历史（防误删）", on_disk() == before, on_disk())

sessions.write_branch(SID, "main", {"message": []}, force=True)  # 显式 force 才行
check("显式 force=True 时才真的清空", on_disk() == [], on_disk())

# 恢复内容，供第 3 条测试用
seed(SID, ["一", "二", "三"])
check("内容已恢复", on_disk() == ["一", "二", "三"], on_disk())


# --------------------------------------------------------------------------- #
# 3. 缺失的分支才补空文件；已存在的分支切换时一个字节都不动
# --------------------------------------------------------------------------- #
_orig_write = sessions.write_branch
writes = []


def spy_write(session_name, branch_id, data, **kwargs):
    writes.append((branch_id, list(data.get("message") or [])))
    return _orig_write(session_name, branch_id, data, **kwargs)


sessions.write_branch = spy_write
try:
    from lingheng import branches
    branches.sessions.write_branch = spy_write
    # 切换到已存在的 main：不应产生任何写
    branches.switch_branch(SID, "main")
    check("切换到已存在的分支时不写分支内容", not [w for w in writes if w[0] == "main"], writes)
    check("切换后磁盘内容仍完好", on_disk() == ["一", "二", "三"], on_disk())

    # 切换到 meta 里登记过、但文件不存在的分支：应补一个空文件
    meta = sessions.load_session_meta(SID)
    meta["branches"] = list(meta["branches"]) + [
        {"id": "b9", "parent": "main", "fork_index": 0, "created_at": ""}]
    sessions.write_session_meta(SID, meta)
    writes.clear()
    branches.switch_branch(SID, "b9")
    check("缺失的分支会被补成空文件", any(w[0] == "b9" for w in writes), writes)
    check("补空文件不影响已有分支", on_disk() == ["一", "二", "三"], on_disk())
finally:
    sessions.write_branch = _orig_write
    from lingheng import branches as _b
    _b.sessions.write_branch = _orig_write


# --------------------------------------------------------------------------- #
# 4. 保存会话时，内存为空不能抹掉存档（端到端）
# --------------------------------------------------------------------------- #
class _FakeState(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as exc:
            raise AttributeError(k) from exc

    def __setattr__(self, k, v):
        self[k] = v


sessions.st.session_state = _FakeState({
    "current_session": SID,
    "current_branch": "main",
    "session_title": SID,
    "message": [],              # ← 模拟"内存里没有消息"（例如读失败后）
    **{k: v for k, v in __import__("lingheng.config", fromlist=["x"]).DEFAULT_PROFILE.items()},
})
sessions.save_session()
check("内存为空时保存不会抹掉存档", on_disk() == ["一", "二", "三"], on_disk())

# 正常保存（内存里有消息）应当照旧工作
sessions.st.session_state["message"] = [{"role": "user", "content": "新的"}]
sessions.save_session()
check("内存里有消息时正常保存", on_disk() == ["新的"], on_disk())

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
