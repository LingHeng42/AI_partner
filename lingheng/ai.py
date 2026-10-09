"""模型调用与对话流程：客户端、上下文拼装、流式生成、重新生成、编辑消息。"""

import json
import os
import sys

import streamlit as st
from openai import OpenAI

from . import branches, config, images, profile, sessions, store

# 当前使用的模型客户端。由 ui.main() 每次重跑时通过 configure_client() 设置；
# 测试可以直接替换它（或替换入口模块上的 client，见 _client()）。
client = None


def configure_client(new_client) -> None:
    """设置本次脚本运行使用的客户端（ui.main() 每次重跑都会调一次）。"""
    global client
    client = new_client


def _client():
    """取模型客户端。

    优先用"入口模块上的覆盖值"：测试会写 ``app.client = 假客户端``，而 Python 的
    模块不支持 __setattr__，那个赋值只落在入口模块自己的 __dict__ 里，所以要回头看一次。
    """
    entry = sys.modules.get("AI_partner")
    if entry is not None:
        override = getattr(entry, "client", None)
        if override is not None:
            return override
    return client



def require_api_key() -> str:
    """每次脚本运行都校验一次 API Key，缺失就停在这一步并给出可操作的提示。"""
    config.load_dotenv_file()
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
        if kept and (len(kept) >= config.MAX_CONTEXT_MESSAGES or used + cost > config.MAX_CONTEXT_CHARS):
            dropped = True
            break
        kept.append(msg)
        used += cost
    if dropped:
        st.caption("⚠️ 对话较长，本次请求只发送了最近的部分历史记录。")
    return list(reversed(kept))


def build_messages(messages: list = None) -> list:
    """拼装请求消息：system prompt + 截断后的历史。

    messages 省略时用当前会话的消息；重新生成时传入新分支的消息列表。
    """
    system_content = config.SYSTEM_PROMPT.format(**profile.profile_from_state())
    source = st.session_state.message if messages is None else messages
    history = []
    for msg in trim_context(source):
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
    return st.session_state.get(name, config.DEFAULT_ADVANCED[name])


def advanced_from_state() -> dict:
    """当前的高级生成参数快照（存进会话存档用）。"""
    return {key: st.session_state.get(key, config.DEFAULT_ADVANCED[key]) for key in config.ADVANCED_KEYS}


def avatar_from_state() -> dict:
    """当前会话的头像设置快照（每个会话独立，存进会话存档用）。

    注意：头像是纯展示用的，**不会**进入请求，也不会发给模型。
    """
    return {key: st.session_state.get(key) for key in config.AVATAR_KEYS}


def apply_advanced(data: dict) -> None:
    """用存档里的高级参数覆盖当前设置；缺失的键回退成默认值。

    只能从回调里调用（回调先于控件实例化），否则改这些已被滑块占用的 key 会报
    StreamlitWidgetAlreadyInstantiatedError。
    """
    for key, default in config.DEFAULT_ADVANCED.items():
        value = data.get(key, default)
        st.session_state[key] = default if value is None else value


