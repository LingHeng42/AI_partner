"""真实端到端登录：用真库 + 真密码哈希跑一遍完整的登录门控。

为什么值得单独跑：登录门控牵扯三方（streamlit-authenticator 的表单/cookie、
Streamlit 的 form 语义、我们自己的两道门），单元测试替换掉 auth 之后就看不出
"这个组合到底能不能跑通"。这里用真凭据（bcrypt 哈希）跑，覆盖：

  1. 未登录：渲染登录表单，且**不进入主界面**（主界面控件一个都不许出现）
  2. 已登录（模拟 cookie 校验通过后的那次重跑）：**登录表单不许再渲染**
     —— 这是"登录界面卡一下才消失"的修复点
  3. 已登录时能正常进入主界面

凭据通过环境变量注入（auth.credentials() 支持 JSON 字符串），不经过 secrets 文件。

用法：python tests/test_login_e2e.py
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
os.environ["DEEPSEEK_API_KEY"] = "sk-test-not-used"   # 越过 Key 门，专测登录门

import bcrypt  # noqa: E402

PASSWORD = "correct-horse-battery"
CREDS = {
    "usernames": {
        "lingheng42": {
            "email": "Lingheng42@outlook.com",
            "name": "lingheng42",
            "password": bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode(),
        }
    }
}
# 打开登录门控（harness 默认把它关掉，避免界面类测试停在登录页）
os.environ.pop("AI_PARTNER_DISABLE_AUTH", None)
os.environ["AUTH_CREDENTIALS"] = json.dumps(CREDS, ensure_ascii=False)
os.environ["COOKIE_KEY"] = "test-cookie-key-for-e2e-only"

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


FAKE = _harness.TMP_DIR / "_login_e2e"
FAKE.mkdir(parents=True, exist_ok=True)
entry = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")


def run_app(prelude: str = ""):
    wrapper = FAKE / "_app.py"
    wrapper.write_text(
        "import sys, os\n"
        f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
        "import streamlit as st\n"
        + prelude
        + "\n"
        + entry,
        encoding="utf-8",
    )
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(wrapper), default_timeout=90).run()


# 先确认门控真的开了（否则下面的断言都是假绿）
from lingheng import auth  # noqa: E402

check("测试环境里登录门控已打开", auth.auth_enabled(), auth.auth_enabled())
check("凭据解析成功（用户名 lingheng42 在表里）",
      "lingheng42" in (auth.credentials().get("usernames") or {}),
      list((auth.credentials().get("usernames") or {}).keys()))

print("\n=== 1. 未登录：应当停在登录页 ===")
at = run_app()
check("未登录时页面无异常", not at.exception, [e.message[:200] for e in at.exception])
check("渲染出了登录表单（有 Password 输入框）",
      any("password" in (t.label or "").lower() for t in at.text_input),
      [(t.label, t.type) for t in at.text_input])
check("未登录时**没有**进入主界面（看不到新建会话按钮）",
      not any(b.label == "新建会话" for b in at.button), [b.label for b in at.button])
check("未登录时也没有会话历史",
      not any(s.value == "会话历史" for s in at.subheader), [s.value for s in at.subheader])

print("\n=== 2. 已登录：登录表单不许再渲染（本次修复点）===")
at2 = run_app(
    "st.session_state['authentication_status'] = True\n"
    "st.session_state['username'] = 'lingheng42'\n"
    "st.session_state['name'] = 'lingheng42'\n"
    "st.session_state['user_api_key'] = 'sk-user-supplied'\n"
)
check("已登录时页面无异常", not at2.exception, [e.message[:200] for e in at2.exception])

passwords = [t for t in at2.text_input if "password" in (t.label or "").lower()]
check("**已登录时不再渲染登录表单**（没有登录用的密码框）", len(passwords) == 0,
      [(t.label, t.type) for t in at2.text_input])
check("已登录时确实进入了主界面（有会话历史）",
      any(s.value == "会话历史" for s in at2.subheader), [s.value for s in at2.subheader])
check("已登录时能看到新建会话按钮",
      any(b.label == "新建会话" for b in at2.button), [b.label for b in at2.button])
check("已登录时页面不是空白", len(at2.main) >= 1, len(at2.main))

print(f"\n  元素数量对比：未登录 main={len(at.main)}  已登录 main={len(at2.main)}")
check("已登录时渲染的东西不比未登录多（说明表单没被重画）",
      len(at2.main) <= len(at.main), (len(at.main), len(at2.main)))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
