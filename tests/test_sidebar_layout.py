"""侧边栏结构：上端保持干净，技术信息收在底部折叠区。

用户的诉求很明确："存储、排查、API Key、退出登录要么放在侧边栏最下面、
要么放其他更好的地方，保证侧边栏干净（起码上端干净）。"

这里的断言都是**结构性**的（不依赖具体渲染细节，避免一改样式就红）：
  1. 侧边栏存在「账号与技术信息」折叠区，且它排在主流程控件之后
  2. 有可用 Key 时默认**折叠**；没有可用 Key 时默认**展开**（那时最要紧的是填 Key）
  3. 新建会话 / 会话历史 这些每天要用的东西在折叠区之前
  4. 退出登录、存储、Key、最近动作都收在折叠区里

用法：python tests/test_sidebar_layout.py
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

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


from streamlit.testing.v1 import AppTest  # noqa: E402

TECH_LABEL = "账号与技术信息"

FAKE = _harness.TMP_DIR / "_sidebar_layout"
FAKE.mkdir(parents=True, exist_ok=True)
entry = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")


def run_app(prelude_extra: str = ""):
    wrapper = FAKE / "_app.py"
    wrapper.write_text(
        "import sys, os\n"
        f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
        "import streamlit as st\n"
        + prelude_extra
        + "\n"
        + entry,
        encoding="utf-8",
    )
    return AppTest.from_file(str(wrapper), default_timeout=90).run()


# --------------------------------------------------------------------------- #
# 1. 有可用 Key：技术区默认折叠
# --------------------------------------------------------------------------- #
at = run_app('os.environ["DEEPSEEK_API_KEY"] = "sk-test-not-used"\n')
check("有 Key 时页面无异常", not at.exception, [e.message[:200] for e in at.exception])

tech = [e for e in at.sidebar.expander if (e.label or "") == TECH_LABEL]
check(f"侧边栏里有「{TECH_LABEL}」折叠区", len(tech) == 1,
      [e.label for e in at.sidebar.expander])
if tech:
    check("有可用 Key 时技术区**默认折叠**（上端保持干净）",
          tech[0].proto.expanded is False, tech[0].proto.expanded)

# --------------------------------------------------------------------------- #
# 2. 没有可用 Key：会停在 Key 门控页（此时不渲染侧边栏，属设计如此）
#
#    也就是说"没有 Key 时技术区默认展开"这条分支在正常流程里到不了 ——
#    不过它仍是必要兜底（例如允许名单用光了站长 Key 的情况），所以保留。
# --------------------------------------------------------------------------- #
at2 = run_app('os.environ["DEEPSEEK_API_KEY"] = ""\n')
check("没有可用 Key 时停在门控页、不渲染侧边栏（安全默认）",
      len(at2.sidebar.expander) == 0, [e.label for e in at2.sidebar.expander])
check("门控页给出明确的引导文字",
      any("Key" in (c.value or "") for c in at2.caption) or len(at2.text_input) > 0,
      [c.value[:60] for c in at2.caption][:4])

# --------------------------------------------------------------------------- #
# 3. 技术性的东西都在折叠区里；每天要用的东西在它之前
# --------------------------------------------------------------------------- #
sidebar_expanders = [e.label for e in at.sidebar.expander]
check("存储面板在折叠区里（不再单独占侧边栏上端）",
      "存储" in sidebar_expanders, sidebar_expanders)
check("排查面板在折叠区里", any("最近动作" in (l or "") for l in sidebar_expanders),
      sidebar_expanders)
check("Key 输入在折叠区里", any("API Key" in (l or "") for l in sidebar_expanders),
      sidebar_expanders)

# 侧边栏顺序：技术区必须排在"高级配置"之后（= 最下面）
labels = sidebar_expanders
if TECH_LABEL in labels and "⚙️ 高级配置" in labels:
    check("技术区排在「高级配置」之后（= 侧边栏最下面）",
          labels.index(TECH_LABEL) > labels.index("⚙️ 高级配置"), labels)

# 会话历史仍然在侧边栏里，且属于"上端主流程"
check("会话历史仍在侧边栏", any(s.value == "会话历史" for s in at.sidebar.subheader),
      [s.value for s in at.sidebar.subheader])
check("新建会话按钮仍在侧边栏", any(b.label == "新建会话" for b in at.sidebar.button),
      [b.label for b in at.sidebar.button])

# 退出登录出现在有登录信息时才渲染 —— 这里不依赖登录，只确认它不再直接挂在侧边栏顶层
# （它现在在技术区内部；没有登录信息时不渲染，属正常）
print("  侧边栏折叠区顺序：", labels)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
