"""回归：编辑消息后再发消息的落点，以及"最新一轮有没有 ⋯"。

两个真实问题（用户报的）：
  1. 「编辑信息后，再发信息会跳到其他分支，而非依据编辑后的信息」
  2. 「最新的一轮对话下没有 ⋯，除非切换一次会话」

成因与修法（都在 lingheng/ai.py）：
- 问题 2 是**渲染时机**：历史回放（render_history）在本轮脚本里跑在用户这条消息
  之前，画不到刚生成出来的回答；而 ⋯ 菜单 / 分支箭头 / 重新生成只在历史回放里画。
  于是最新一轮永远缺这些入口，要等下一次重跑才补上。修法：生成完统一 st.rerun()
  一次（render_reply / render_pending_regen），让两个时候渲染同一套东西。
- 问题 1 表面上是"跳分支"，实际数据一直是对的。这个套件把"落点"钉死：编辑后
  继续发的消息，必须写进**编辑产生的那条分支**，并且模型收到的上下文就是那条
  分支的内容（不能是原分支）。

运行：
    .venv\\Scripts\\python.exe tests\\test_edit_flow.py
"""
import json
import os
import sys
from pathlib import Path

import _harness  # noqa: E402
from _harness import PROJECT, TMP_DIR  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


os.environ["AI_PARTNER_DISABLE_AUTH"] = "1"
entry = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")

FAKE = TMP_DIR / "_edit_flow"
FAKE.mkdir(parents=True, exist_ok=True)
CALLS_LOG = FAKE / "_calls.jsonl"
CALLS_LOG.write_text("", encoding="utf-8")

# 假模型：固定回答（便于断言）+ 把每次请求的消息内容落盘（看模型到底看到了什么）
STUB = f'''
import json as _json
from pathlib import Path as _Path
import lingheng.ai as _ai

class _Delta:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.reasoning_content = reasoning

class _Choice:
    def __init__(self, delta):
        self.delta = delta

class _Chunk:
    def __init__(self, content=None, reasoning=None):
        self.choices = [_Choice(_Delta(content, reasoning))]

_ANSWERS = []

class _Completions:
    def create(self, **kw):
        msgs = kw.get("messages") or []
        seen = [m.get("content", "")[:40] for m in msgs]
        with _Path({str(CALLS_LOG).replace(chr(92), "/")!r}).open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(seen, ensure_ascii=False) + "\\n")
        _ANSWERS.append(len(_ANSWERS) + 1)
        return iter([_Chunk(content="回答%d" % _ANSWERS[-1])])

class _Chat:
    completions = _Completions()

class _Client:
    chat = _Chat()

_ai._client = lambda: _Client()
_ai.images.current_avatar = lambda role: None
'''

wrapper = FAKE / "_app.py"
wrapper.write_text(
    "import sys, os\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    "import streamlit as st\n"
    "st.session_state['user_api_key'] = 'sk-test'\n"
    + STUB
    + "\n"
    + entry,
    encoding="utf-8",
)

at = AppTest.from_file(str(wrapper), default_timeout=90).run()


