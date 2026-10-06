"""凌恒的酒馆 —— 基于 Streamlit + DeepSeek 的角色扮演聊天应用。

运行方式：
    streamlit run AI_partner.py

需要环境变量 DEEPSEEK_API_KEY（可在系统环境变量或 .env 中配置）。
"""

import datetime
import json
import os
import time
from pathlib import Path

import streamlit as st
from openai import OpenAI

# --------------------------------------------------------------------------- #
# 路径：全部基于脚本自身位置解析，从任何工作目录启动都能找到 sessions/ 与 resources/
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
RESOURCES_DIR = BASE_DIR / "resources"
LOGO_PATH = RESOURCES_DIR / "logo.png"

# --------------------------------------------------------------------------- #
# 默认人设：只在这里定义一次，初始化 / 加载 / 删除会话统一复用
# --------------------------------------------------------------------------- #
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


# 先尝试补全环境变量，后面的 require_api_key() / OpenAI 客户端都依赖它
load_dotenv_file()


# --------------------------------------------------------------------------- #
# 会话存档
# --------------------------------------------------------------------------- #
def new_session_id() -> str:
    """生成会话 ID：精确到毫秒，避免同一秒内连续新建会话互相覆盖。"""
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]


def profile_from_state() -> dict:
    return {key: st.session_state[key] for key in PROFILE_KEYS}


def reset_profile() -> None:
    """把角色人设恢复为默认值（同时重置对话内容）。"""
    for key, value in DEFAULT_PROFILE.items():
        st.session_state[key] = value
    st.session_state.message = []


def save_session() -> None:
    """把当前会话原子写入 sessions/<id>.json，写一半崩溃也不会损坏旧存档。"""
    if not st.session_state.get("current_session"):
        return
    session_data = {"message": st.session_state.message, **profile_from_state()}
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    target = SESSIONS_DIR / f"{st.session_state.current_session}.json"
    tmp = target.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)
    for attempt in range(5):  # Windows 上杀毒/索引可能短暂占用文件，重试几次
        try:
            os.replace(tmp, target)  # 原子替换
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def load_session_list() -> list:
    """会话名本身就是可排序时间戳，直接倒序，不依赖 os.listdir 的返回顺序。"""
    if not SESSIONS_DIR.exists():
        return []
    names = [f.stem for f in SESSIONS_DIR.glob("*.json")]
    return sorted(names, reverse=True)


def _safe_session_path(session_name: str) -> Path:
    """只允许纯文件名，挡掉 ../ 之类的路径穿越。"""
    if not session_name or Path(session_name).name != session_name:
        raise ValueError(f"非法会话名：{session_name!r}")
    return SESSIONS_DIR / f"{session_name}.json"


def load_selected_session(session_name: str) -> None:
    try:
        path = _safe_session_path(session_name)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as f:
            session_data = json.load(f)
        st.session_state.message = session_data.get("message", [])
        for key, default in DEFAULT_PROFILE.items():
            st.session_state[key] = session_data.get(key) or default
        st.session_state.current_session = session_name
    except Exception as e:  # noqa: BLE001 - 单个存档损坏不应中断整个页面
        st.error(f"加载会话失败: {e}")
        return
    st.rerun()  # 刷新页面


def delete_session(session_name: str) -> None:
    try:
        path = _safe_session_path(session_name)
        if path.exists():
            path.unlink()
        if session_name == st.session_state.current_session:
            reset_profile()
            st.session_state.current_session = new_session_id()
    except Exception as e:  # noqa: BLE001
        st.error(f"删除会话失败: {e}")
        return
    st.rerun()  # 刷新页面


# --------------------------------------------------------------------------- #
# OpenAI 客户端：API Key 缺失时给出清晰提示，而不是抛 SDK 的堆栈
# --------------------------------------------------------------------------- #
def require_api_key() -> str:
    """每次脚本运行都校验一次 API Key，缺失就停在这一步并给出可操作的提示。"""
    load_dotenv_file()
    api_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        st.error(
            "未读取到环境变量 `DEEPSEEK_API_KEY`，无法调用 DeepSeek 接口。\n\n"
            "请在系统环境变量或项目根目录的 `.env` 中配置后重启应用。"
        )
        st.stop()
    return api_key


@st.cache_resource(show_spinner=False)
def get_client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url="https://api.deepseek.com")


def trim_context(messages: list) -> list:
    """从最新消息往前取，同时受条数与字符数限制（system prompt 不在此列表内）。"""
    kept, used, dropped = [], 0, False
    for msg in reversed(messages):
        cost = len(str(msg.get("content", ""))) + 8
        if kept and (len(kept) >= MAX_CONTEXT_MESSAGES or used + cost > MAX_CONTEXT_CHARS):
            dropped = True
            break
        kept.append(msg)
        used += cost
    if dropped:
        st.caption("⚠️ 对话较长，本次请求只发送了最近的部分历史记录。")
    return list(reversed(kept))


def build_messages() -> list:
    system_content = SYSTEM_PROMPT.format(**profile_from_state())
    return [{"role": "system", "content": system_content}, *trim_context(st.session_state.message)]


