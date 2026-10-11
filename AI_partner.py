"""凌恒的酒馆 —— 基于 Streamlit + DeepSeek 的角色扮演聊天应用（入口）。

运行方式：
    streamlit run AI_partner.py

需要环境变量 DEEPSEEK_API_KEY（可在系统环境变量或项目根目录的 .env 中配置）。

实现已经拆到 lingheng/ 包里（模块划分见 lingheng/__init__.py）。这个文件只做两件事：

1. **代理导出**包里的公开名字，所以历史上写过的 ``app.save_session(...)``、
   ``app.ARCHIVE_DIR = ...``、``app.client = ...`` 之类都照旧可用 —— 测试正是靠
   这些来注入假 streamlit、假客户端和临时存档目录的。
2. 执行页面：调用 ``lingheng.ui.main()``。

为什么把模块**按顺序一次性全部导入**（而不是用到再导入）：包内用了
``from . import config`` 这种"从包导入子模块"的写法。如果导入顺序不确定，某个
子模块可能在另一个还没放进 ``sys.modules`` 时就去导入它，于是抛::

    KeyError: 'lingheng.config'
    AttributeError: partially initialized module 'lingheng.ui' ... (circular import)

这在本地 Python 3.12 上不会发作，但 Streamlit Cloud 的 Python 3.14 会 —— 属于
"本地好好的、线上白屏"那类事故。按依赖顺序导全，就不会有半初始化的包。
"""

import importlib
import os
import sys

import lingheng

# 顺序 = 依赖顺序（下层在前）。**不要**改成按需导入，原因见上面的说明。
MODULE_ORDER = (
    "config",    # 路径与常量、默认人设、密钥与名单
    "storage",   # 存储后端（本地文件 / MongoDB）与按用户分区
    "store",     # 存储原语：会话/分支/附件的键名
    "profile",   # 人设与高级生成参数
    "sessions",  # 会话：元信息、分支文件、草稿、新建/删除
    "branches",  # 分支
    "images",    # 图片与头像
    "auth",      # 身份：登录、当前用户、允许名单
    "ai",        # 模型调用与对话流程
    "ui",        # 页面渲染（最后，因为它依赖上面全部）
)

# 按上面的顺序逐个导入。用 importlib 而不是 ``getattr(lingheng, name)``：
# 子模块**必须先被导入**才会成为包的属性，直接取属性拿不到。
_modules = [importlib.import_module(f"{lingheng.__name__}.{name}") for name in MODULE_ORDER]


def _child_modules():
    """已经加载的子模块（按依赖顺序）。程序内部用，所以用下划线开头。"""
    return _modules


def _symbol_map():
    """公开名字 -> 子模块；冲突时以更靠后的模块（更上层）为准。"""
    table = {}
    for module in _modules:
        for symbol in dir(module):
            if not symbol.startswith("__"):
                table[symbol] = module
    return table


def __getattr__(name):
    """``app.任何名字`` 都转发到定义它的子模块（历史上写过的用法照旧可用）。"""
    for module in _modules:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_symbol_map()))


# 执行页面：每次重跑都要真的调用一次。
# 注意不能只靠 `import lingheng.ui` —— import 有缓存，第二次重跑时模块已在
# sys.modules 里、顶层代码不会再执行，页面就白屏了（真踩过这个坑）。
#
# ``AI_PARTNER_SKIP_UI=1`` 只给测试用：让测试能 import 这个入口检查公开名字，
# 而不触发页面渲染。生产环境不会设置它。
if os.environ.get("AI_PARTNER_SKIP_UI") != "1":
    _modules[MODULE_ORDER.index("ui")].main()
