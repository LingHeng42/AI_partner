"""回归：编辑消息后再发消息的落点，"最新一轮有没有 ⋯"，以及编辑后能不能切回原文。

三个真实问题（用户报的）：
  1. 「编辑信息后，再发信息会跳到其他分支，而非依据编辑后的信息」
  2. 「最新的一轮对话下没有 ⋯，除非切换一次会话」
  3. 「修改留言内容根本不能创建分支」

成因与修法（在 lingheng/ai.py 与 lingheng/branches.py）：
- 问题 2 是**渲染时机**：历史回放（render_history）在本轮脚本里跑在用户这条消息
  之前，画不到刚生成出来的回答；而 ⋯ 菜单 / 分支箭头 / 重新生成只在历史回放里画。
  于是最新一轮永远缺这些入口，要等下一次重跑才补上。修法：生成完统一 st.rerun()
  一次（render_reply / render_pending_regen），让两个时候渲染同一套东西。
- 问题 1 表面上是"跳分支"，实际数据一直是对的。这个套件把"落点"钉死：编辑后
  继续发的消息，必须写进**编辑产生的那条分支**，并且模型收到的上下文就是那条
  分支的内容（不能是原分支）。
- 问题 3 是**分支组算错**：新分支确实建了，但源分支（main）自己没有 fork_index，
  按"fork_index 相等"筛不进兄弟组，于是从新分支切回 main 之后，main 那边算出来
  只有它自己、箭头因为"只有 1 条"整个不渲染 —— 再也切不回改写版本，看起来就
  像"编辑没创建分支"。修法：源分支也算组员（见 branches.group_members），且箭头
  只画在**分叉点那一条消息**下面（见 ai._render_message_actions 的 forked_at）。

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

# --------------------------------------------------------------------------- #
# 5. 编辑必须"看得出来"创建了分支：分叉点上要有 ‹ 1/2 › 能切回原文
#
# 用户报的「修改留言内容根本不能创建分支」就是这个位置：分支确实建了，但源分支
# （main）不是分叉产生的、没有 fork_index 记录，以前按 fork_index 相等分组时它
# 进不了兄弟组 → 组里只剩新分支 → 箭头因为"只有 1 条"整个不渲染。用户改完留言
# 只看到内容被换掉、没有任何切换入口，自然认为"没有创建分支"。
# --------------------------------------------------------------------------- #
def arrows():
    return [b.key for b in at.button
            if (b.key or "").startswith(("branch_prev_", "branch_next_"))]


def branch_buttons():
    return [b.key for b in at.button
            if (b.key or "").startswith("branch_")
            and not (b.key or "").startswith(("branch_menu_", "branch_rename_",
                                              "branch_delete_", "branch_prev_",
                                              "branch_next_"))]


check("编辑后分叉点上出现分支箭头",
      sorted(arrows()) == [f"branch_next_{branch_after_edit}_0",
                           f"branch_prev_{branch_after_edit}_0"],
      arrows())
check("分叉点上的箭头显示成 2/2",
      any("2/2" in (c.value or "") for c in at.get("caption")), 
      [c.value for c in at.get("caption") if "分支" in (c.value or "")])
check("侧边栏列也列出了两条分支", len(branch_buttons()) == 2, branch_buttons())

# 切回原分支：内容必须是原文，且箭头要**还在**（否则就切不回来了）
at = [b for b in at.button if (b.key or "").startswith("branch_next_")][0].click().run()
check("› 切回了原分支 main", at.session_state.get("current_branch") == "main",
      at.session_state.get("current_branch"))
check("原分支内容完好",
      [m["content"] for m in at.session_state.message]
      == ["第一个问题", "回答1", "第二个问题", "回答1"],
      [m["content"] for m in at.session_state.message])
check("切回原分支后箭头仍在（能再切回去）",
      sorted(arrows()) == ["branch_next_main_0", "branch_prev_main_0"], arrows())
check("原分支上显示成 1/2",
      any("1/2" in (c.value or "") for c in at.get("caption")),
      [c.value for c in at.get("caption") if "分支" in (c.value or "")])

# 再切回编辑版本
at = [b for b in at.button if (b.key or "").startswith("branch_prev_")][0].click().run()
check("‹ 又切回编辑版本", at.session_state.get("current_branch") == branch_after_edit,
      at.session_state.get("current_branch"))
check("编辑版本内容完好（含之前那条追问）",
      [m["content"] for m in at.session_state.message]
      == ["改写过的问题", "回答1", "编辑后的追问", "回答1"],
      [m["content"] for m in at.session_state.message])

# --------------------------------------------------------------------------- #
# 6. 第三条分支（编辑后分支上的 AI 回复）也要能互切，且不能丢内容
# --------------------------------------------------------------------------- #
edit_buttons = [b for b in at.button if b.label == "编辑这条"]
at = edit_buttons[1].click().run()
boxes = [t for t in at.text_area if "编辑这条消息" in (t.label or "")]
check("能编辑 AI 回复", len(boxes) == 1, [t.label for t in at.text_area])
boxes[0].set_value("改写的回答")
at = [b for b in at.button if b.label == "保存并重新生成"][0].click().run()
third = at.session_state.get("current_branch")
check("编辑 AI 回复也新建了分支", third not in (None, "main", branch_after_edit), third)
# 编辑是**嵌套分叉**（见 branches.fork_point_for）：在 b2 上改，新分支从 b2 分出来，
# 所以这一组是 {b2, 新分支} 而不是把 main 也拉进来。main 仍然可达：先切到 b2，
# 再从 b2 切到 main（每一步的箭头都在）。
check("新分支与它的来源分支互切（2/2）",
      any("2/2" in (c.value or "") for c in at.get("caption")),
      [c.value for c in at.get("caption") if "分支" in (c.value or "")])
meta_now = json.loads(_harness.session_meta_path(sid).read_text(encoding="utf-8"))
third_rec = next(b for b in meta_now["branches"] if b["id"] == third)
edit_rec = next(b for b in meta_now["branches"] if b["id"] == branch_after_edit)
check("这一组里没有 main（编辑是嵌套分叉：新分支的分叉点是来源分支）",
      str(third_rec.get("fork_point")) == branch_after_edit
      and str(edit_rec.get("fork_point")) == "main",
      (third_rec.get("fork_point"), edit_rec.get("fork_point")))
check("切到来源分支后箭头指向 main（回得去）",
      at.session_state.get("current_branch") == third,
      at.session_state.get("current_branch"))
# 从新分支切回来源分支，再从来源分支切回 main
at = [b for b in at.button if (b.key or "").startswith("branch_prev_")][0].click().run()
check("切回了来源分支 b2", at.session_state.get("current_branch") == branch_after_edit,
      at.session_state.get("current_branch"))
check("在 b2 上分叉点箭头仍在（2/2）",
      sorted(arrows()) == [f"branch_next_{branch_after_edit}_0",
                           f"branch_prev_{branch_after_edit}_0"], arrows())
# b2 的组是 [main, b2]，b2 排在第 2 位 → › 回到 main（箭头 key 里是**当前**分支名）
to_main = [b for b in at.button
           if (b.key or "") == f"branch_next_{branch_after_edit}_0"]
check("在 b2 上有回到 main 的箭头", bool(to_main), arrows())
if to_main:
    check("箭头提示指向 main", "main" in (to_main[0].help or ""), to_main[0].help)
    at = to_main[0].click().run()
    check("最终切回了 main", at.session_state.get("current_branch") == "main",
          at.session_state.get("current_branch"))
    check("main 内容完好",
          [m["content"] for m in at.session_state.message]
          == ["第一个问题", "回答1", "第二个问题", "回答1"],
          [m["content"] for m in at.session_state.message])

on_disk = {}
for f in sorted(root.glob("*.json")):
    payload = json.loads(f.read_text(encoding="utf-8"))
    on_disk[f.stem] = [m["content"] for m in payload.get("message", [])]
check("三个版本都各自留在磁盘上",
      on_disk.get("main") == ["第一个问题", "回答1", "第二个问题", "回答1"]
      and on_disk.get(branch_after_edit)
      == ["改写过的问题", "回答1", "编辑后的追问", "回答1"]
      and on_disk.get(third) == ["改写过的问题", "改写的回答"],
      on_disk)

print()
if failures:
    print("失败:", ", ".join(failures))
    sys.exit(1)
print("test_edit_flow 全部通过")