def render_reply(placeholder) -> None:
    """请求模型并把流式回答渲染到 placeholder，最后写入会话历史并落盘。

    异常在这里被消化成 st.error，调用方（页面脚本）不再需要 try/except。
    """
    full_response = ""
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=build_messages(),
            stream=True,
            reasoning_effort="low" if st.session_state.thinking else None,
            extra_body={"thinking": {"type": "enabled" if st.session_state.thinking else "disabled"}},
        )
        with placeholder.chat_message("assistant"):
            message_placeholder = st.empty()
            for chunk in response:
                if not getattr(chunk, "choices", None):
                    continue
                delta = chunk.choices[0].delta
                if delta is None or not delta.content:
                    continue
                full_response += delta.content
                # 末尾补一个光标，让流式输出的边界可见；只更新占位符，不重建整条消息
                message_placeholder.markdown(full_response + "▌")
            message_placeholder.markdown(full_response)
    except Exception as e:  # noqa: BLE001 - 网络/限流/余额等异常不该把页面打成 traceback
        placeholder.empty()
        st.error(f"请求模型失败：{e}")
        st.caption("本条消息已保留在对话中，可直接重试或继续输入。")

    if full_response:
        st.session_state.message.append({"role": "assistant", "content": full_response})
    else:
        st.warning("模型没有返回任何内容，本次回答未记录。")

    save_session()


# --------------------------------------------------------------------------- #
# 页面配置（必须是第一个 Streamlit 命令）
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="凌恒的酒馆",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "🐳",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Report a bug": "https://github.com/LingHeng42",
        "About": "凌恒的酒馆是一个基于人工智能的聊天平台，旨在为用户提供一个有趣、互动和智能的聊天体验。",
    },
)

# 会话状态初始化
for _key, _value in DEFAULT_PROFILE.items():
    if _key not in st.session_state:
        st.session_state[_key] = _value
if "message" not in st.session_state:
    st.session_state.message = []
if "thinking" not in st.session_state:
    st.session_state.thinking = False
if "current_session" not in st.session_state:
    st.session_state.current_session = new_session_id()

# 每次脚本运行都校验 Key（函数本身不缓存），缓存只作用在客户端构造上
client = get_client(require_api_key())

# 标题
st.header("凌恒的酒馆")

# logo（文件缺失时不影响页面）
if LOGO_PATH.exists():
    try:
        st.logo(str(LOGO_PATH), size="large")
    except Exception:  # noqa: BLE001 - 老版本 streamlit 没有 st.logo
        pass

# 当前会话标识（原来的 st.text 调试残留，改为弱化的说明文字）
st.caption(f"当前会话：{st.session_state.current_session}")

# 展示历史对话
for msg in st.session_state.message:
    st.chat_message(msg["role"]).write(msg["content"])

# --------------------------------------------------------------------------- #
# 侧边栏
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.title("AI管理面板")

    # 新建会话
    if st.button("新建会话", width="stretch", icon="📝"):
        save_session()
        reset_profile()
        st.session_state.current_session = new_session_id()
        save_session()
        st.rerun()  # 刷新页面

    st.subheader("会话历史")
    session_list = load_session_list()
    if not session_list:
        st.caption("还没有保存的会话。")
    for index, session in enumerate(session_list):
        col1, col2 = st.columns([4, 1])
        with col1:
            st.button(
                session,
                width="stretch",
                icon="📄",
                key=f"session_{index}_{session}",
                type="primary" if session == st.session_state.current_session else "secondary",
                on_click=lambda s=session: load_selected_session(s),
            )
        with col2:
            # 删除前二次确认，避免误删存档
            with st.popover("❌", width="stretch"):
                st.caption(f"确认删除会话\n\n`{session}` ？")
                if st.button("确认删除", key=f"confirm_delete_{index}_{session}", width="stretch", type="primary"):
                    delete_session(session)
    st.divider()

    # 角色管理（先落盘到 session_state，再作为控件默认值回填）
    st.subheader("管理角色")
    nickname = st.text_input("昵称", value=st.session_state.nickname, placeholder="请输入昵称")
    if nickname:
        st.session_state.nickname = nickname
    nature = st.text_area("性格", value=st.session_state.nature, placeholder="请输入性格描述")
    if nature:
        st.session_state.nature = nature
    role_description = st.text_area("角色简介", value=st.session_state.role_description, placeholder="请输入角色简介")
    if role_description:
        st.session_state.role_description = role_description
    output_rules = st.text_area("输出规则", value=st.session_state.output_rules, placeholder="请输入输出规则")
    if output_rules:
        st.session_state.output_rules = output_rules

    st.divider()

    # 生成参数
    st.subheader("生成参数")
    st.toggle(
        "深度思考",
        key="thinking",
        help="开启后模型会先推理再回答，速度更慢但更严谨；关闭则直接作答。",
    )


# --------------------------------------------------------------------------- #
# 对话框
# --------------------------------------------------------------------------- #
prompt = st.chat_input("请输入您的想法...")

if prompt:
    st.session_state.message.append({"role": "user", "content": prompt})  # 用户输入入列
    st.chat_message("user").write(prompt)
    # 复用同一个占位符，避免每个 chunk 都重建组件
    render_reply(st.empty())
