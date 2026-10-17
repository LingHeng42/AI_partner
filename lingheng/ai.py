"""模型调用与对话流程：客户端、上下文拼装、流式生成、重新生成、编辑消息。"""

import json
import sys

import streamlit as st
from openai import OpenAI

from . import auth, branches, config, images, profile, sessions, storage, store

# 当前使用的模型客户端。
#
# ⚠️ 这里只能当**兜底**，不能当"当前会话的客户端"。模块级全局是所有浏览器会话、
# 所有线程共用的：两个人（或同一个人的两个标签页）同时用时，后一次 ui.main() 的
# configure_client() 会把前一个人**正在流式请求**的客户端换掉，于是"A 的提问用
# B 的 Key 发出去"——花错人的钱，还把 A 的提问记到 B 的额度上。
# 这跟 `users/local` 是同一类 bug：**身份不能存进程级全局**。
# 真正的存放处是浏览器会话自己的 session_state（见 CLIENT_STATE），
# 这个全局只留给"不在脚本上下文里"的调用方（工具脚本、测试里的老写法）。
client = None

# 本次浏览器会话要用的模型客户端（存在 session_state 里，按会话隔离）
CLIENT_STATE = "_model_client"

# 用户自己填的 DeepSeek Key 存在这里（**只在内存里**，不落盘、不进数据库、不进日志）。
USER_KEY_STATE = "user_api_key"


def configure_client(new_client) -> None:
    """设置**本次浏览器会话**使用的客户端（ui.main() 每次重跑都会调一次）。

    写 session_state 而不是模块全局（原因见上面 client 的说明）。
    取不到 session_state 时（不在脚本上下文里，例如工具脚本）才退回模块全局，
    保证老用法仍然可用。
    """
    global client
    try:
        st.session_state[CLIENT_STATE] = new_client
        return
    except Exception:  # noqa: BLE001 - 不在 Streamlit 脚本上下文里（测试/工具）
        client = new_client


def _client():
    """取模型客户端。优先级：入口模块的覆盖值 → 本次会话的 → 模块全局兜底。

    第一优先是"入口模块上的覆盖值"：测试会写 ``app.client = 假客户端``，而 Python 的
    模块不支持 __setattr__，那个赋值只落在入口模块自己的 __dict__ 里，所以要回头看一次。
    """
    entry = sys.modules.get("AI_partner")
    if entry is not None:
        override = getattr(entry, "client", None)
        if override is not None:
            return override
    try:
        mine = st.session_state.get(CLIENT_STATE)
    except Exception:  # noqa: BLE001 - 不在脚本上下文里
        mine = None
    if mine is not None:
        return mine
    return client


def user_api_key() -> str:
    """用户在当前会话里填的 key（只存在 session_state，浏览器刷新即丢弃）。"""
    return str(st.session_state.get(USER_KEY_STATE) or "").strip()


def resolve_api_key(username: str = None) -> tuple:
    """决定这次调用用谁的 key。

    返回 ``(key, source)``，source 取 "user" / "owner" / "none"：

    1. 用户自己填了 → 用他的（**优先**：即使被授权用站长 key，也先花自己的）
    2. 在允许名单里 → 用站长的
    3. 没配登录（本地开发）→ 用站长的（**仅此一种放宽**）
    4. 其余（线上已配登录但不在名单里）→ "none"，谁的钱都不花

    第 3 条是必要的：本地没配登录时用户名是 "local"，永远不在名单里，
    如果这里也 fail-closed，本地开发就拿不到 key。线上配了登录就一定走第 2/4 条，
    所以"名单写错导致全员花站长钱"不会发生。
    """
    config.load_dotenv_file()  # 本地开发时把 .env 读进环境变量
    own = user_api_key()
    if own:
        return own, "user"
    name = str(username if username is not None else auth.current_user_id()).strip()
    if not auth.auth_enabled() or auth.can_use_owner_key(name):
        owner = config.owner_api_key()
        if owner:
            return owner, "owner"
    return "", "none"


def api_key_error_message(station_owner_allowed: bool = False) -> str:
    """没 key 可用时给用户看的提示（说清怎么解决，而不是只报错）。"""
    if station_owner_allowed:
        return (
            "**没有可用的 API Key。** 请在侧边栏「API Key」里填入你自己的 DeepSeek Key，"
            "或者联系站长把你加进允许名单。"
        )
    return (
        "**还没有配置 API Key。** 请在侧边栏「API Key」里填入你自己的 DeepSeek Key"
        "（只保存在本次会话的内存里，不会写到服务器上）。"
    )