def model_calls():
    if not CALLS_LOG.exists():
        return []
    return [json.loads(line) for line in
            CALLS_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


def send(text):
    at.chat_input[0].set_value(text).run()
    return at


# --------------------------------------------------------------------------- #
# 1. 发消息 → 最新一轮必须当场就有 ⋯（不必等切换会话）
# --------------------------------------------------------------------------- #
at = send("第一个问题")
check("发第一条消息无异常", not list(at.exception),
      [e.message[:120] for e in at.exception])
check("回答已进入内存消息", [m["content"] for m in at.session_state.message]
      == ["第一个问题", "回答1"],
      [m["content"] for m in at.session_state.message])

at = send("第二个问题")
check("发第二条消息无异常", not list(at.exception),
      [e.message[:120] for e in at.exception])
indexes = sorted({p.key.rsplit("_", 1)[-1] for p in at.get("popover")
                  if (p.key or "").startswith("msg_menu_")})
check("最新一轮当场就有 ⋯（历史回放把 4 条都画了）", indexes == ["0", "1", "2", "3"], indexes)
regen = [b for b in at.button if b.label == "重新生成"]
check("最新一轮的「重新生成」也在（只有最后一条回答该有）", len(regen) == 1,
      [b.key for b in regen])

# --------------------------------------------------------------------------- #
# 2. 编辑第 0 条用户消息 → 新分支，内容 = 改写后的那条 + 新回答
# --------------------------------------------------------------------------- #
edit_buttons = [b for b in at.button if b.label == "编辑这条"]
check("每条消息都有「编辑这条」", len(edit_buttons) == 4, len(edit_buttons))
at = edit_buttons[0].click().run()
boxes = [t for t in at.text_area if "编辑这条消息" in (t.label or "")]
check("点开后出现编辑框", len(boxes) == 1, [t.label for t in at.text_area])
boxes[0].set_value("改写过的问题")
save = [b for b in at.button if b.label == "保存并重新生成"]
check("出现「保存并重新生成」", len(save) == 1, len(save))
at = save[0].click().run()

check("保存后无异常", not list(at.exception), [e.message[:120] for e in at.exception])
branch_after_edit = at.session_state.get("current_branch")
check("编辑产生了新分支（不再停在 main）", branch_after_edit not in (None, "main"),
      branch_after_edit)
check("新分支内容 = 改写后的消息 + 新回答",
      [m["content"] for m in at.session_state.message] == ["改写过的问题", "回答1"],
      [m["content"] for m in at.session_state.message])
check("编辑后的那一轮当场就有 ⋯（与问题 2 同一处修法）",
      any((p.key or "").startswith(f"msg_menu_{branch_after_edit}_")
          for p in at.get("popover")),
      [p.key for p in at.get("popover")])

# --------------------------------------------------------------------------- #
# 3. 编辑之后再发一条 → 必须落在编辑后的那条分支上，且上下文就是它
# --------------------------------------------------------------------------- #
before_calls = len(model_calls())
at = send("编辑后的追问")
after_calls = model_calls()[before_calls:]

check("追问无异常", not list(at.exception), [e.message[:120] for e in at.exception])
check("追问后仍停在编辑产生的分支上",
      at.session_state.get("current_branch") == branch_after_edit,
      f"{branch_after_edit} -> {at.session_state.get('current_branch')}")
check("内存消息 = 编辑后的分支 + 追问 + 回答",
      [m["content"] for m in at.session_state.message]
      == ["改写过的问题", "回答1", "编辑后的追问", "回答1"],
      [m["content"] for m in at.session_state.message])
check("本轮只请求了模型一次", len(after_calls) == 1, len(after_calls))
if after_calls:
    seen = after_calls[0]
    check("模型看到的是编辑后的分支（含改写后的消息、不含原消息）",
          any("改写过的问题" in s for s in seen)
          and not any("第一个问题" in s for s in seen),
          seen)
    check("模型收到了追问，且它是最后一条", seen[-1].startswith("编辑后的追问"), seen[-1:])
check("追问那一轮当场就有 ⋯",
      any((p.key or "").startswith(f"msg_menu_{branch_after_edit}_")
          for p in at.get("popover")),
      [p.key for p in at.get("popover")])

# --------------------------------------------------------------------------- #
# 4. 磁盘上的分支内容：原分支必须完好，新分支承接追问
# --------------------------------------------------------------------------- #
sid = at.session_state.get("current_session")
root = _harness.session_dir(sid) / "branches"
on_disk = {}
for f in sorted(root.glob("*.json")):
    payload = json.loads(f.read_text(encoding="utf-8"))
    on_disk[f.stem] = [m["content"] for m in payload.get("message", [])]
check("原分支内容没被覆盖",
      on_disk.get("main") == ["第一个问题", "回答1", "第二个问题", "回答1"],
      on_disk.get("main"))
check("编辑分支承接了追问",
      on_disk.get(branch_after_edit)
      == ["改写过的问题", "回答1", "编辑后的追问", "回答1"],
      on_disk.get(branch_after_edit))
meta = json.loads(_harness.session_meta_path(sid).read_text(encoding="utf-8"))
check("meta 里的 current_branch 与界面一致",
      meta.get("current_branch") == branch_after_edit,
      meta.get("current_branch"))

print()
if failures:
    print("失败:", ", ".join(failures))
    sys.exit(1)
print("test_edit_flow 全部通过")
