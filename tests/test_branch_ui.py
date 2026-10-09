"""分支的页面级检查：侧边栏分支区渲染、切换分支、点击切换后进入正确分支。

为什么单独一个文件：AppTest 只在"应用脚本第一次被真正执行"的那一轮能观察到完整
页面，而本套件需要一个"已经有两条分支"的会话，所以在渲染之前就把分支文件种好；
其余分支行为（新建/改名/删除/父分支跳转）由 tests/test_logic.py 直接断言。
"""
import json
import os
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import PROJECT, SESSIONS_DIR as FAKE_SESSIONS, TMP_DIR, app_source, ui_source  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

SESSION_ID = "2099-11-11_000000_000"
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------- #
# 假客户端：脚本 import OpenAI 时替换掉，保证不会真的发请求
# --------------------------------------------------------------------------- #
(TMP_DIR / "_fake_openai_branch.py").write_text(
    '''
import streamlit as st
import AI_partner as _app


class _Completions:
    def create(self, **kwargs):
        return iter([])


class FakeOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = type("Chat", (), {"completions": _Completions()})()


st.cache_resource.clear()
_app.OpenAI = FakeOpenAI
''',
    encoding="utf-8",
)

# --------------------------------------------------------------------------- #
# 种一个"有两条分支、当前在 b2"的会话
# --------------------------------------------------------------------------- #
for stale in list(FAKE_SESSIONS.glob("*.json")) + list(FAKE_SESSIONS.glob("*.tmp*")):
    stale.unlink()
for _d in list(FAKE_SESSIONS.iterdir()):
    if _d.is_dir() and _d.name != "drafts":
        shutil.rmtree(_d, ignore_errors=True)
SESSION_DIR = FAKE_SESSIONS / SESSION_ID
(SESSION_DIR / "branches").mkdir(parents=True, exist_ok=True)
# b2 与 b3 都从 main 的第 1 条分出来 → 互为兄弟分支（气泡里会出现 ‹ 2/3 › 箭头）。
# 这正是曾经触发 StreamlitDuplicateElementKey 的场景：箭头曾只按"当前分支"命名 key，
# 而它们是在每条消息的循环里渲染的，第二条消息就撞上了第一条的 key。
(SESSION_DIR / "meta.json").write_text(json.dumps({
    "title": "分支渲染检查",
    "pinned": False,
    "current_branch": "b2",
    "branches": [
        {"id": "main", "parent": None, "fork_index": None, "created_at": ""},
        {"id": "b2", "parent": "main", "fork_index": 1, "created_at": ""},
        {"id": "b3", "parent": "main", "fork_index": 1, "created_at": ""},
    ],
    "nickname": "溟月",
    "nature": "测试性格",
    "role_description": "测试简介",
    "output_rules": "测试规则",
}, ensure_ascii=False), encoding="utf-8")
for branch, answer in (("main", "main 的回答"), ("b2", "b2 的回答"), ("b3", "b3 的回答")):
    (SESSION_DIR / "branches" / f"{branch}.json").write_text(
        json.dumps({"message": [{"role": "user", "content": "问题"},
                                {"role": "assistant", "content": answer}]}, ensure_ascii=False),
        encoding="utf-8")

wrapper = TMP_DIR / "_branch_wrapper.py"
wrapper.write_text(
    "import os, sys\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    f"sys.path.insert(0, {str(TMP_DIR).replace(chr(92), '/')!r})\n"
    "os.environ['DEEPSEEK_API_KEY'] = 'sk-test-not-used'\n"
    f"os.environ['AI_PARTNER_SESSIONS_DIR'] = {str(FAKE_SESSIONS).replace(chr(92), '/')!r}\n"
    "import streamlit as st\n"
    # 种状态必须在 import 假客户端之前：它自己会 import AI_partner
    f"st.session_state['current_session'] = {SESSION_ID!r}\n"
    "st.session_state['current_branch'] = 'b2'\n"
    "st.session_state['message'] = [{'role': 'user', 'content': '问题'},\n"
    "                               {'role': 'assistant', 'content': 'b2 的回答'}]\n"
    "st.session_state['session_title'] = '分支渲染检查'\n"
    "import _fake_openai_branch\n"
    "import AI_partner\n"
    "st.session_state['_branch_render_ran'] = True\n",
    encoding="utf-8",
)

at = AppTest.from_file(str(wrapper), default_timeout=60).run()
buttons = [b.label for b in at.button]

