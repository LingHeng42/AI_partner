"""登录门控与"用谁的 key"的界面级测试。

用假的 ``streamlit_authenticator`` 模块驱动，不需要真的装它，也不需要 OAuth：
只要控制 `st.session_state['authentication_status']`，就能模拟"已登录/未登录"。

**为什么每个用例要单独起进程**：AppTest 的 session_state 在同一个进程里会跨
`run()` 残留（这个项目里已经踩过好几次），一个用例种下的 `user_api_key` 会漏到
下一个用例，断言就失去意义。所以这里按项目惯例用子进程隔离（见 run_tests.py）。

用法：
    python tests/test_auth_ui.py           # 依次跑三个用例（各自子进程）
    python tests/test_auth_ui.py <case>    # 只跑某个用例（内部使用）
"""
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CASES = ("anonymous", "userkey", "nokey")


# --------------------------------------------------------------------------- #
# 编排：依次在子进程里跑每个用例
# --------------------------------------------------------------------------- #
def run_all() -> int:
    failed = []
    for case in CASES:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), case],
                                cwd=str(TESTS_DIR.parent))
        if result.returncode != 0:
            failed.append(case)
    print()
    print("=" * 46)
    if failed:
        print("失败的用例:", ", ".join(failed))
        return 1
    print(f"登录取向用例全部通过：{', '.join(CASES)}")
    return 0


if len(sys.argv) == 1:
    sys.exit(run_all())

# --------------------------------------------------------------------------- #
# 以下只在子进程里执行：跑单个用例
# --------------------------------------------------------------------------- #
import os  # noqa: E402
import types  # noqa: E402

sys.path.insert(0, str(TESTS_DIR))
import _harness  # noqa: E402

PROJECT = _harness.PROJECT
from streamlit.testing.v1 import AppTest  # noqa: E402

CASE = sys.argv[1]
SESSION_ID = "2099-12-31_235959_000"
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


# 假的 streamlit_authenticator：login() 直接把当前用户设成 alice
FAKE_PKG = _harness.TMP_DIR / "_fake_pkgs"
(FAKE_PKG / "streamlit_authenticator").mkdir(parents=True, exist_ok=True)
(FAKE_PKG / "streamlit_authenticator" / "__init__.py").write_text(
    "import streamlit as st\n"
    "\n"
    "\n"
    "class Authenticate:\n"
    "    def __init__(self, credentials, cookie_name, cookie_key, expiry_days=30):\n"
    "        self.credentials = credentials\n"
    "\n"
    "    def login(self, location='main'):\n"
    "        if st.session_state.get('authentication_status') is None:\n"
    "            st.session_state['authentication_status'] = True\n"
    "            st.session_state['username'] = 'alice'\n"
    "            st.session_state['name'] = 'Alice'\n"
    "\n"
    "    def logout(self, location='sidebar'):\n"
    "        for key in ('authentication_status', 'username', 'name', 'email'):\n"
    "            st.session_state.pop(key, None)\n",
    encoding="utf-8",
)

CREDS = "{'usernames': {'alice': {'name': 'Alice', 'password': 'pw'}}}"

# 每个用例的前置：种状态 / 补丁身份与配置
PRELUDE = {
    "anonymous": (
        "import lingheng.auth as _auth\n"
        "st.session_state['authentication_status'] = None\n"
        f"_auth._credentials_raw = lambda: {CREDS}\n"
    ),
    "userkey": (
        "import lingheng.auth as _auth\n"
        "st.session_state['authentication_status'] = True\n"
        "st.session_state['username'] = 'alice'\n"
        "st.session_state['name'] = 'Alice'\n"
        "st.session_state['user_api_key'] = 'sk-test-user-key'\n"
        f"_auth._credentials_raw = lambda: {CREDS}\n"
    ),
    "nokey": (
        "import lingheng.auth as _auth\n"
        "import lingheng.config as _config\n"
        "st.session_state['authentication_status'] = True\n"
        "st.session_state['username'] = 'bob'\n"
        "st.session_state['name'] = 'Bob'\n"
        f"_auth._credentials_raw = lambda: {CREDS}\n"
        # bob 不在允许名单里；站长虽然配了 key，但也不能给他用（fail-closed）
        "_config.allowed_users = lambda: []\n"
        "_config.owner_api_key = lambda: 'sk-owner'\n"
    ),
}

wrapper = _harness.TMP_DIR / f"_auth_case_{CASE}.py"
wrapper.write_text(
    "import os, sys\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    f"sys.path.insert(0, {str(_harness.TMP_DIR).replace(chr(92), '/')!r})\n"
    f"sys.path.insert(0, {str(FAKE_PKG).replace(chr(92), '/')!r})\n"
    f"os.environ['AI_PARTNER_SESSIONS_DIR'] = {str(_harness.SESSIONS_DIR).replace(chr(92), '/')!r}\n"
    "import streamlit as st\n"
    + PRELUDE[CASE]
    + "import AI_partner\n",
    encoding="utf-8",
)

at = AppTest.from_file(str(wrapper), default_timeout=60).run()
titles = [t.value for t in at.title]
subheaders = [s.value for s in at.subheader]
captions = [c.value for c in at.caption]
infos = [i.value for i in at.info]

if CASE == "anonymous":
    check("未登录时渲染登录页标题", any("凌恒的酒馆" in t for t in titles), titles)
    check("未登录时不渲染聊天输入框", not at.chat_input)
    check("未登录时不渲染侧边栏（会话历史）", not [s for s in subheaders if "会话历史" in s], subheaders)
    check("未登录时没有异常", not at.exception, [e.message for e in at.exception])

elif CASE == "userkey":
    check("登录且有 key 时渲染侧边栏（会话历史）", any("会话历史" in s for s in subheaders), subheaders)
    check("显示已登录用户名", any("已登录" in c and "Alice" in c for c in captions), captions[:6])
    check("显示正在使用自己的 Key",
          any("你自己的 Key" in c for c in captions + infos), (captions + infos)[:6])
    check("没有出现'配置 API Key'引导页",
          not [t for t in titles if "配置 API Key" in t], titles)
    check("登录后没有异常", not at.exception, [e.message for e in at.exception])

elif CASE == "nokey":
    check("没 key 时渲染'配置 API Key'引导页", any("配置 API Key" in t for t in titles), titles)
    check("引导页说明了去哪填 key", any("API Key" in i for i in infos), infos)
    check("没 key 时不进聊天界面（不渲染会话历史）",
          not [s for s in subheaders if "会话历史" in s], subheaders)
    check("未授权的用户不会自动用上站长的 key（fail-closed）",
          not any("站长的 Key" in t for t in titles + captions + infos), (titles + captions + infos)[:6])
    check("没 key 时没有异常", not at.exception, [e.message for e in at.exception])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
