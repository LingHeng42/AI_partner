"""包结构与导入顺序（线上事故的防回归）。

**事故经过**：``lingheng/`` 目录起初没有 ``__init__.py``，于是成了 namespace
package。本地 Python 3.12 跑得好好的，但 Streamlit Community Cloud 用 **Python
3.14**，对"部分初始化的 namespace package + 循环导入"更严格，直接抛::

    KeyError: 'lingheng.config'
    AttributeError: partially initialized module 'lingheng.ui' ...
        has no attribute 'main' (most likely due to a circular import)

结果：**页面脚本根本没执行完**，用户看到的就是"切换/删除会话后一片空白"。
加了 ``lingheng/__init__.py``（正规包）并把入口改成按依赖顺序一次性导入之后解决。

用法：python tests/test_package_import.py
"""
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
import _harness  # noqa: E402

PROJECT = _harness.PROJECT
PACKAGE = PROJECT / "lingheng"
failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  -> " + str(extra) if extra else ""))
    if not cond:
        failures.append(name)


# --------------------------------------------------------------------------- #
# 1. 必须是"正规包"，不能是 namespace package
# --------------------------------------------------------------------------- #
check("lingheng/__init__.py 存在（否则会退化成 namespace package）",
      (PACKAGE / "__init__.py").is_file())
# 可编辑安装或打包时也要带上它
import tomllib  # noqa: E402

check("包内模块都是 .py 文件（没有意外嵌套目录）",
      all(p.is_file() for p in PACKAGE.glob("*.py")),
      [p.name for p in PACKAGE.iterdir() if p.is_dir()])

# --------------------------------------------------------------------------- #
# 2. 每个子模块都能被**单独**导入（顺序无关，不依赖别人先准备好）
#    —— 这在子进程里做，避免本地已有缓存掩盖问题
# --------------------------------------------------------------------------- #
def run_python(code: str, skip_ui: bool = False) -> tuple:
    """在全新进程里跑一段代码。

    skip_ui=True 时设置 AI_PARTNER_SKIP_UI=1，让入口只做导入与代理导出、
    不触发页面渲染（否则在测试进程里渲染会失败）。
    """
    env = {**_harness.os.environ, "AI_PARTNER_DISABLE_AUTH": "1"}
    if skip_ui:
        env["AI_PARTNER_SKIP_UI"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT), capture_output=True, text=True, timeout=120,
        env=env,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


ok, out, err = run_python(
    "import lingheng, sys\n"
    "print('regular' if getattr(lingheng, '__file__', None) else 'namespace')\n"
)
check("lingheng 是正规包（有 __file__）", out == "regular", f"{out!r} {err[-200:]}")

# 最坏顺序：一上来就导入最上层的 ui（它依赖其余全部模块）
ok, out, err = run_python(
    "import lingheng.ui as ui\n"
    "print('main' if hasattr(ui, 'main') else 'no-main')\n"
)
check("直接导入 lingheng.ui 不会 circular import", out == "main", f"{out!r} {err[-300:]}")

# 逐个模块单独导入（每个都在全新进程里）
for module in ("config", "storage", "store", "profile", "sessions",
               "branches", "images", "auth", "ai", "ui"):
    ok, out, err = run_python(f"import lingheng.{module}\nprint('ok')\n")
    check(f"单独导入 lingheng.{module} 成功", ok == 0 and out == "ok", err[-160:])

# --------------------------------------------------------------------------- #
# 3. 入口按依赖顺序导入，且能渲染页面
# --------------------------------------------------------------------------- #
entry = (_harness.PROJECT / "AI_partner.py").read_text(encoding="utf-8")
check("入口声明了模块依赖顺序", "MODULE_ORDER" in entry)
check("入口用 importlib 显式按顺序导入（不靠 getattr 取属性）",
      "importlib.import_module" in entry, entry[:200])
for earlier, later in (("config", "storage"), ("storage", "store"), ("auth", "ai"),
                       ("ai", "ui")):
    check(f"依赖顺序里 {earlier} 在 {later} 之前",
          entry.index(f'"{earlier}"') < entry.index(f'"{later}"'))
check("入口没有遗留的调试 print", "TRACE" not in entry.upper() or "TRACE" not in entry,
      [line for line in entry.splitlines() if "TRACE" in line])

# 最终验证：**完整导入入口**（含全部子模块与代理导出），不触发页面渲染。
# 这一步是"Python 3.12 本地也能抓住那个事故"的关键：如果 lingheng 退化成
# namespace package、或者导入顺序不对，这里就会报 KeyError / circular import。
ok, out, err = run_python(
    "import AI_partner as app\n"
    "print('imported')\n"
    "print('sessions' if hasattr(app, 'save_session') else 'no-sessions')\n"
    "print('profile' if hasattr(app, 'DEFAULT_PROFILE') else 'no-profile')\n"
    "print('storage' if hasattr(app, 'ARCHIVE_DIR') else 'no-storage')\n",
    skip_ui=True,
)
lines = out.splitlines()
check("完整导入 AI_partner 不报错（KeyError / circular import 都不会出现）",
      ok == 0 and lines[:1] == ["imported"], err[-300:])
check("代理导出可用：app.save_session", "sessions" in lines, lines)
check("代理导出可用：app.DEFAULT_PROFILE", "profile" in lines, lines)
check("代理导出可用：app.ARCHIVE_DIR", "storage" in lines, lines)

# 真的能渲染（用 AppTest 跑真实入口）
from streamlit.testing.v1 import AppTest  # noqa: E402

at = AppTest.from_file(str(PROJECT / "AI_partner.py"), default_timeout=90).run()
check("真实入口能正常渲染（没有导入期异常）", not at.exception,
      [e.message[:200] for e in at.exception])

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
