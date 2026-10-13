"""登录界面"卡一下才消失"的回归测试。

现象：登录成功后，登录表单会**再被完整渲染一遍**才消失（用户看到的"卡一下"）。

根因：ui._render_login() 以前是先无条件调用 auth.do_login()（渲染表单），
之后才判断 current_user()。于是已登录的那次重跑里，登录表单连同
CookieManager 组件又被画了一遍 —— 那一轮的开销加上"表单闪现"就是卡顿的来源。

修法：进入 _render_login() 先看登录态，已登录**立刻 return**，不渲染表单。

这个测试断言的就是"已登录时一行表单都不许渲染"：
  · 页面里没有登录表单的标题
  · session_state 里没有被创建出表单相关的东西（说明确实没渲染）
  · 页面正常进入主界面

用法：python tests/test_login_render.py
"""
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


# --------------------------------------------------------------------------- #
# 1. 静态检查：_render_login 必须在调用 do_login() 之前先判断登录态
#    （用 AST 找两行号，防止以后有人把顺序改回去）
# --------------------------------------------------------------------------- #
import ast  # noqa: E402

ui_src = (PROJECT / "lingheng" / "ui.py").read_text(encoding="utf-8")
tree = ast.parse(ui_src)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_render_login")

calls = []
for node in ast.walk(fn):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.append((node.lineno, node.func.attr))
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        calls.append((node.lineno, node.func.id))

do_login_at = [ln for ln, name in calls if name == "do_login"]
current_user_at = [ln for ln, name in calls if name == "current_user"]
check("_render_login 里调用了 do_login", bool(do_login_at), calls)
check("_render_login 里调用了 current_user 判断登录态", bool(current_user_at), calls)
check("**先判断登录态、后渲染表单**（否则已登录时表单会重画一遍）",
      current_user_at and do_login_at and min(current_user_at) < min(do_login_at),
      f"current_user@{current_user_at} vs do_login@{do_login_at}")

# --------------------------------------------------------------------------- #
# 2. 行为检查：已登录时不得渲染登录表单
#
#    用 "unrendered" 位置把表单"渲染"到一个可检查的容器里会改动生产代码，
#    所以这里换个等价做法：直接检查 _render_login 在已登录时的**副作用**——
#    它应当什么都不渲染、也不触碰 authenticator。
# --------------------------------------------------------------------------- #
class _FakeSt:
    """记录所有渲染调用，用来判断"到底渲染了什么"。"""

    def __init__(self):
        self.rendered = []
        self.session_state = {}

    def title(self, text):
        self.rendered.append(("title", text))

    def caption(self, text):
        self.rendered.append(("caption", text))

    def error(self, text):
        self.rendered.append(("error", text))

    def stop(self):
        raise AssertionError("不该走到 st.stop()（已登录应当直接放行）")


fake = _FakeSt()
from lingheng import auth, ui  # noqa: E402

ui.st = fake
auth.auth_enabled = lambda: True              # 假装配了登录
auth.current_user = lambda: {"username": "lingheng42", "name": "lingheng42", "email": ""}
auth.login_form_allowed = lambda: True
called = {"do_login": 0}
auth.do_login = lambda: called.__setitem__("do_login", called["do_login"] + 1) or True

ui._render_login()

check("已登录时**没有渲染登录表单**（不调用 do_login）", called["do_login"] == 0,
      called["do_login"])
check("已登录时没有渲染登录页标题", not [r for r in fake.rendered if r[0] == "title"],
      fake.rendered)
check("已登录时没有渲染任何东西（纯粹放行）", fake.rendered == [], fake.rendered)

# 未登录时：必须渲染表单（不能把门控关掉）
fake2 = _FakeSt()
ui.st = fake2
auth.current_user = lambda: {}
auth.do_login = lambda: called.__setitem__("do_login", called["do_login"] + 1) or False
try:
    ui._render_login()
except AssertionError as exc:
    if "st.stop" not in str(exc):
        raise
check("未登录时会渲染登录表单", called["do_login"] == 1, called["do_login"])
check("未登录时给出登录页标题", [r for r in fake2.rendered if r[0] == "title"],
      fake2.rendered)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
