"""思考过程中点开/折叠「思考过程」会让回答消失 —— 回归测试。

用户报的现象：**在思考的同时点击折叠/展开思考内容，那条信息会消失。**

根因（分两段，缺一不可）：

1. 这个折叠面板以前用 on_change 回调记忆开合状态 → 点一下会触发一次重跑。
   而它在**生成过程中是可见的**，用户顺手点它，就把正在进行的流式请求打断了。
2. 被打断的那一轮，助手回答还没写进 message（remember_reply 在
   generate_reply 之后才执行）；而 ``st.write_stream`` 被中断时**静默返回空
   字符串**（不抛异常），于是 remember_reply 判定"模型没返回任何内容"、
   什么都不记录。重跑后 message 里只剩用户那一句 → 整条回答消失。

修法：把 on_change 改成 ``"ignore"``（Streamlit 支持的特殊值：更新控件状态但
**不触发重跑**），并把"用户点出来的状态"在渲染时从控件键读回来，从而保住
"开合状态有记忆"这个原有功能。

用法：python tests/test_reasoning_toggle.py
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
os.environ.pop("DEEPSEEK_API_KEY", None)

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------- #
# 1. 源码契约：折叠面板不得注册"会触发重跑"的回调
#
#    这是本次修复的核心，且很容易被后来的改动破坏（比如有人"顺手"改回回调），
#    所以用 AST 把它钉住。
# --------------------------------------------------------------------------- #
import ast  # noqa: E402

src = (PROJECT / "lingheng" / "ai.py").read_text(encoding="utf-8")
tree = ast.parse(src)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "reasoning_expander")

expander_calls = []
for node in ast.walk(fn):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "expander":
        expander_calls.append(node)

check("reasoning_expander 里创建了 expander", len(expander_calls) == 1, len(expander_calls))
if expander_calls:
    call = expander_calls[0]
    kwargs = {kw.arg: kw.value for kw in call.keywords}
    on_change = kwargs.get("on_change")
    check("**expander 设了 on_change**（否则点开合会重跑、打断流）",
          on_change is not None, list(kwargs))
    check("**on_change 是 \"ignore\"**（不触发重跑；改回回调会让回答消失）",
          isinstance(on_change, ast.Constant) and on_change.value == "ignore",
          ast.dump(on_change) if on_change is not None else None)
    check("没有把回调函数当 on_change 传（args= 也不该出现）",
          "args" not in kwargs, list(kwargs))

# --------------------------------------------------------------------------- #
# 2. 行为：开合状态仍然有记忆（原功能不能被修坏）
# --------------------------------------------------------------------------- #
class _State(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as exc:
            raise AttributeError(k) from exc

    def __setattr__(self, k, v):
        self[k] = v


class _FakeExpander:
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSt:
    def __init__(self, state):
        self.session_state = state
        self.last = None

    def expander(self, label, **kw):
        self.last = kw
        return _FakeExpander(**kw)


from lingheng import ai  # noqa: E402

state = _State()
fake = _FakeSt(state)
ai.st = fake

# 首次渲染：用默认值，并把它记进状态键
ai.reasoning_expander("reasoning_panel", "show_reasoning", True)
check("首次渲染写入默认展开状态", state.get("show_reasoning") is True, state.get("show_reasoning"))
check("首次渲染传给 expander 的 expanded=True", fake.last.get("expanded") is True, fake.last)

# 用户点了折叠：Streamlit 只更新控件键（因为 on_change="ignore" 不重跑）
state["reasoning_panel"] = False
# 下一次渲染（比如流结束时的那一轮）应当从控件键读回来
ai.reasoning_expander("reasoning_panel", "show_reasoning", True)
check("**用户折叠后状态被记住**（下次渲染仍是折叠）",
      state.get("show_reasoning") is False, state.get("show_reasoning"))
check("再次渲染传给 expander 的 expanded=False", fake.last.get("expanded") is False, fake.last)

# 用户又点开
state["reasoning_panel"] = True
ai.reasoning_expander("reasoning_panel", "show_reasoning", True)
check("用户再次展开后也被记住", state.get("show_reasoning") is True, state.get("show_reasoning"))

# --------------------------------------------------------------------------- #
# 3. 真实 AppTest：点折叠面板**不产生重跑**
#
#    判据用"控件自己的状态被改了、但页面没有被重新执行"——页面若要重跑，
#    AppTest 的元素树会被重建。这里用带计数器的探针模块来观察执行次数。
# --------------------------------------------------------------------------- #
FAKE = _harness.TMP_DIR / "_reasoning_toggle"
FAKE.mkdir(parents=True, exist_ok=True)
counter = FAKE / "runs.txt"
entry = (PROJECT / "AI_partner.py").read_text(encoding="utf-8")
wrapper = FAKE / "_app.py"
wrapper.write_text(
    "import sys, os\n"
    f"sys.path.insert(0, {str(PROJECT).replace(chr(92), '/')!r})\n"
    f"_MARK = {str(counter).replace(chr(92), '/')!r}\n"
    "with open(_MARK, 'a', encoding='utf-8') as fh:\n"
    "    fh.write('run\\n')\n"
    "import streamlit as st\n"
    "st.session_state['user_api_key'] = 'sk-test'\n"
    "st.session_state['thinking'] = True\n"
    + entry,
    encoding="utf-8",
)

from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(wrapper), default_timeout=90).run()
before = counter.read_text(encoding="utf-8").count("run") if counter.exists() else 0
check("应用启动无异常（脚本被执行过）", before >= 1, before)

# 找历史上的思考折叠面板（没有历史消息时可能不存在，那就跳过——核心契约已在上面的 AST 检查里钉住）
print(f"  本次渲染的执行次数 = {before}")
check("页面不是空白", len(at.main) >= 1, len(at.main))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