def get_client(api_key: str) -> OpenAI:
    """按 key 构造客户端。

    **故意不加 st.cache_resource**：那个缓存是全局的，会把每个用户的 key 留在
    服务器内存里（用户撤销 key 后还可能残留）。OpenAI 客户端本身很轻，每次重跑
    新建一个的代价可以忽略。
    """
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
    """把用户手动展开/折叠的状态镜像到状态键。

    注意：**现在这条路只在重跑之后才走到**（on_change="ignore" 不触发重跑，
    见 reasoning_expander 的说明），所以它其实是个兜底；真正生效的是渲染时
    从控件键把状态读回来。
    """
    if widget_key in st.session_state:
        st.session_state[state_key] = st.session_state[widget_key]


def reasoning_expander(key: str, state_key: str, expanded: bool):
    """带记忆的「思考过程」折叠面板。

    type="compact" 是 Streamlit 官方推荐的思考块样式（无边框内联展开）。

    **关键：on_change 必须是 "ignore"。** 这个面板在生成过程中是可见的，用户
    很自然会在"思考中"顺手点它开合。以前这里是 on_change 回调 —— 那会触发一次
    重跑，而重跑会**打断正在进行的流式请求**：

      · 被打断的那一轮，助手回答还没写进 message（remember_reply 还没执行）
      · st.write_stream 被中断时**静默返回空字符串**（不抛异常），
        于是 remember_reply 认为"模型没有返回任何内容"，什么都不记录
      · 重跑后 message 里只剩用户那一句 → **整条回答连同思考内容一起消失**

    这就是用户报的"思考时点折叠面板，那条消息会消失"。用 "ignore" 之后，
    这个控件只改自己的状态、不重跑，流不会被打断。
    """
    # 注意：不能用 st.session_state.setdefault —— 真实的 session_state 是代理对象，
    # 没有这个方法，属性访问会变成"读取同名键"并抛 KeyError（线上白屏的原因之一）。
    if state_key not in st.session_state:
        st.session_state[state_key] = expanded
    # on_change="ignore" 不会给我们回调，所以每次渲染时把"用户上次点出来的状态"
    # 从控件键读回来（控件键由 Streamlit 自己维护，跨重跑都在）。
    elif key in st.session_state:
        st.session_state[state_key] = st.session_state[key]
    return st.expander(
        "思考过程",
        expanded=st.session_state[state_key],
        key=key,
        type="compact",
        icon=":material/neurology:",
        # 不触发重跑：既避免打断流式生成，也避免"点一下就跑一轮"的开销
        on_change="ignore",
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
    """用户发消息后的回答：生成 → 追加到当前分支 → 落盘 → **统一重跑一次**。

    "最新一轮没有 ⋯"的根因就在这个函数：历史回放（render_history）在本轮脚本里
    是**在用户这条消息之前**跑的，它不可能画到刚生成出来的回答；而辅助控件
    （⋯ 菜单 / 分支箭头 / 重新生成）只在历史回放里画。于是最新一轮永远缺这些入口，
    必须等下一次重跑（切一次会话、刷新、或再发一条）才补上。

    修法是让两个时候**渲染同一套东西**：生成并落盘后主动重跑一次，下一轮由历史
    回放统一画出完整的、带控件的消息列表。副作用是"内容不再重复显示" —— 流式
    内容随本轮结束被清理掉，正好不会和回放出来的内容重叠。

    成本很低：重跑不重新请求模型（生成已经结束、内容已落盘），只是把同一份内容
    再渲染一次；这也是新建会话时早就在走的同一条路。
    """
    full_response, reasoning = generate_reply(placeholder, st.session_state.message)
    remember_reply(full_response, reasoning)

    # 无论回答成功与否都先落盘：用户这条消息必须durable（尤其是新会话的第一条 ——
    # 不落盘的话这次失败会让整个会话连同用户输入一起消失）。
    sessions.save_session()

    if not full_response:
        # 生成失败：没有新回答，也就没有"最新一轮缺 ⋯"的问题；而且这里**不能重跑** ——
        # generate_reply 刚 st.error 出来的提示会随本轮作废，用户什么都看不到。
        return

    # 成功才重跑：让历史回放（render_history）在下一轮统一画出带 ⋯ / 分支箭头的
    # 完整消息列表 —— 它在本轮脚本里跑在用户这条消息之前，画不到刚生成的回答。
    st.rerun()
    return  # st.rerun() 正常会抛异常终止本轮；显式 return 让它在测试替身下也停下


def regenerate(index: int) -> None:
    """重新生成第 index 条（assistant）回复：新建分支，不覆盖原分支。

    作为按钮回调执行：只准备新分支与待生成标记，真正的请求交给页面主体
    （回调阶段不适合发起流式请求）。
    """
    _bind_callback_uid(f"重新生成第 {index} 条")
    sid = st.session_state.get("current_session")
    messages = st.session_state.get("message", [])
    if not sid or not (0 <= index < len(messages)):
        return
    prefix = [dict(m) for m in messages[:index]]
    # 分叉点显式沿用：用户连续点"重新生成"时，每次都是同一个分叉点上的另一种
    # 可能，必须都记成同一个 fork_point，否则它们不互为兄弟、‹ 2/3 › 箭头出不来
    # （这就是"分支似乎只能存在两个"的成因）。
    point = branches.fork_point_for(
        sid, st.session_state.get("current_branch") or "main", index)
    branch_id = branches.fork_branch(sid, prefix, seed=None,
                            parent=point, fork_index=index, fork_point=point)
    st.session_state.current_branch = branch_id
    st.session_state.message = [dict(m) for m in prefix]
    sessions.mark_message_owner(st.session_state.get("current_session"), branch_id)
    st.session_state.pending_regen = True
    # 记进"最近动作"面板：排查"点了没有分支"时，第一件事就是确认这个回调到底跑没跑
    sessions.note_action(
        f"回调：重新生成第 {index} 条 → 新建 {branch_id}"
        f"（分叉点 {point}／第 {index} 条）")


def start_edit(index: int) -> None:
    """进入某条消息的编辑态（在聊天气泡里就地编辑）。"""
    st.session_state.editing_index = index


def cancel_edit() -> None:
    st.session_state.editing_index = None


def _bind_callback_uid(what: str) -> bool:
    """回调开始时把当前用户定成"这次浏览器会话的用户"，返回身份是否可用。

    为什么：``set_current_uid`` 是 ``ui.main()`` 里调的，而控件回调跑在脚本主体
    **之前**、还可能落在另一个线程上。此时线程局部量可能是空的、或残留着别的会话
    （甚至别的账号）的用户名 —— 于是这里的写入（新建分支）会落进 ``users/local``
    或别人的分区。表现就是用户反复报的那句"**保存并重新生成，还是没有分支**"：
    日志说新建了 b2（写入确实成功了），界面上却永远只有 main（读的是自己的分区）。
    这种事以前完全静默，所以必须在动手写之前把身份定下来。

    实现只有一份（``sessions.bind_callback_uid``）：这里只是保留一个薄的转发，
    免得"怎么绑定身份"有两套代码各自演化。
    """
    sessions.bind_callback_uid(what)
    return True


def submit_edit(index: int, input_key: str) -> None:
    """保存编辑：新建分支（保留原分支），分叉点是"被编辑的这条"。

    与"重新生成"的区别只在于分叉点上放什么：
    - 编辑用户消息 → 新分支里放改写后的用户消息，然后生成新的回答
    - 编辑 AI 回复  → 新分支里放改写后的 AI 回复，不再额外生成
    这是按钮回调，Streamlit 在回调之后本来就会重跑一次，所以不调用 st.rerun()。
    """
    _bind_callback_uid(f"编辑第 {index} 条")
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
    # 编辑是"从这条分支出发分叉"（嵌套分叉）→ reuse_chain=False，
    # 分叉点就是当前分支本身。这样在 b3 上编辑它的第 0 条不会并进 main 组。
    point = branches.fork_point_for(
        sid, st.session_state.get("current_branch") or "main", index,
        reuse_chain=False)
    branch_id = branches.fork_branch(sid, prefix, seed=seed,
                            parent=point, fork_index=index, fork_point=point)
    st.session_state.current_branch = branch_id
    st.session_state.message = [*[dict(m) for m in prefix], dict(seed)]
    sessions.mark_message_owner(st.session_state.get("current_session"), branch_id)
    st.session_state.editing_index = None
    # 改的是用户消息 → 需要模型接着回答；改的是 AI 回复 → 他自己写了内容，不再生成
    st.session_state.pending_regen = seed.get("role") == "user"
    sessions.note_action(
        f"回调：编辑第 {index} 条 → 新建 {branch_id}"
        f"（分叉点 {point}／第 {index} 条，{seed.get('role')}）")


def render_pending_regen() -> None:
    """页面主体：处理"重新生成"标记（回调阶段不发起流式请求）。

    和 render_reply 一样，生成完统一重跑一次：历史回放是在脚本顶部跑的，那时这条
    新回答还不存在，不重跑就会出现"有内容、但没有下面的 ⋯ 操作入口"。
    """
    if not st.session_state.get("pending_regen"):
        return
    st.session_state.pending_regen = False
    full_response, reasoning = generate_reply(st.empty(), st.session_state.message)
    remember_reply(full_response, reasoning)
    if not full_response:
        # 生成失败：没有新消息可回放，也就没有"缺少 ⋯"的问题；而且**不能重跑**，
        # 否则 generate_reply 刚 st.error 出来的提示会随本轮作废。
        return
    sessions.save_session()
    st.rerun()


def render_history() -> None:
    """按存储顺序回放历史，并给出每条消息的操作入口。

    - 每条消息（用户与 AI）都能「编辑」，保存后**新建分支**并从该位置重新生成
    - 当前分支的最后一条助手回答额外提供「重新生成」与分支切换箭头
    控件 key 里必须带**分支名 + 消息序号**：只用序号的话，切换分支后会串到
    另一条分支的同序号消息上（与"删会话后弹层串位"是同一类问题）。
    """
    branch = st.session_state.get("current_branch") or "main"
    siblings = _siblings_for(branch)
    last = len(st.session_state.message) - 1
    forked_at = _fork_index_for(branch) if len(siblings) > 1 else None
    if len(siblings) > 1 and forked_at is None:
        # 兄弟组是有的，但"从第几条分的"算不出来（老存档连 fork_index 都没有）。
        # 这时画在最后一条上：位置不精确，但远好过"整个没有切换入口" ——
        # 用户会把完全看不到箭头理解成"编辑根本没建分支"。
        forked_at = last
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
                _render_message_actions(branch, index, last, siblings, forked_at)

def _siblings_for(branch: str) -> list:
    """当前分支所在的分叉组（没有存档时就是它自己）。"""
    if sessions.session_file_exists():
        return branches.branch_siblings(st.session_state.current_session, branch)
    return [branch]


def _fork_index_for(branch: str) -> int | None:
    """当前兄弟组在哪一条消息处分叉（箭头只画在那一条下面）。

    算不出来时返回 None（调用方会退化成"画在最后一条"），**不能**返回 0 或 -1：
    那样会在不存在的序号上比较，``index == forked_at`` 永远不成立 → 箭头一个都不画。
    """
    if not sessions.session_file_exists():
        return None
    return branches.group_fork_index(st.session_state.current_session, branch)


def _render_message_actions(branch: str, index: int, last: int, siblings: list,
                            forked_at: int) -> None:
    """一条消息的操作入口：⋯ 菜单（编辑/重新生成）与分支切换箭头。

    **必须由"历史回放"和"刚刚生成完的那轮"共用。** 以前只有历史回放里画这些，
    于是刚发完消息时最新一轮没有 ⋯ —— 要等下一次重跑（比如切一次会话）才补上。

    ⋯ 与切换箭头放在**同一个横向容器**里（一行）：以前 ⋯ 一行、箭头另起一行
    （还占满整行宽度），每轮消息都要多占一份竖向空间，看着像两组不相干的控件。
    横向容器里子元素按内容宽度排列，所以这里不用再套 st.columns。
    """
    with st.container(horizontal=True, vertical_alignment="center"):
        with st.popover("⋯", key=f"msg_menu_{branch}_{index}",
                        help="编辑这条消息；最后一条回答还能重新生成"):
            if index == last and st.session_state.message[index].get("role") == "assistant":
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
        if index == forked_at:
            _render_branch_arrows(branch, index, siblings)


def _render_branch_arrows(branch: str, index: int, siblings: list) -> None:
    """分支切换箭头（‹ 2/3 ›）：只在同一个分叉点的兄弟之间循环。

    必须走 pending 路由（只登记、主脚本体里执行）：switch_branch 会改
    st.session_state.current_branch，而回调里的状态变更会被 Streamlit 回滚 ——
    直接当回调用会出现"点了箭头没反应/内容不对"。

    只有**分叉点那一条消息**下面才画（forked_at）。以前是每条消息都画，
    等于说"后面每一轮也各自分了叉"，既误导又更容易撞控件 key。

    不自己开一行：调用方已经把它放进横向容器，和 ⋯ 排在同一行（以前这里用
    ``st.columns([1, 2, 1])`` 独占一整行，箭头会被撑到消息两端的对角上）。
    这里按「‹ 2/3 ›」的阅读顺序直接摆三个子元素，宽度由内容决定。
    """
    if len(siblings) <= 1:
        return
    position = siblings.index(branch) if branch in siblings else 0
    prev_branch = siblings[(position - 1) % len(siblings)]
    next_branch = siblings[(position + 1) % len(siblings)]
    st.button(
        "", key=f"branch_prev_{branch}_{index}",
        icon=":material/chevron_left:",
        help=f"切到 {prev_branch}",
        on_click=sessions.request_switch_branch,
        args=(st.session_state.current_session, prev_branch),
    )
    st.caption(f"分支 {position + 1}/{len(siblings)}", width="content")
    st.button(
        "", key=f"branch_next_{branch}_{index}",
        icon=":material/chevron_right:",
        help=f"切到 {next_branch}",
        on_click=sessions.request_switch_branch,
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