def build_request_payload(messages: list = None) -> dict:
    """按侧边栏的高级配置拼装请求参数，未启用的项不发送。"""
    payload = {
        "model": config.MODEL_NAME,
        "messages": build_messages(messages),
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
    """带记忆的「思考过程」折叠面板：用户手动开合后不会被流式重跑重置。

    type="compact" 是 Streamlit 官方推荐的思考块样式（无边框内联展开），
    与 ChatGPT / DeepSeek 的「思考过程」观感一致。
    """
    st.session_state.setdefault(state_key, expanded)
    return st.expander(
        "思考过程",
        expanded=st.session_state[state_key],
        key=key,
        type="compact",
        icon=":material/neurology:",
        on_change=_remember_expander,
        args=(key, state_key),
    )


def generate_reply(placeholder, messages: list) -> tuple:
    """请求模型并把流式结果渲染进 placeholder，返回 (正文, 推理内容)。

    正文交给 st.write_stream（官方推荐的流式渲染方式）；推理片段在同一趟迭代里
    顺带写进「思考过程」折叠面板，所以两种内容仍然是边收边显示。
    异常在这里被消化成 st.error，调用方（页面脚本）不再需要 try/except。

    注意：**不把回答写进 st.session_state.message**，也不落盘——那由调用方决定，
    因为重新生成与编辑消息需要先把新分支准备好、再决定怎么写入。
    """
    reasoning = ""
    full_response = ""  # 异常路径下也要有定义
    thinking_enabled = bool(st.session_state.thinking)
    reasoning_text = None
    try:
        response = _client().chat.completions.create(**build_request_payload(messages))

        def stream_body():
            """边消费模型流边渲染：先出推理，再流式吐正文。"""
            nonlocal reasoning
            for chunk in response:
                reasoning_piece, content_piece = _delta_text(chunk)
                if reasoning_piece:
                    reasoning += reasoning_piece
                    if thinking_enabled:
                        # 推理内容只更新占位符，不重建整条消息
                        reasoning_text.markdown(
                            format_reasoning_html(reasoning, streaming=True),
                            unsafe_allow_html=True,
                        )
                if content_piece:
                    if thinking_enabled and reasoning_text is not None:
                        # 思考结束：撤掉"思考中"的微光提示，把完整推理定格
                        if reasoning:
                            reasoning_text.markdown(
                                format_reasoning_html(reasoning),
                                unsafe_allow_html=True,
                            )
                        else:
                            reasoning_text.markdown("_（本次没有输出推理内容）_")
                    yield content_piece

        with placeholder.chat_message("assistant", avatar=images.current_avatar("assistant")):
            if thinking_enabled:
                with reasoning_expander("reasoning_panel", "show_reasoning", True):
                    reasoning_text = st.empty()
                    # 官方推荐的进行中提示：微光文字本身就是状态指示
                    reasoning_text.markdown(":shimmer[思考中…]")
            # 正文渲染交给 st.write_stream（占位容器保留在聊天气泡里）。
            # 没有产出时它返回空列表，统一成空字符串便于后续判断
            full_response = st.write_stream(stream_body()) or ""
    except Exception as e:  # noqa: BLE001 - 网络/限流/余额等异常不该把页面打成 traceback
        placeholder.empty()
        st.error(f"请求模型失败：{e}")
        st.caption("本条消息已保留在对话中，可直接重试或继续输入。")
    return full_response, reasoning


def remember_reply(full_response: str, reasoning: str) -> None:
    """把一次生成的回答追加进当前消息列表（内存）。"""
    if not full_response:
        st.warning("模型没有返回任何内容，本次回答未记录。")
        return
    reply = {"role": "assistant", "content": full_response}
    if reasoning:
        reply["reasoning_content"] = reasoning
    st.session_state.message.append(reply)


def render_reply(placeholder) -> None:
    """用户发消息后的回答：生成 → 追加到当前分支 → 落盘。"""
    full_response, reasoning = generate_reply(placeholder, st.session_state.message)
    remember_reply(full_response, reasoning)

    existed = sessions.session_file_exists()
    sessions.save_session()
    if not existed and sessions.session_file_exists():
        # 本次是这条会话的第一个存档：侧边栏是在本轮脚本顶部渲染的，那时文件还
        # 不存在，所以必须主动重跑一次，会话才会立刻出现在会话历史里。
        # （不能只依赖 chat_input 提交后浏览器的隐式重跑，实测它不保证发生。）
        st.rerun()


def regenerate(index: int) -> None:
    """重新生成第 index 条（assistant）回复：新建分支，不覆盖原分支。

    作为按钮回调执行：只准备新分支与待生成标记，真正的请求交给页面主体
    （回调阶段不适合发起流式请求）。
    """
    sid = st.session_state.get("current_session")
    messages = st.session_state.get("message", [])
    if not sid or not (0 <= index < len(messages)):
        return
    prefix = [dict(m) for m in messages[:index]]
    branch_id = branches.fork_branch(sid, prefix, seed=None,
                            parent=st.session_state.get("current_branch") or "main",
                            fork_index=index)
    st.session_state.current_branch = branch_id
    st.session_state.message = [dict(m) for m in prefix]
    st.session_state._message_branch = branch_id
    st.session_state.pending_regen = True


def start_edit(index: int) -> None:
    """进入某条消息的编辑态（在聊天气泡里就地编辑）。"""
    st.session_state.editing_index = index


def cancel_edit() -> None:
    st.session_state.editing_index = None


def submit_edit(index: int, input_key: str) -> None:
    """保存编辑：新建分支（保留原分支），分叉点是"被编辑的这条"。

    与"重新生成"的区别只在于分叉点上放什么：
    - 编辑用户消息 → 新分支里放改写后的用户消息，然后生成新的回答
    - 编辑 AI 回复  → 新分支里放改写后的 AI 回复，不再额外生成
    这是按钮回调，Streamlit 在回调之后本来就会重跑一次，所以不调用 st.rerun()。
    """
    sid = st.session_state.get("current_session")
    messages = st.session_state.get("message", [])
    if not sid or not (0 <= index < len(messages)):
        return
    new_text = (st.session_state.get(input_key) or "").strip()
    if not new_text:
        st.error("消息内容不能为空")
        return
    prefix = [dict(m) for m in messages[:index]]
    seed = dict(messages[index])
    seed["content"] = new_text
    seed.pop("reasoning_content", None)  # 改写过就不再保留旧的推理过程
    branch_id = branches.fork_branch(sid, prefix, seed=seed,
                            parent=st.session_state.get("current_branch") or "main",
                            fork_index=index)
    st.session_state.current_branch = branch_id
    st.session_state.message = [*[dict(m) for m in prefix], dict(seed)]
    st.session_state._message_branch = branch_id
    st.session_state.editing_index = None
    # 改的是用户消息 → 需要模型接着回答；改的是 AI 回复 → 他自己写了内容，不再生成
    st.session_state.pending_regen = seed.get("role") == "user"


def render_pending_regen() -> None:
    """页面主体：处理"重新生成"标记（回调阶段不发起流式请求）。"""
    if not st.session_state.get("pending_regen"):
        return
    st.session_state.pending_regen = False
    full_response, reasoning = generate_reply(st.empty(), st.session_state.message)
    remember_reply(full_response, reasoning)
    if not full_response:
        # 生成失败：保留"前缀 + 分叉点内容"这个状态，用户可以直接重试
        return
    sessions.save_session()
    # 历史是在脚本顶部渲染的，那时这条新回答还没生成：不重跑一次的话，
    # 它会带着内容显示出来、但没有下面的 ⋯ 操作入口（要刷新才出现）。
    st.rerun()


def render_history() -> None:
    """按存储顺序回放历史，并给出每条消息的操作入口。

    - 每条消息（用户与 AI）都能「编辑」，保存后**新建分支**并从该位置重新生成
    - 当前分支的最后一条助手回答额外提供「重新生成」与分支切换箭头
    控件 key 里必须带**分支名 + 消息序号**：只用序号的话，切换分支后会串到
    另一条分支的同序号消息上（与"删会话后弹层串位"是同一类问题）。
    """
    branch = st.session_state.get("current_branch") or "main"
    siblings = branches.branch_siblings(st.session_state.current_session, branch) if sessions.session_file_exists() else [branch]
    last = len(st.session_state.message) - 1
    for index, msg in enumerate(st.session_state.message):
        with st.chat_message(msg["role"], avatar=images.current_avatar(msg["role"])):
            if msg.get("reasoning_content"):
                with reasoning_expander(f"history_reasoning_{branch}_{index}",
                                        f"history_reasoning_open_{branch}_{index}", False):
                    # 引用块，原生md，不存在html懒渲染问题
                    st.markdown(
                        format_reasoning_html(msg["reasoning_content"]),
                        unsafe_allow_html=True,
                    )

            if st.session_state.get("editing_index") == index:
                draft = st.text_area(
                    "编辑这条消息",
                    value=msg.get("content", ""),
                    key=f"edit_box_{branch}_{index}",
                    height=140,
                    help="保存后会新建一条分支，并从这条消息之后重新生成；原分支会保留。",
                )
                save_col, cancel_col = st.columns(2)
                with save_col:
                    st.button(
                        "保存并重新生成",
                        key=f"edit_save_{branch}_{index}",
                        width="stretch",
                        type="primary",
                        icon=":material/check:",
                        on_click=submit_edit,
                        args=(index, f"edit_box_{branch}_{index}"),
                    )
                with cancel_col:
                    st.button(
                        "取消",
                        key=f"edit_cancel_{branch}_{index}",
                        width="stretch",
                        icon=":material/close:",
                        on_click=cancel_edit,
                    )
            else:
                if msg.get("content"):
                    st.markdown(msg["content"])
                with st.popover("⋯", key=f"msg_menu_{branch}_{index}",
                                help="编辑这条消息；最后一条回答还能重新生成"):
                    if index == last and msg.get("role") == "assistant":
                        st.button(
                            "重新生成",
                            key=f"regen_{branch}_{index}",
                            width="stretch",
                            icon=":material/refresh:",
                            on_click=regenerate,
                            args=(index,),
                        )
                    st.button(
                        "编辑这条",
                        key=f"edit_{branch}_{index}",
                        width="stretch",
                        icon=":material/edit:",
                        on_click=start_edit,
                        args=(index,),
                    )
                    # 分支切换箭头：只在同源分支之间循环（‹ 2/3 ›）
                    if len(siblings) > 1:
                        position = siblings.index(branch) if branch in siblings else 0
                        prev_branch = siblings[(position - 1) % len(siblings)]
                        next_branch = siblings[(position + 1) % len(siblings)]
                        nav = st.columns([1, 2, 1], vertical_alignment="center")
                        with nav[0]:
                            st.button(
                                "", key=f"branch_prev_{branch}_{index}",
                                icon=":material/chevron_left:",
                                help=f"切到 {prev_branch}", on_click=branches.switch_branch,
                                args=(st.session_state.current_session, prev_branch),
                            )
                        with nav[1]:
                            st.caption(f"分支 {position + 1}/{len(siblings)}")
                        with nav[2]:
                            st.button(
                                "", key=f"branch_next_{branch}_{index}",
                                icon=":material/chevron_right:",
                                help=f"切到 {next_branch}", on_click=branches.switch_branch,
                                args=(st.session_state.current_session, next_branch),
                            )

def format_reasoning_html(text: str, streaming: bool = False) -> str:
    """把推理内容包装成紧凑样式的 HTML，流式时带光标。"""
    cursor = "▌" if streaming else ""
    escaped = (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
    return (
        '<div style="font-size: 0.875rem; line-height: 1.6; '
        'color: var(--text-color); opacity: 0.75; '
        'white-space: pre-wrap; font-family: inherit;">'
        f'{escaped}{cursor}</div>'
    )
