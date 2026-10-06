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
# 存档目录：默认 <项目>/sessions。测试用 AI_PARTNER_SESSIONS_DIR 指到临时目录，
# 这样测试永远不会碰到真实存档（曾经因为测试清空真实目录而丢过用户数据）。
SESSIONS_DIR = Path(
    os.environ.get("AI_PARTNER_SESSIONS_DIR") or (BASE_DIR / "sessions")
).expanduser()
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


# 先尝试补全环境变量，后面的 require_api_key() / OpenAI 客户端都依赖它
load_dotenv_file()


# --------------------------------------------------------------------------- #
# 会话存档
# --------------------------------------------------------------------------- #
DEFAULT_SESSION_TITLE = "新会话"


def new_session_id() -> str:
    """生成会话 ID（= 存档文件名）：精确到毫秒，避免同一秒内连续新建会话互相覆盖。"""
    return datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")[:-3]


def profile_from_state() -> dict:
    return {key: st.session_state[key] for key in PROFILE_KEYS}


def session_meta(session_name: str) -> dict:
    """读某个存档的元信息（名称、是否置顶）；读不出来时给出安全默认值。"""
    meta = {"title": session_name, "pinned": False}
    try:
        path = _safe_session_path(session_name)
        if not path.exists():
            return meta
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        meta["title"] = (data.get("title") or "").strip() or session_name
        meta["pinned"] = bool(data.get("pinned"))
    except Exception:  # noqa: BLE001 - 元信息损坏不该影响整个侧边栏
        pass
    return meta


def session_title(session_name: str) -> str:
    """读某个存档的自定义会话名称；没设置过就回退成会话 ID（时间戳）。"""
    return session_meta(session_name)["title"]


