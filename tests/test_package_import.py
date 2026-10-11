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
import os
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

    ⚠️ 必须显式指定 UTF-8：Windows 上 ``text=True`` 会按系统编码（GBK）解码子进程
    输出，而报错信息里含中文，会直接抛 UnicodeDecodeError 把测试本身弄崩
    （与应用的编码无关，纯粹是测试的读法问题）。

    ⚠️ 还必须给子进程一个**独立的临时根目录**（AI_PARTNER_TEST_TMP）：``_harness``
    在被导入时会调用 ``reset_tmp()`` 清空整个 TMP_DIR。如果子进程共用父进程的
    tests/.tmp，它会把**父测试正在用的存档目录一并清掉**，于是同一次 run_tests.py 里
    排在后面的套件会莫名其妙失败（而且每次失败的套件都不一样，极难定位）。
    """
    env = {
        **_harness.os.environ,
        "AI_PARTNER_DISABLE_AUTH": "1",
        # 让子进程输出 UTF-8，两端编码一致
        "PYTHONIOENCODING": "utf-8",
        # 子进程用**独立的临时根**（带父进程 pid，保证唯一），绝不能清掉父进程的
        # 存档目录。若与父进程共用 tests/.tmp，子进程导入 _harness 时的 reset_tmp()
        # 会把父测试正在用的 sessions_<pid> 一并删掉 —— 表现为同一次 run_tests.py 里
        # 排在后面的套件随机失败，且每次失败的套件都不一样（极难定位）。
        "AI_PARTNER_TEST_TMP": str(
            _harness.TMP_DIR.parent / f"_child_tmp_{os.getpid()}"
        ),
    }
    if skip_ui:
        env["AI_PARTNER_SKIP_UI"] = "1"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=str(PROJECT), capture_output=True, text=True, timeout=120,
        env=env, encoding="utf-8", errors="replace",
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

# --------------------------------------------------------------------------- #
# 3.5 禁止对 session_state 用"属性式"访问（pop / setdefault / update / copy）
#
# 线上事故：``st.session_state.pop(KEY, None)`` 在真实的 session_state 上不是方法
# 调用，而是被当成"读取名为 pop 的键"，直接抛::
#
#     KeyError: 'st.session_state has no key "pop"'
#
# 它发生在主脚本体**一开头**（run_pending_actions），于是每次重跑页面都空白。
# 测试里的假 session_state 有 pop 方法，所以本地跑不出来 —— 只能靠静态检查兜住。
# 正确写法：先 ``get()`` 再 ``st.session_state[KEY] = None``。
# --------------------------------------------------------------------------- #
import io  # noqa: E402
import re  # noqa: E402
import tokenize  # noqa: E402

BAD_SESSION_ACCESS = (
    r"session_state\s*\.\s*pop\s*\(",
    r"session_state\s*\.\s*setdefault\s*\(",
    r"session_state\s*\.\s*update\s*\(",
)


def code_only(path: Path) -> str:
    """去掉注释与字符串字面量，只留真正的代码。

    否则连"注释里说明不要用 setdefault"都会被判成违规（假阳性）。
    """
    text = path.read_text(encoding="utf-8")
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except tokenize.TokenError:
        return text
    return " ".join(out)


offenders = []
for py in sorted(PACKAGE.glob("*.py")) + [PROJECT / "AI_partner.py"]:
    text = code_only(py)
    for pattern in BAD_SESSION_ACCESS:
        for _ in re.finditer(pattern, text):
            offenders.append(f"{py.name}: {pattern}")
check("没有对 session_state 用属性式访问（pop/setdefault/update）", not offenders, offenders)

# --------------------------------------------------------------------------- #
# 3.6 st.rerun() 不能出现在主脚本开头调用的下层函数里
#
# 线上事故：delete_session 内部有 st.rerun()，而它是在主脚本**一开头**
# （run_pending_actions）被调用的。st.rerun() 靠**抛异常**中断脚本，于是在主脚本
# 刚开头就把后面的渲染全部掐掉 —— 表现是"每次删除/切换会话后页面空白"。
# 更隐蔽的是它还会被自己的 except Exception 抓住、串成多层 rerun。
# 诊断面板只记到"主脚本：发现待办"，永远没有"已执行"，一眼就能看出没返回。
#
# 规矩：**重跑只能由 run_pending_actions 发起**，被它调用的 delete_session /
# new_session 里绝不能有 st.rerun()。
# --------------------------------------------------------------------------- #
import ast as _ast  # noqa: E402

GUARDED = {"delete_session": "sessions.py", "new_session": "sessions.py"}
rerun_offenders = []
for func_name, file_name in GUARDED.items():
    tree = _ast.parse((PACKAGE / file_name).read_text(encoding="utf-8"))
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == func_name:
            for sub in _ast.walk(node):
                if (isinstance(sub, _ast.Call)
                        and isinstance(sub.func, _ast.Attribute)
                        and sub.func.attr == "rerun"):
                    rerun_offenders.append(f"{file_name}:{sub.lineno} {func_name}() 里有 st.rerun()")
check("主脚本开头调用的 delete_session/new_session 里没有 st.rerun()（会掐断渲染）",
      not rerun_offenders, rerun_offenders)

# 反过来：run_pending_actions 必须显式发起一次重跑，否则页面画的是旧状态
tree = _ast.parse((PACKAGE / "sessions.py").read_text(encoding="utf-8"))
pending_has_rerun = False
for node in _ast.walk(tree):
    if isinstance(node, _ast.FunctionDef) and node.name == "run_pending_actions":
        pending_has_rerun = any(
            isinstance(sub, _ast.Call)
            and isinstance(sub.func, _ast.Attribute)
            and sub.func.attr == "rerun"
            for sub in _ast.walk(node)
        )
check("run_pending_actions 执行完会重跑一次（用新状态重画）", pending_has_rerun)

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
check("首次渲染确实画出了页面（不是空白）", len(at.main) >= 1, len(at.main))

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