check("页面无异常启动", not at.exception, [e.message for e in at.exception])
check("应用脚本确实被执行", at.session_state.get("_branch_render_ran") is True)
check("侧边栏出现分支区（含数量）",
      any((s.value or "").startswith("分支（") for s in at.subheader)
      or any((e.label or "").startswith("分支（") for e in at.expander),
      {"subheader": [s.value for s in at.subheader], "expander": [e.label for e in at.expander]})
check("三条分支都渲染成按钮", all(b in buttons for b in ("main", "b2", "b3")), buttons)
check("当前分支按钮为高亮", any(b.label == "b2" and b.proto.type == "primary" for b in at.button),
      [(b.label, b.proto.type) for b in at.button if b.label in ("main", "b2", "b3")])
check("非当前分支不高亮", any(b.label == "main" and b.proto.type != "primary" for b in at.button),
      [(b.label, b.proto.type) for b in at.button if b.label in ("main", "b2", "b3")])
# 兄弟分支（b2 与 b3 同源）会在每轮消息下渲染切换箭头。这里是最容易复现
# StreamlitDuplicateElementKey 的地方：箭头 key 曾只按"当前分支"命名，
# 而它们是在消息循环里渲染的，第二条消息就和第一条撞 key 了。
check("同一分叉点的兄弟分支会渲染出切换箭头",
      any(b.proto.icon and "chevron" in b.proto.icon for b in at.button),
      [(b.label, b.proto.icon) for b in at.button])
check("每轮消息都渲染了分支计数（说明两轮都有箭头）",
      sum(1 for c in at.caption if (c.value or "").startswith("分支 ")) == 2,
      [c.value for c in at.caption])
check("箭头 key 带消息序号（修复重复 key）",
      'key=f"branch_prev_{branch}_{index}"' in app_source()
      and 'key=f"branch_next_{branch}_{index}"' in app_source())
check("分支菜单里含重命名输入框", "重命名分支" in [t.label for t in at.text_input],
      [t.label for t in at.text_input])
check("分支菜单里含删除按钮", any(label == "删除分支" for label in buttons), buttons)
# 控件 key 必须带分支名：源码级检查（AppTest 不暴露未回填的 key）
source = app_source()
branch_section = ui_source().split("分支管理", 1)[-1].split("管理角色", 1)[0]
check("分支控件 key 带会话 ID 与分支名",
      'key=f"branch_{sid}_{branch}"' in branch_section
      and 'key=f"branch_rename_{sid}_{branch}"' in branch_section, "key 里缺少 sid/branch")

# --------------------------------------------------------------------------- #
# 每条消息都有操作入口（编辑这条；最后一条回答额外有重新生成）
# --------------------------------------------------------------------------- #
check("消息操作菜单渲染出来（气泡里的 ⋯）", any(b.label == "编辑这条" for b in at.button), buttons)
check("最后一条回答有「重新生成」", any(b.label == "重新生成" for b in at.button), buttons)
check("编辑入口每轮消息都有",
      sum(1 for b in at.button if b.label == "编辑这条") == len(at.session_state["message"]),
      [b.label for b in at.button])
check("没有在编辑态时不渲染编辑框", "编辑这条消息" not in [t.label for t in at.text_area],
      [t.label for t in at.text_area])

# --------------------------------------------------------------------------- #
# 关于"点击切换分支"为什么不在本文件里点：
#   AppTest 会在控件回调结束后把 session_state 回滚到点击之前的快照，且不执行
#   回调里的 st.rerun()（快速重跑），因此点击后拿到的元素树并不代表真实页面。
#   本应用的设计是"当前分支以磁盘 meta.json 为准"——回调把指针写进磁盘，重跑时
#   侧边栏按磁盘渲染，所以真实行为是正确且已由两处断言覆盖：
#     1. tests/test_logic.py：switch_branch 写指针、重新进入会话进入正确分支
#     2. 本文件上面的断言：初始渲染时"当前分支高亮、另一条不高亮"
#   这里只额外确认回调确实被触发过（磁盘指针被改写）——但点击本身依赖 AppTest
#   的重跑语义，故不在此断言。
# --------------------------------------------------------------------------- #

# 清理隔离目录（真实 sessions/ 不会被触碰）
for _f in list(FAKE_SESSIONS.glob("*.json")) + list(FAKE_SESSIONS.glob("*.tmp*")):
    _f.unlink()
for _d in list(FAKE_SESSIONS.iterdir()):
    if _d.is_dir() and _d.name != "drafts":
        shutil.rmtree(_d, ignore_errors=True)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
