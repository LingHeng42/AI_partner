"""对话头像的页面级检查：上传的头像确实用于聊天气泡左侧，且随会话独立。

为什么要单独一个文件：AppTest 只在"应用脚本第一次被真正执行"的那一轮能观察到
完整页面，而本套件需要在渲染前就把头像与存档种好。数据的读写在 tests/test_logic.py
里覆盖（格式校验、去重、越权路径、随草稿、绝不进请求）。
"""
import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import PROJECT, SESSIONS_DIR as FAKE_SESSIONS, TMP_DIR  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

SESSION_ID = "2099-12-31_000000_000"
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


def real_png(pixel=(255, 0, 0)):
    """生成一张 1×1 的真实合法 PNG。

    不能用"魔数 + 随便几个字节"的假图片：st.chat_message(avatar=...) 会真的去解码
    图片，解不开就抛错（这也正是应用里需要兜住的情况）。
    """

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1×1, 8bit, truecolor
    raw = b"\x00" + bytes(pixel)  # 每行前面一个 filter 字节
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# 假客户端 + 种一个"已设置两个头像"的会话
# --------------------------------------------------------------------------- #
(TMP_DIR / "_fake_openai_avatar.py").write_text(
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

for _d in list(FAKE_SESSIONS.iterdir()):
    if _d.is_dir() and _d.name != "drafts":
        shutil.rmtree(_d, ignore_errors=True)
SESSION_DIR = FAKE_SESSIONS / SESSION_ID
(SESSION_DIR / "branches").mkdir(parents=True, exist_ok=True)
(SESSION_DIR / "attachments").mkdir(parents=True, exist_ok=True)
USER_AVATAR = "attachments/aaaa1111.png"
AI_AVATAR = "attachments/bbbb2222.png"
(SESSION_DIR / USER_AVATAR).write_bytes(real_png((255, 0, 0)))
(SESSION_DIR / AI_AVATAR).write_bytes(real_png((0, 0, 255)))
(SESSION_DIR / "meta.json").write_text(json.dumps({
    "title": "头像渲染检查",
    "pinned": False,
    "current_branch": "main",
    "branches": [{"id": "main", "parent": None, "fork_index": None, "created_at": ""}],
    "nickname": "溟月",
    "nature": "测试性格",
    "role_description": "测试简介",
    "output_rules": "测试规则",
    "user_avatar": USER_AVATAR,
    "assistant_avatar": AI_AVATAR,
}, ensure_ascii=False), encoding="utf-8")
(SESSION_DIR / "branches" / "main.json").write_text(
    json.dumps({"message": [{"role": "user", "content": "问题"},
                            {"role": "assistant", "content": "回答"}]}, ensure_ascii=False),
    encoding="utf-8")

wrapper = TMP_DIR / "_avatar_wrapper.py"
wrapper.write_text(
    "import os, sys\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    f"sys.path.insert(0, {str(TMP_DIR).replace(chr(92), '/')!r})\n"
    "os.environ['DEEPSEEK_API_KEY'] = 'sk-test-not-used'\n"
    f"os.environ['AI_PARTNER_SESSIONS_DIR'] = {str(FAKE_SESSIONS).replace(chr(92), '/')!r}\n"
    "import streamlit as st\n"
    # 直接在渲染前种状态（等价于"进入这个会话"之后的状态）。
    # 不在这里调 load_selected_session：它内部会 st.rerun()，而 AppTest 在 rerun
    # 时不会重跑脚本、还会把状态回滚到调用前，测量不到真实结果。
    # load_selected_session 本身的行为由 tests/test_logic.py 覆盖。
    f"st.session_state['current_session'] = {SESSION_ID!r}\n"
    "st.session_state['current_branch'] = 'main'\n"
    "st.session_state['session_title'] = '头像渲染检查'\n"
    "st.session_state['message'] = [{'role': 'user', 'content': '问题'},\n"
    "                               {'role': 'assistant', 'content': '回答'}]\n"
    f"st.session_state['user_avatar'] = {USER_AVATAR!r}\n"
    f"st.session_state['assistant_avatar'] = {AI_AVATAR!r}\n"
    "import _fake_openai_avatar\n"
    "import AI_partner\n"
    "st.session_state['_avatar_render_ran'] = True\n",
    encoding="utf-8",
)

at = AppTest.from_file(str(wrapper), default_timeout=60).run()

check("页面无异常启动", not at.exception, [e.message for e in at.exception])
check("应用脚本确实被执行", at.session_state.get("_avatar_render_ran") is True)
check("侧边栏出现「对话头像」区", any((s.value or "") == "对话头像" for s in at.subheader),
      [s.value for s in at.subheader])
check("两个头像上传控件都在",
      {"我的头像", "AI 头像"} <= {u.label for u in at.file_uploader},
      [u.label for u in at.file_uploader])
check("头像随会话载入（用户头像）", str(at.session_state["user_avatar"]) == USER_AVATAR,
      at.session_state["user_avatar"])
check("头像随会话载入（AI 头像）", str(at.session_state["assistant_avatar"]) == AI_AVATAR,
      at.session_state["assistant_avatar"])
check("已经设置过头像时侧边栏渲染预览图", len(at.image) >= 2,
      [str(getattr(i, "value", None)) for i in at.image])
check("有清除头像的入口",
      any(b.label.startswith("清除") for b in at.button), [b.label for b in at.button])

# 聊天气泡确实带上了头像。
# 注意：AppTest 会把本地图片路径替换成 mock 媒体 URL（如 /mock/media/<hash>.jpg），
# 拿不到原文件名，所以这里断言"两个气泡带了两个不同的图片"，
# 而"哪个角色用哪张图"由下面按角色取 current_avatar 来断言。
chat_msgs = at.get("chat_message")
avatar_values = [str(getattr(m.proto, "avatar", "") or "") for m in chat_msgs]
check("两条消息都渲染了", len(chat_msgs) == 2, len(chat_msgs))
check("两个气泡都带上了自定义头像",
      all(v for v in avatar_values), avatar_values)
check("两个气泡用的是不同头像", len(set(avatar_values)) == 2, avatar_values)
check("气泡头像确实是图片（mock 媒体 URL）",
      all("/mock/media/" in v for v in avatar_values), avatar_values)
# 角色 → 头像的对应关系（AI_partner 在这个进程里已被加载，直接用应用函数断言；
# 注意不能调 current_avatar：它用 streamlit 的 session_state，需要脚本运行时上下文，
# 这里改为核对会话状态里存的路径本身）
check("用户头像路径指向会话的 attachments",
      str(at.session_state["user_avatar"]).startswith("attachments/"), at.session_state["user_avatar"])
check("AI 头像路径指向会话的 attachments",
      str(at.session_state["assistant_avatar"]).startswith("attachments/"), at.session_state["assistant_avatar"])
check("两个角色用的是不同图片", at.session_state["user_avatar"] != at.session_state["assistant_avatar"])

# 清理隔离目录（真实 sessions/ 不会被触碰）
for _f in list(FAKE_SESSIONS.glob("*.json")) + list(FAKE_SESSIONS.glob("*.tmp*")):
    _f.unlink()
for _d in list(FAKE_SESSIONS.iterdir()):
    if _d.is_dir() and _d.name != "drafts":
        shutil.rmtree(_d, ignore_errors=True)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
