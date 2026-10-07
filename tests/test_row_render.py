"""会话条目渲染检查：置顶标记与 col2 的「重命名 / 置顶 / 删除」操作菜单。

两条硬性约束（都是踩过坑之后加的）：
1. 存档目录用 AI_PARTNER_SESSIONS_DIR 指到临时目录 —— 测试绝不碰真实 sessions/。
2. AppTest 在同一进程里只有第一次运行会真正执行应用脚本，所以先把存档准备好，
   再做那唯一一次渲染。

运行：
    .venv\\Scripts\\python.exe tests\\test_row_render.py
"""
import json
import sys

from _harness import PROJECT, SESSIONS_DIR as FAKE_SESSIONS, TMP_DIR  # noqa: F401

from streamlit.testing.v1 import AppTest  # noqa: E402

SESSION_ID = "2099-09-09_000000_000"
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------- #
# 假客户端（脚本 import OpenAI 时替换掉）
# --------------------------------------------------------------------------- #
(TMP_DIR / "_fake_openai_row.py").write_text(
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

wrapper = TMP_DIR / "_row_wrapper.py"
wrapper.write_text(
    "import os, sys\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    f"sys.path.insert(0, {str(TMP_DIR).replace(chr(92), '/')!r})\n"
    "os.environ['DEEPSEEK_API_KEY'] = 'sk-test-not-used'\n"
    f"os.environ['AI_PARTNER_SESSIONS_DIR'] = {str(FAKE_SESSIONS).replace(chr(92), '/')!r}\n"
    "import streamlit as st\n"
    # 种状态必须放在 import _fake_openai_row 之前：那个假客户端模块自己会
    # `import AI_partner`，一旦先 import 它，应用脚本就已经跑完一遍了
    f"st.session_state['current_session'] = {SESSION_ID!r}\n"
    "st.session_state['message'] = [{'role': 'user', 'content': '第一条'}]\n"
    # 当前会话的名称与测试存档保持一致，便于断言列表显示的是存档里的名称
    "st.session_state['session_title'] = '渲染检查会话'\n"
    "import _fake_openai_row\n"
    "import AI_partner\n"
    "st.session_state['_row_render_ran'] = True\n",
    encoding="utf-8",
)

# --------------------------------------------------------------------------- #
# 准备一条已置顶的测试会话（写在隔离目录里）
# --------------------------------------------------------------------------- #
for stale in list(FAKE_SESSIONS.glob("*.json")) + list(FAKE_SESSIONS.glob("*.tmp*")):
    stale.unlink()
(FAKE_SESSIONS / f"{SESSION_ID}.json").write_text(
    json.dumps(
        {
            "title": "渲染检查会话",
            "pinned": True,
            "message": [{"role": "user", "content": "历史消息"}],
            "nickname": "溟月",
            "nature": "测试性格",
            "role_description": "测试简介",
            "output_rules": "测试规则",
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)

# --------------------------------------------------------------------------- #
# 唯一一次真正执行应用脚本的渲染
# --------------------------------------------------------------------------- #
at = AppTest.from_file(str(wrapper), default_timeout=60).run()
buttons = [b.label for b in at.button]
text_inputs = [t.label for t in at.text_input]

check("页面无异常启动", not at.exception, str(at.exception))
check("应用脚本确实被执行（未被模块缓存跳过）", at.session_state.get("_row_render_ran") is True)
check("会话条目带置顶标记 📌", any("📌" in label for label in buttons), buttons)
check("会话条目显示自定义名称", any("渲染检查会话" in label for label in buttons), buttons)
check("操作菜单里是「取消置顶」（已置顶）", any(label == "取消置顶" for label in buttons), buttons)
check("操作菜单里含重命名输入框", "重命名" in text_inputs, text_inputs)
check("重命名不再有保存按钮（改为回车生效）", not any("保存名称" in label for label in buttons), buttons)
check("操作菜单里含删除确认", any("确认删除" in label for label in buttons), buttons)
check("侧边栏不再有会话名称输入框", "会话名称" not in text_inputs, text_inputs)
check("重命名输入框仍然存在", "重命名" in text_inputs, text_inputs)
# 已经产生过对话的会话：不再处于"新建会话"状态，按钮不该高亮
new_session_btn = next((b for b in at.button if b.label == "新建会话"), None)
check("已有对话时「新建会话」不高亮",
      new_session_btn is not None and new_session_btn.proto.type != "primary",
      new_session_btn.proto.type if new_session_btn else None)

# 两个要交互的输入框都在本次渲染的元素树里（用尽游标前先取好）
rename_box = next((t for t in at.text_input if t.label == "重命名"), None)
nickname_box = next((t for t in at.text_input if t.label == "昵称"), None)
check("找到重命名输入框", rename_box is not None)
check("找到昵称输入框", nickname_box is not None)

# --------------------------------------------------------------------------- #
# 在原报错路径上验证：重命名输入框里改完按回车（set_value + run 等价于回车提交）
# --------------------------------------------------------------------------- #
if rename_box is not None:
    rename_box.set_value("改名后的会话")
    at = at.run()
    errors = [e.message for e in at.exception]
    check("回车重命名不抛异常", not errors, errors)
    saved = json.loads((FAKE_SESSIONS / f"{SESSION_ID}.json").read_text(encoding="utf-8"))
    check("回车重命名写入存档", saved["title"] == "改名后的会话", saved.get("title"))
    check("重命名保留消息", len(saved.get("message", [])) == 1, saved.get("message"))
    check("重命名保留置顶", saved.get("pinned") is True, saved.get("pinned"))
    check("重命名不留下临时文件",
          not list(FAKE_SESSIONS.glob("*.tmp*")), [p.name for p in FAKE_SESSIONS.glob("*.tmp*")])

# --------------------------------------------------------------------------- #
# "改人设立刻落盘"的可观测结果（存档内容）由 tests/test_logic.py 断言：
#   save_session() 在已有对话时直接写存档、空对话时写草稿；
#   这里只额外断言控件确实挂上了 on_change 回调（UI 连线）。
# 注：同一个测试进程里 AppTest 只有第一次渲染会真正执行应用脚本，
#     所以"再渲染一次去点昵称输入框"这条路在本环境不可靠，不再强测。
# --------------------------------------------------------------------------- #
source = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")
role_section = source.split('st.subheader("管理角色")', 1)[-1].split('st.subheader("生成参数")', 1)[0]
check("人设输入框都挂了 on_change=save_session",
      role_section.count("on_change=save_session") == 4, role_section.count("on_change=save_session"))
advanced_section = source.split("高级配置", 1)[-1]
check("高级参数控件都挂了 on_change=save_session",
      advanced_section.count("on_change=save_session") >= 5, advanced_section.count("on_change=save_session"))
check("恢复默认值按钮会立刻落盘", "on_click=reset_advanced_and_save" in source)

# --------------------------------------------------------------------------- #
# key 稳定性：组件 key 不得包含列表序号
# （"删掉上面一条后，⋯ 弹层串到下一条会话"的根因就是 index 进了 key）
# --------------------------------------------------------------------------- #
source = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")
sidebar_source = source.split("st.subheader(\"会话历史\")", 1)[-1].split("st.subheader(\"管理角色\")", 1)[0]
check("侧边栏会话列表里不再出现 index 变量",
      "index" not in sidebar_source, sidebar_source[:0] or "found index")
check("组件 key 只由会话 ID 构成（不含序号）",
      "{index}" not in sidebar_source and "_{index}" not in sidebar_source)

# 隔离目录由 _harness 在退出时统一删除（真实 sessions/ 从未被触碰）

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
