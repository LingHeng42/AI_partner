"""凌恒的酒馆 —— 基于 Streamlit + DeepSeek 的角色扮演聊天应用（入口）。

运行方式：
    streamlit run AI_partner.py

需要环境变量 DEEPSEEK_API_KEY（可在系统环境变量或项目根目录的 .env 中配置）。

实现已经拆到 lingheng/ 包里（模块划分见 lingheng/__init__.py）。这个文件只做两件事：

1. **代理导出**包里的公开名字，所以历史上写过的 ``app.save_session(...)``、
   ``app.ARCHIVE_DIR = ...``、``app.client = ...`` 之类都照旧可用——测试正是靠
   这些来注入假 streamlit、假客户端和临时存档目录的。
2. 按 Streamlit 的方式执行页面：导入 ``lingheng.ui`` 就等于渲染整个页面。
"""

import importlib
import sys

UNDERLYING = ("config", "store", "profile", "sessions", "branches", "images", "ai", "ui")
PACKAGE = "lingheng"


def _import(name):
    """按需导入并缓存子模块。"""
    full = f"{PACKAGE}.{name}"
    module = sys.modules.get(full)
    if module is None:
        module = importlib.import_module(full)
    return module


def _loaded(name):
    return sys.modules.get(f"{PACKAGE}.{name}")


def _symbol_map():
    """公开名字 -> 子模块；冲突时以更靠后的模块（更上层）为准。"""
    table = {}
    for name in UNDERLYING:
        module = _loaded(name)
        if module is None:
            continue
        for symbol in dir(module):
            if not symbol.startswith("__"):
                table[symbol] = module
    return table


def __getattr__(name):
    for module_name in UNDERLYING:
        module = _loaded(module_name)
        if module is not None and hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __setattr__(name, value):
    if name == "ARCHIVE_DIR":
        import sys as _s; print("TRACE setattr ARCHIVE_DIR ->", value, file=_s.stderr); print("TRACE 目标 =", _symbol_map().get(name), file=_s.stderr)
    """赋值转发到"定义了这个名字"的子模块。

    例：``ARCHIVE_DIR = 临时目录`` 会改到 store 里那个变量，而其它模块都是运行时读
    ``store.ARCHIVE_DIR``，所以整套代码立刻跟着走；``client = 假客户端`` 同理改到 ai。
    """
    target = _symbol_map().get(name)
    if target is not None:
        setattr(target, name, value)
        return
    globals()[name] = value


def __dir__():
    return sorted(set(globals()) | set(_symbol_map()))


# 执行页面。放在最后：此时上面的代理已就绪。
_import("ui")
