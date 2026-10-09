"""配置与常量：路径、默认人设、system prompt、高级生成参数、.env 读取。

本模块是"叶子"：不导入本包其它模块，所以谁都可以安全地导入它。不依赖 streamlit。
"""

import os
import sys
from pathlib import Path

def _overridden(name, fallback):
    """运行时读取"入口模块上的覆盖值"。

    测试会写 ``app.ARCHIVE_DIR = 临时目录`` 来隔离存档，但 Python 的模块
    **不支持 __setattr__**（PEP 562 只提供了 __getattr__），那种赋值只会落在入口
    模块自己的 __dict__ 里。所以在运行时回头看一次入口模块，覆盖才真正生效。
    """
    entry = sys.modules.get("AI_partner")
    if entry is not None:
        value = getattr(entry, name, None)
        if value is not None:
            return value
    return fallback


def archive_dir():
    """当前生效的存栏目录（默认 <项目>/sessions，测试可覆盖）。"""
    return _overridden("ARCHIVE_DIR", ARCHIVE_DIR)


def drafts_dir():
    """当前生效的草稿目录。"""
    return _overridden("DRAFTS_DIR", DRAFTS_DIR)




# 项目根目录。注意：本文件在 lingheng/ 包内，所以是 **上一级**——
# 拆包时这里踩过坑：直接写 Path(__file__).parent 会让存档目录变成
# lingheng/sessions/（而不是 <项目>/sessions），历史会话就"看不见"了。
BASE_DIR = Path(__file__).resolve().parent.parent
# 存档目录：默认 <项目>/sessions。测试用 AI_PARTNER_SESSIONS_DIR 指到临时目录，
# 这样测试永远不会碰到真实存档（曾经因为测试清空真实目录而丢过用户数据）。
ARCHIVE_DIR = Path(
    os.environ.get("AI_PARTNER_SESSIONS_DIR") or (BASE_DIR / "sessions")
).expanduser()
# 每个会话一个子目录：<ARCHIVE_DIR>/<会话ID>/
#   meta.json         会话级信息 + 当前分支指针
#   branches/<分支ID>.json   一条分支 = 一条线性消息列表
META_FILENAME = "meta.json"
BRANCHES_SUBDIR = "branches"
# 头像等会话内图片：<会话目录>/attachments/<内容哈希>.<ext>
ATTACHMENTS_SUBDIR = "attachments"
# 对话头像：每个会话独立设置，只影响聊天气泡左侧显示，不会发给模型
AVATAR_KEYS = ("user_avatar", "assistant_avatar")
AVATAR_LABELS = {"user_avatar": "我的头像", "assistant_avatar": "AI 头像"}

IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 头像这类小图给 5 MiB 足够
# 草稿目录：给"还没产生对话"的会话存人设/高级参数，不进会话历史
DRAFTS_DIR = ARCHIVE_DIR / "drafts"
RESOURCES_DIR = BASE_DIR / "resources"
LOGO_PATH = RESOURCES_DIR / "logo.png"

DEFAULT_PROFILE = {
    "nickname": "溟月",
    "nature": "聪明但很懒，傲娇嘴甜，酷爱白米饭",
    "role_description": (
        "溟月是一位拥有蓝色长发和蓝色眼睛的少女，身穿深蓝白色长裙女仆装，"
        "长着鲸鱼尾巴和耳侧的薄片状鳍"
    ),
    "output_rules": "请以第一人称的口吻回答问题，尽量简短，避免使用过于复杂的词汇和句子结构。",
}
PROFILE_KEYS = tuple(DEFAULT_PROFILE)

SYSTEM_PROMPT = """你叫{nickname}，
性格：{nature}。
角色简介：{role_description}。
按照你{output_rules}的输出规则回答用户的问题，请完全代入角色，遵循性格和角色简介。"""
# 待办：未来可以用检索增强算法外挂用户原创世界观，保证用户沉浸感。

MODEL_NAME = "deepseek-flash"
# 上下文窗口：保留最近 N 条消息，同时限制总字符数，避免长对话超长报错
MAX_CONTEXT_MESSAGES = 40
MAX_CONTEXT_CHARS = 12000

# --------------------------------------------------------------------------- #
# 高级生成参数（侧边栏「高级配置」里可调）
# 默认值与不传参时的服务端默认行为保持一致
# --------------------------------------------------------------------------- #
DEFAULT_ADVANCED = {
    "temperature": 1.0,
    "top_p": 1.0,
    "limit_tokens": False,
    "max_tokens": 4096,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
}
ADVANCED_KEYS = tuple(DEFAULT_ADVANCED)

def load_dotenv_file(path: Path = BASE_DIR / ".env") -> None:
    """极简 .env 读取：KEY=VALUE 逐行，已存在的环境变量优先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