def _write_json_atomic(path: Path, data: dict) -> None:
    """原子写 JSON：先写临时文件再替换，失败时清理临时文件。

    临时文件用随机后缀，避免和上一次失败残留的 .tmp 互相干扰。
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        for attempt in range(5):  # Windows 上杀毒/索引可能短暂占用文件，重试几次
            try:
                os.replace(tmp, path)  # 原子替换
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        # 成功时 tmp 已被 replace 掉；失败时把它删掉，不留孤儿文件
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _update_session_meta(session_name: str, **changes) -> None:
    """就地更新存档里的元信息字段（title / pinned），保留消息与人设。"""
    path = _safe_session_path(session_name)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.update(changes)
    _write_json_atomic(path, data)


def rename_session(session_name: str, new_title: str = None, input_key: str = None) -> None:
    """重命名会话：只改存档里的 title，不重命名文件。

    作为 st.text_input 的 on_change 回调执行（回调先于控件实例化）：
    不传 new_title 时从 input_key 指向的输入框里取值；重命名当前会话时同步
    st.session_state['session_title']，让侧边栏的「会话名称」和主区域标题跟着变。
    这个同步**只能**在回调里做，否则会撞上
    StreamlitWidgetAlreadyInstantiatedError。
    """
    if new_title is None:
        new_title = st.session_state.get(input_key, "")
    title = (new_title or "").strip() or DEFAULT_SESSION_TITLE
    try:
        _update_session_meta(session_name, title=title)
        if session_name == st.session_state.get("current_session"):
            st.session_state.session_title = title
    except Exception as e:  # noqa: BLE001
        st.error(f"重命名失败: {e}")


def set_pinned(session_name: str, pinned: bool) -> None:
    """置顶 / 取消置顶会话（写入存档的 pinned 字段），同样走回调。"""
    try:
        _update_session_meta(session_name, pinned=bool(pinned))
    except Exception as e:  # noqa: BLE001
        st.error(f"置顶失败: {e}")
        return
    st.rerun()


def reset_profile() -> None:
    """把角色人设恢复为默认值（同时重置对话内容）。"""
    for key, value in DEFAULT_PROFILE.items():
        st.session_state[key] = value
    st.session_state.message = []


def save_session() -> None:
    """把当前会话原子写入 sessions/<会话ID>.json，写一半崩溃也不会损坏旧存档。

    空对话（还没有任何消息）不落盘：否则每次新建会话都会留下一个空档案，
    历史里会出现两条同名记录。
    """
    if not st.session_state.get("current_session"):
        return
    if not st.session_state.get("message"):
        return
    session_data = {
        "title": st.session_state.get("session_title", ""),
        "pinned": bool(st.session_state.get("session_pinned", False)),
        "message": st.session_state.message,
        **profile_from_state(),
    }
    _write_json_atomic(SESSIONS_DIR / f"{st.session_state.current_session}.json", session_data)


def load_session_list() -> list:
    """会话历史：置顶的排在最前，其余按时间从新到旧。

    只列出磁盘上真正存在的存档；新开但还没发言的会话不会出现（空对话不落盘）。
    """
    if not SESSIONS_DIR.exists():
        return []
    names = [f.stem for f in SESSIONS_DIR.glob("*.json")]
    # key 用字符串而非 datetime，省一次解析；会话 ID 是定长时间戳，字典序即时间序
    return sorted(names, key=lambda n: (session_meta(n)["pinned"], n), reverse=True)


def _safe_session_path(session_name: str) -> Path:
    """只允许纯文件名，挡掉 ../ 之类的路径穿越。"""
    if not session_name or Path(session_name).name != session_name:
        raise ValueError(f"非法会话名：{session_name!r}")
    return SESSIONS_DIR / f"{session_name}.json"


def session_file_exists(session_name: str = None) -> bool:
    """当前会话是否已有存档文件（= 侧边栏会话历史里能否列出它）。"""
    name = session_name or st.session_state.get("current_session")
    if not name:
        return False
    try:
        return _safe_session_path(name).exists()
    except ValueError:
        return False


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
        # 会话名称与置顶状态存在存档里；老存档没这些字段就回退成默认值
        st.session_state.session_title = (session_data.get("title") or "").strip() or session_name
        st.session_state.session_pinned = bool(session_data.get("pinned"))
        st.session_state.current_session = session_name
    except Exception as e:  # noqa: BLE001 - 单个存档损坏不应中断整个页面
        st.error(f"加载会话失败: {e}")
        return
    st.rerun()  # 刷新页面


def new_session() -> None:
    """新建会话：先把当前会话落盘，再清空人设与对话、换新 ID 和默认名称。

    必须作为按钮的 on_click 回调执行：回调在控件实例化之前运行，
    否则修改 st.session_state['session_title']（已被输入框占用）会报
    StreamlitWidgetAlreadyInstantiatedError。
    """
    save_session()
    reset_profile()
    st.session_state.current_session = new_session_id()
    st.session_state.session_title = DEFAULT_SESSION_TITLE
    st.session_state.session_pinned = False
    save_session()


def delete_session(session_name: str) -> None:
    try:
        path = _safe_session_path(session_name)
        if path.exists():
            path.unlink()
        if session_name == st.session_state.current_session:
            reset_profile()
            st.session_state.current_session = new_session_id()
            st.session_state.session_title = DEFAULT_SESSION_TITLE
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
    history = []
    for msg in trim_context(st.session_state.message):
        # 官方示例会把 reasoning_content 一起回传，API 会忽略它、也不计入上下文
        if msg.get("role") == "assistant" and msg.get("reasoning_content"):
            history.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "reasoning_content": msg["reasoning_content"],
            })
        else:
            history.append({"role": msg["role"], "content": msg.get("content", "")})
    return [{"role": "system", "content": system_content}, *history]


def advanced_value(name: str):
    return st.session_state.get(name, DEFAULT_ADVANCED[name])


def reset_advanced() -> None:
    """恢复高级配置默认值。

    必须作为 on_click 回调执行：这些键已被滑块/复选框控件占用，
    在控件实例化之后再赋值会报 StreamlitWidgetAlreadyInstantiatedError。
    """
    for key, value in DEFAULT_ADVANCED.items():
        st.session_state[key] = value


def build_request_payload() -> dict:
    """按侧边栏的高级配置拼装请求参数，未启用的项不发送。"""
    payload = {
        "model": MODEL_NAME,
        "messages": build_messages(),
        "stream": True,
        "temperature": advanced_value("temperature"),
        "top_p": advanced_value("top_p"),
        "extra_body": {
            "thinking": {"type": "enabled" if st.session_state.thinking else "disabled"},
        },
    }
    if st.session_state.thinking:
        payload["reasoning_effort"] = "low"
    if advanced_value("limit_tokens"):
        payload["max_tokens"] = int(advanced_value("max_tokens"))
    if advanced_value("frequency_penalty"):
        payload["frequency_penalty"] = advanced_value("frequency_penalty")
    if advanced_value("presence_penalty"):
        payload["presence_penalty"] = advanced_value("presence_penalty")
    return payload


def _first_choice(chunk):
    choices = getattr(chunk, "choices", None)
    if not choices:
        return None
    return choices[0]


def _delta_text(chunk) -> tuple:
    """返回本片段里的 (推理内容, 正文内容)，两者都可能为空字符串。"""
    choice = _first_choice(chunk)
    delta = getattr(choice, "delta", None) if choice is not None else None
    if delta is None:
        return "", ""
    reasoning = getattr(delta, "reasoning_content", None) or ""
    content = getattr(delta, "content", None) or ""
    return reasoning, content


def _remember_expander(widget_key: str, state_key: str) -> None:
    """把用户手动展开/折叠的状态镜像到状态键，避免每次重跑都被重置。"""
    st.session_state[state_key] = st.session_state[widget_key]


def reasoning_expander(key: str, state_key: str, expanded: bool):
    """带记忆的「思考过程」折叠面板：用户手动开合后不会被流式重跑重置。"""
    st.session_state.setdefault(state_key, expanded)
    return st.expander(
        "🤔 思考过程",
        expanded=st.session_state[state_key],
        key=key,
        on_change=_remember_expander,
        args=(key, state_key),
    )


def render_reply(placeholder) -> None:
    """请求模型，把流式推理渲染进折叠面板、正文渲染进气泡，最后写入历史并落盘。

    异常在这里被消化成 st.error，调用方（页面脚本）不再需要 try/except。
    """
    reasoning, full_response = "", ""
    thinking_enabled = bool(st.session_state.thinking)
    try:
        response = client.chat.completions.create(**build_request_payload())
        with placeholder.chat_message("assistant"):
            reasoning_placeholder, reasoning_text = None, None
            if thinking_enabled:
                reasoning_placeholder = reasoning_expander("reasoning_panel", "show_reasoning", True)
                with reasoning_placeholder:
                    reasoning_text = st.empty()
            message_placeholder = st.empty()
            if thinking_enabled:
                message_placeholder.markdown("_正在思考…_")
            for chunk in response:
                reasoning_piece, content_piece = _delta_text(chunk)
                if reasoning_piece:
                    reasoning += reasoning_piece
                    if reasoning_placeholder is not None:
                        reasoning_text.markdown(reasoning + "▌")
                if content_piece:
                    full_response += content_piece
                    # 末尾补一个光标，让流式输出的边界可见；只更新占位符，不重建整条消息
                    message_placeholder.markdown(full_response + "▌")
            if reasoning_placeholder is not None:
                reasoning_text.markdown(reasoning or "_（本次没有输出推理内容）_")
            message_placeholder.markdown(full_response)
    except Exception as e:  # noqa: BLE001 - 网络/限流/余额等异常不该把页面打成 traceback
        placeholder.empty()
        st.error(f"请求模型失败：{e}")
        st.caption("本条消息已保留在对话中，可直接重试或继续输入。")

    if full_response:
        reply = {"role": "assistant", "content": full_response}
        if reasoning:
            reply["reasoning_content"] = reasoning
        st.session_state.message.append(reply)
    else:
        st.warning("模型没有返回任何内容，本次回答未记录。")

    existed = session_file_exists()
    save_session()
    if not existed and session_file_exists():
        # 本次是这条会话的第一个存档：侧边栏是在本轮脚本顶部渲染的，那时文件还
        # 不存在，所以必须主动重跑一次，会话才会立刻出现在会话历史里。
        # （不能只依赖 chat_input 提交后浏览器的隐式重跑，实测它不保证发生。）
        st.rerun()


def render_history() -> None:
    """按存储顺序回放历史：带推理的回答把思考过程放进折叠面板。"""
    for index, msg in enumerate(st.session_state.message):
        with st.chat_message(msg["role"]):
            if msg.get("reasoning_content"):
                with reasoning_expander(f"history_reasoning_{index}", f"history_reasoning_open_{index}", False):
                    st.markdown(msg["reasoning_content"])
            if msg.get("content"):
                st.markdown(msg["content"])


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
    st.session_state.setdefault(_key, _value)
for _key, _value in DEFAULT_ADVANCED.items():
    st.session_state.setdefault(_key, _value)
st.session_state.setdefault("message", [])
st.session_state.setdefault("thinking", False)
st.session_state.setdefault("session_title", DEFAULT_SESSION_TITLE)
if "current_session" not in st.session_state:
    st.session_state.current_session = new_session_id()

# 每次脚本运行都校验 Key（函数本身不缓存），缓存只作用在客户端构造上
client = get_client(require_api_key())

# logo（文件缺失时不影响页面）
# if LOGO_PATH.exists():
#     try:
#         st.logo(str(LOGO_PATH), size="large")
#     except Exception:  # noqa: BLE001 - 老版本 streamlit 没有 st.logo
#         pass

# 展示历史对话（开启过深度思考的回答会带上可折叠的思考过程）
st.header(st.session_state.session_title)
render_history()

# --------------------------------------------------------------------------- #
# 侧边栏
# --------------------------------------------------------------------------- #
with st.sidebar:
    # 标题：小号字放在 logo 右侧
    logo_col, title_col = st.columns([1, 6], vertical_alignment="center")
    with logo_col:
        if LOGO_PATH.exists():
            try:
                st.image(str(LOGO_PATH), width=64)
            except Exception:  # noqa: BLE001 - 图片渲染失败不影响功能
                pass
    with title_col:
        st.markdown("### 凌恒的酒馆")
        st.caption("AI 角色扮演聊天")

    st.divider()

    # 新建会话：状态改动全部放在 on_click 回调里（回调先于控件实例化执行）
    st.button("新建会话", width="stretch", icon="📝", on_click=new_session)

    # 会话名称：与存档文件名（会话 ID）解耦，改名不会新建/移动文件
    # 注意：控件 key 直接占用 "session_title"，所以只能在回调里改这个键
    st.text_input(
        "会话名称",
        value=st.session_state.session_title,
        key="session_title",
        placeholder=DEFAULT_SESSION_TITLE,
        help="只影响显示，不会改变存档文件名（文件名始终是会话 ID 时间戳）。",
    )

    st.subheader("会话历史")
    session_list = load_session_list()
    if not session_list:
        st.caption("还没有会话。发出第一条消息后，这里会出现本次会话。")
    for session in session_list:
        current = session == st.session_state.current_session
        meta = session_meta(session)
        # 当前会话的名称就在输入框里，直接用它，避免和输入框内容不一致
        label = st.session_state.session_title if current else meta["title"]
        pin_mark = "📌 " if meta["pinned"] else ""
        col1, col2 = st.columns([4, 1])
        with col1:
            st.button(
                f"{pin_mark}{label}（当前）" if current else f"{pin_mark}{label}",
                width="stretch",
                key=f"session_{session}",
                help=f"会话 ID：{session}",
                type="primary" if current else "secondary",
                on_click=lambda s=session: load_selected_session(s),
            )
        with col2:
            # 一个入口搞定：重命名 / 置顶 / 删除。
            # key 一律只用会话 ID，不带列表序号：删掉上一条会整体上移，
            # 带序号的话弹层展开状态和输入框内容会串到下一条会话上。
            with st.popover("⋯", width="stretch", help="重命名、置顶或删除这个会话", key=f"menu_{session}"):
                # 重命名：输入框里改完按回车即生效。
                # 必须用 on_change 回调：回调在控件实例化之前执行，这样改
                # st.session_state['session_title'] 才是合法操作。
                st.text_input(
                    "重命名",
                    value=label,
                    key=f"rename_input_{session}",
                    on_change=rename_session,
                    args=(session,),
                    kwargs={"new_title": None, "input_key": f"rename_input_{session}"},
                )
                if meta["pinned"]:
                    st.button(
                        "取消置顶",
                        key=f"unpin_{session}",
                        width="stretch",
                        icon="📌",
                        on_click=lambda s=session: set_pinned(s, False),
                    )
                else:
                    st.button(
                        "置顶",
                        key=f"pin_{session}",
                        width="stretch",
                        icon="📌",
                        on_click=lambda s=session: set_pinned(s, True),
                    )
                with st.popover(
                    "删除会话",
                    icon="🗑️",
                    width="stretch",
                    type="primary",
                    key=f"delete_menu_{session}",
                ):
                    st.button(
                        "确认删除",
                        key=f"confirm_delete_{session}",
                        width="stretch",
                        type="primary",
                        icon="✖",
                        on_click=lambda s=session: delete_session(s),
                    )


    st.divider()

    # 角色管理：控件 key 直接就是状态键，不需要手工回写（回写会在控件实例化后
    # 修改同名 session_state，触发 StreamlitWidgetAlreadyInstantiatedError）
    st.subheader("管理角色")
    st.text_input("昵称", key="nickname", placeholder="请输入昵称")
    st.text_area("性格", key="nature", placeholder="请输入性格描述")
    st.text_area("角色简介", key="role_description", placeholder="请输入角色简介")
    st.text_area("输出规则", key="output_rules", placeholder="请输入输出规则")

    st.divider()

    # 生成参数
    st.subheader("生成参数")
    st.toggle(
        "深度思考",
        key="thinking",
        help="开启后模型会先推理再回答（推理过程显示在可折叠的「思考过程」里），速度更慢但更严谨；关闭则直接作答。",
    )

    # 高级配置：默认折叠，点开才展开（展开状态会被记住）
    with st.expander("⚙️ 高级配置", expanded=False, key="advanced_panel"):
        st.slider(
            "温度 (temperature)",
            min_value=0.0,
            max_value=2.0,
            step=0.05,
            key="temperature",
            help="越高越天马行空、越低越稳定保守。角色扮演想有个性可以调高，想稳定复现就调低。",
        )
        st.slider(
            "核采样 (top_p)",
            min_value=0.0,
            max_value=1.0,
            step=0.05,
            key="top_p",
            help="与温度二选一调即可。一般固定温度、把 top_p 留在 1.0。",
        )
        st.slider(
            "重复惩罚 (frequency_penalty)",
            min_value=-2.0,
            max_value=2.0,
            step=0.1,
            key="frequency_penalty",
            help="正值会降低重复用词的概率，负值鼓励复读。",
        )
        st.slider(
            "话题新鲜度 (presence_penalty)",
            min_value=-2.0,
            max_value=2.0,
            step=0.1,
            key="presence_penalty",
            help="正值鼓励聊新话题，负值鼓励围绕已有内容展开。",
        )
        st.checkbox("限制单次回复长度", key="limit_tokens", help="不勾选则由服务端决定上限。")
        if st.session_state.limit_tokens:
            st.number_input(
                "最大 tokens",
                min_value=256,
                max_value=32768,
                step=256,
                key="max_tokens",
                help="单次回答（不含思考过程）的长度上限。",
            )
        st.button("恢复默认值", width="stretch", on_click=reset_advanced)


# --------------------------------------------------------------------------- #
# 对话框
# --------------------------------------------------------------------------- #
prompt = st.chat_input("请输入您的想法...")

if prompt:
    st.session_state.message.append({"role": "user", "content": prompt})  # 用户输入入列
    st.chat_message("user").write(prompt)
    # 复用同一个占位符，避免每个 chunk 都重建组件
    render_reply(st.empty())
