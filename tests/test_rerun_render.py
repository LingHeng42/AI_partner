"""验证"每次重跑都会重新渲染页面"（白屏 bug 的回归测试）。

原理：模拟 Streamlit 的重跑语义——执行入口脚本，然后清掉入口模块但不清理
已导入的 lingheng.*，再执行一次。如果页面主体还是模块顶层代码，第二次就什么
都不渲染（elements 数量为 0）；包成 main() 并由入口显式调用后，两次都应渲染。

用法：python tests/test_rerun_render.py
"""
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _harness  # noqa: E402  隔离存档目录与环境变量

PROJECT = _harness.PROJECT
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


class _Recorder(types.ModuleType):
    """记录所有被调用过的 streamlit 命令，用来数"这次重跑渲染了多少元素"。"""

    def __init__(self, name):
        super().__init__(name)
        self.calls = []

    def __getattr__(self, item):
        # 已经显式设置过的属性（session_state / sidebar / columns ...）优先
        try:
            return object.__getattribute__(self, item)
        except AttributeError:
            pass
        if item.startswith("_"):
            raise AttributeError(item)

        def call(*args, **kwargs):
            self.calls.append(item)
            return _Stub()
        return call


class _Stub:
    def __getattr__(self, item):
        return _Stub()

    def __call__(self, *args, **kwargs):
        return _Stub()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter((_Stub(), _Stub()))

    def setdefault(self, *args, **kwargs):
        return None


class _State(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self[key] = value


fake_st = _Recorder("streamlit")
fake_st.session_state = _State()
fake_st.cache_resource = lambda *a, **k: (a[0] if a else (lambda f: f))
fake_st.columns = lambda *a, **k: [_Stub(), _Stub()]
fake_st.chat_input = lambda *a, **k: None
fake_st.tabs = lambda *a, **k: [_Stub()]
fake_st.sidebar = _Stub()
sys.modules["streamlit"] = fake_st


def run_entry():
    """按 Streamlit 的语义"执行入口脚本"：清掉入口模块，但保留已导入的包。"""
    for name in list(sys.modules):
        if name == "AI_partner":
            del sys.modules[name]
    fake_st.calls.clear()
    import AI_partner  # noqa: F401  执行入口
    return list(fake_st.calls)


first = run_entry()
check("首次加载渲染了页面", len(first) > 5, f"{len(first)} 个 st 调用")

second = run_entry()
check("第二次重跑仍然渲染页面（不再是白屏）", len(second) > 5, f"{len(second)} 个 st 调用")

# 真正的不变量是"页面骨架每次都在"，而不是"元素数完全相等"：
# 侧边栏的 Key 状态区会随会话状态变化（用自己的 key / 用站长的 key 渲染不同），
# 所以数量比较是错的判据——只要骨架还在，就不是白屏。
SKELETON = ("set_page_config", "header", "divider", "subheader", "expander",
            "text_input", "text_area", "slider", "toggle", "file_uploader")
for name in SKELETON:
    check(f"第一次渲染有骨架 {name}", name in first)
    check(f"第二次重跑仍有骨架 {name}", name in second)

# 关键：页面主体不能留在模块顶层（否则 import 缓存会吃掉它）
ui_source = (_harness.APP_PACKAGE / "ui.py").read_text(encoding="utf-8")
check("ui.py 的页面主体包在 main() 里", "def main() -> None:" in ui_source)
import ast

ui_tree = ast.parse(ui_source)
top_calls = [
    n for n in ui_tree.body
    if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
    and getattr(n.value.func, "attr", "") not in ("", ) and isinstance(n.value.func, ast.Attribute)
]
check("ui.py 顶层没有直接调用 st.* 的语句", not top_calls,
      [f"line {n.lineno}" for n in top_calls])
entry_source = (_harness.PROJECT / "AI_partner.py").read_text(encoding="utf-8")
check("入口显式调用 ui.main()", '.main()' in entry_source, entry_source[-120:])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
