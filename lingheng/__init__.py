"""凌恒的酒馆 —— 应用实现。

这个文件（``__init__.py``）**必须存在**，它让 ``lingheng`` 成为一个**正规包**。

血泪教训（线上事故）：原来这里没有 ``__init__.py``，``lingheng`` 就成了
namespace package。本地 Python 3.12 跑得好好的，但 Streamlit Community Cloud
用的是 **Python 3.14**，对"部分初始化的 namespace package + 循环导入"更严格，
于是在 `from . import config` 处直接抛::

    KeyError: 'lingheng.config'
    AttributeError: partially initialized module 'lingheng.ui' ... (circular import)

页面脚本因此根本没执行完，表现就是"切换/删除会话后空白一片"。
**不要删掉这个文件。**

模块划分（依赖自底向上，不要反向导入）：

    config    路径与常量、默认人设、密钥与名单
    storage   存储后端：本地文件 / MongoDB，并负责按用户分区
    store     存储原语：会话/分支/附件的键名
    profile   人设与高级生成参数的读写
    sessions  会话：元信息、分支文件、草稿、新建/删除
    branches  分支：新建、切换、改名、删除
    images    会话内图片与头像
    auth      身份：登录、当前用户、允许名单
    ai        模型调用与对话流程
    ui        页面渲染（主区域 + 侧边栏）

入口 ``AI_partner.py`` 只做两件事：代理导出这里的公开名字，以及执行 ``ui.main()``。
"""

# 包版本（便于排查线上到底跑的是哪一版）
__version__ = "2.0"
