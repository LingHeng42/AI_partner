"""页面渲染：主区域历史回放与操作入口、侧边栏（会话/分支/头像/人设/参数）。"""

import streamlit as st

from . import ai, branches, config, images, profile, sessions, store



# --------------------------------------------------------------------------- #
# 页面配置（必须是第一个 Streamlit 命令）
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="凌恒的酒馆",
    page_icon=str(config.LOGO_PATH) if config.LOGO_PATH.exists() else ":material/local_bar:",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Report a bug": "https://github.com/LingHeng42/AI_partner/issues",
        "About": "凌恒的酒馆是一个基于人工智能的聊天平台，旨在为用户提供一个有趣、互动和智能的聊天体验。",
    },
)

# 会话状态初始化
for _key, _value in config.DEFAULT_PROFILE.items():
    st.session_state.setdefault(_key, _value)
for _key, _value in config.DEFAULT_ADVANCED.items():
    st.session_state.setdefault(_key, _value)
st.session_state.setdefault("message", [])
st.session_state.setdefault("thinking", False)
st.session_state.setdefault("session_title", store.DEFAULT_SESSION_TITLE)
# 对话头像：每个会话独立，未设置时为 None（用 Streamlit 的默认头像）
for _key in config.AVATAR_KEYS:
    st.session_state.setdefault(_key, None)
# 当前所在分支（一条分支 = 一条线性消息列表；切换分支就是整体换掉 message）
st.session_state.setdefault("current_branch", "main")
# 内存里的 message 属于哪条分支（渲染时据此判断要不要从磁盘重载）
st.session_state.setdefault("_message_branch", st.session_state.current_branch)
# 「重新生成」标记：按钮回调里只做标记，流式请求交给页面主体
st.session_state.setdefault("pending_regen", False)
# 正在就地编辑第几条消息（None = 没有在编辑）
st.session_state.setdefault("editing_index", None)
if "current_session" not in st.session_state:
    st.session_state.current_session = store.new_session_id()

# 每次脚本运行都校验 Key（函数本身不缓存），缓存只作用在客户端构造上
client = ai.get_client(ai.require_api_key())

# logo（文件缺失时不影响页面）
# if LOGO_PATH.exists():
#     try:
#         st.logo(str(LOGO_PATH), size="large")
#     except Exception:  # noqa: BLE001 - 老版本 streamlit 没有 st.logo
#         pass

# 展示历史对话（开启过深度思考的回答会带上可折叠的思考过程）
st.header(st.session_state.session_title)
ai.render_history()

# 上一轮点了「重新生成」：在这里真正发起流式请求（回调阶段不适合做流式渲染）
ai.render_pending_regen()

# --------------------------------------------------------------------------- #
# 侧边栏
# --------------------------------------------------------------------------- #
with st.sidebar:
    # 标题：小号字放在 logo 右侧
    logo_col, title_col = st.columns([1, 6], vertical_alignment="center")
    with logo_col:
        if config.LOGO_PATH.exists():
            try:
                st.image(str(config.LOGO_PATH), width=64)
            except Exception:  # noqa: BLE001 - 图片渲染失败不影响功能
                pass
    with title_col:
        st.markdown("### 凌恒的酒馆")
        st.caption("AI 角色扮演聊天")

    st.divider()

    # 新建会话：状态改动全部放在 on_click 回调里（回调先于控件实例化执行）。
    # 处于"新建会话"状态（还没有产生对话）时用主色高亮，产生对话、会话进入历史后
    # 自动恢复成普通按钮
    st.button(
        "新建会话",
        width="stretch",
        icon=":material/add_comment:",
        type="primary" if sessions.is_fresh_session() else "secondary",
        on_click=sessions.new_session,
    )

    # 会话名称：与存档文件名（会话 ID）解耦，改名不会新建/移动文件
    # 注意：控件 key 直接占用 "session_title"，所以只能在回调里改这个键
    # st.text_input(
    #     "会话名称",
    #     value=st.session_state.session_title,
    #     key="session_title",
    #     placeholder=DEFAULT_SESSION_TITLE,
    #     help="只影响显示，不会改变存档文件名（文件名始终是会话 ID 时间戳）。",
    # )

    st.subheader("会话历史")
    session_list = sessions.load_session_list()
    if not session_list:
        st.caption("还没有会话。发出第一条消息后，这里会出现本次会话。")
    for session in session_list:
        current = session == st.session_state.current_session
        meta = sessions.load_session_meta(session)
        # 当前会话的名称/昵称直接取内存里的，避免与输入框内容不一致。
        # 「是不是当前会话」由按钮颜色（type=primary）表示，名称里不再加"（当前）"
        label = st.session_state.session_title if current else meta["title"]
        nickname = st.session_state.nickname if current else (meta.get("nickname") or config.DEFAULT_PROFILE["nickname"])
        pin_mark = ":material/push_pin: " if meta["pinned"] else ""
        col1, col2 = st.columns([4, 1])
        with col1:
            st.button(
                f"{pin_mark}{nickname}-{label}",
                width="stretch",
                key=f"session_{session}",
                help=f"会话 ID：{session}",
                type="primary" if current else "secondary",
                on_click=lambda s=session: sessions.load_selected_session(s),
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
                    on_change=sessions.rename_session,
                    args=(session,),
                    kwargs={"new_title": None, "input_key": f"rename_input_{session}"},
                )
                if meta["pinned"]:
                    st.button(
                        "取消置顶",
                        key=f"unpin_{session}",
                        width="stretch",
                        icon=":material/push_pin:",
                        on_click=lambda s=session: sessions.set_pinned(s, False),
                    )
                else:
                    st.button(
                        "置顶",
                        key=f"pin_{session}",
                        width="stretch",
                        icon=":material/keep:",
                        on_click=lambda s=session: sessions.set_pinned(s, True),
                    )
                with st.popover(
                    "删除会话",
                    icon=":material/delete:",
                    width="stretch",
                    type="primary",
                    key=f"delete_menu_{session}",
                ):
                    st.button(
                        "确认删除",
                        key=f"confirm_delete_{session}",
                        width="stretch",
                        type="primary",
                        icon=":material/delete_forever:",
                        on_click=lambda s=session: sessions.delete_session(s),
                    )

    # 分支管理：一个会话可以有多条分支（编辑消息 / 重新生成都会新建分支）
    sid = st.session_state.current_session
    if sessions.session_file_exists(sid):
        meta = sessions.load_session_meta(sid)
        disk_branch = meta.get("current_branch") or "main"
        st.session_state.current_branch = disk_branch
        # 以磁盘为准：如果内存里的消息还是另一条分支的，就把当前分支的消息读回来。
        # 这样"点击切换分支"只依赖回调写盘，不依赖回调改内存状态
        # （AppTest 等场景下，控件回调结束后内存状态可能被回滚到点击之前）。
        if st.session_state.get("_message_branch") != disk_branch:
            st.session_state.message = sessions.read_branch(sid, disk_branch).get("message", [])
            st.session_state._message_branch = disk_branch
        current_branch = disk_branch
        branch_list = sessions.branch_ids(sid)
        with st.expander(f"分支（{len(branch_list)}）", expanded=False, key="branch_expander"):
            if len(branch_list) == 1:
                st.caption("编辑消息或重新生成时会自动新建分支，旧分支会保留。")
            for branch in branch_list:
                active = branch == current_branch
                bcol1, bcol2 = st.columns([4, 1])
                with bcol1:
                    st.button(
                        branch,
                        key=f"branch_{sid}_{branch}",
                        width="stretch",
                        type="primary" if active else "secondary",
                        help=f"分支 {branch}",
                        on_click=branches.switch_branch,
                        args=(sid, branch),
                    )
                with bcol2:
                    with st.popover("⋯", width="stretch", help="重命名或删除这条分支",
                                    key=f"branch_menu_{sid}_{branch}"):
                        st.text_input(
                            "重命名分支",
                            value=branch,
                            key=f"branch_rename_{sid}_{branch}",
                            on_change=branches.rename_branch,
                            args=(sid, branch),
                            kwargs={"new_id": None, "input_key": f"branch_rename_{sid}_{branch}"},
                        )
                        st.button(
                            "删除分支",
                            key=f"branch_delete_{sid}_{branch}",
                            width="stretch",
                            icon=":material/delete:",
                            disabled=len(branch_list) <= 1,
                            on_click=branches.delete_branch,
                            args=(sid, branch),
                        )



    # 角色管理：控件 key 直接就是状态键；on_change 里立刻落盘，
    # 不需要手动回写（回写会在控件实例化后改同名 session_state 而报错）
    st.subheader("管理角色")
    with st.expander("头像设置", expanded=False, key="avatar_panel"):

        # 对话头像：每个会话独立设置，只影响聊天气泡左侧的显示，**不会发给模型**
        st.subheader("对话头像")
        for _key in config.AVATAR_KEYS:
            _current = images.avatar_file(st.session_state.current_session, _key)
            if _current is not None:
                _preview_col, _text_col = st.columns([1, 3], vertical_alignment="center")
                with _preview_col:
                    st.image(str(_current), width=48)
                with _text_col:
                    st.caption(f"已设置{config.AVATAR_LABELS[_key]}")
            st.file_uploader(
                config.AVATAR_LABELS[_key],
                type=["png", "jpg", "jpeg", "gif", "webp"],
                key=f"_uploader_{_key}",
                on_change=images.set_avatar,
                args=(_key,),
                help="只改变这个会话里的显示，图片保存在该会话的存档目录里。",
            )
            if _current is not None:
                st.button(
                    f"清除{config.AVATAR_LABELS[_key]}",
                    key=f"clear_avatar_{_key}",
                    width="stretch",
                    icon=":material/hide_image:",
                    on_click=images.clear_avatar,
                    args=(_key,),
                )

    with st.expander("角色设定", expanded=False, key="chara_panel"):
        st.text_input("昵称", key="nickname", placeholder="请输入昵称", on_change=sessions.save_session)
        st.text_area("性格", key="nature", placeholder="请输入性格描述", on_change=sessions.save_session)
        st.text_area("角色简介", key="role_description", placeholder="请输入角色简介", on_change=sessions.save_session)
        st.text_area("输出规则", key="output_rules", placeholder="请输入输出规则", on_change=sessions.save_session)


    # 生成参数
    st.subheader("生成参数")
    st.toggle(
        "深度思考",
        key="thinking",
        help="开启后模型会先思考再回答；关闭则直接作答。",
    )

    # 高级配置：默认折叠，点开才展开（展开状态会被记住）。
    # 每个控件都挂 on_change=save_session：改完立刻写进存档/草稿
    with st.expander("⚙️ 高级配置", expanded=False, key="advanced_panel"):
        st.slider(
            "温度 (temperature)",
            min_value=0.0,
            max_value=2.0,
            step=0.05,
            key="temperature",
            on_change=sessions.save_session,
            help="越高越天马行空、越低越稳定保守。角色扮演想有个性可以调高，想稳定复现就调低。",
        )
        st.slider(
            "核采样 (top_p)",
            min_value=0.0,
            max_value=1.0,
            step=0.05,
            key="top_p",
            on_change=sessions.save_session,
            help="top_p=1：全部词汇都参与采样，随机性最大；top_p 越小：候选词越少，输出越确定、保守",
        )
        st.slider(
            "重复惩罚 (frequency_penalty)",
            min_value=-2.0,
            max_value=2.0,
            step=0.1,
            key="frequency_penalty",
            on_change=sessions.save_session,
            help="正值会降低重复用词的概率，负值鼓励复读。",
        )
        st.slider(
            "话题新鲜度 (presence_penalty)",
            min_value=-2.0,
            max_value=2.0,
            step=0.1,
            key="presence_penalty",
            on_change=sessions.save_session,
            help="正值鼓励聊新话题，负值鼓励围绕已有内容展开。",
        )
        st.checkbox("限制单次回复长度", key="limit_tokens", on_change=sessions.save_session,
                    help="不勾选则由服务端决定上限。")
        if st.session_state.limit_tokens:
            st.number_input(
                "最大 tokens",
                min_value=256,
                max_value=32768,
                step=256,
                key="max_tokens",
                on_change=sessions.save_session,
                help="单次回答（不含思考过程）的长度上限。",
            )
        st.button("恢复默认值", width="stretch", on_click=sessions.reset_advanced_and_save)

# --------------------------------------------------------------------------- #
# 对话框
# --------------------------------------------------------------------------- #
prompt = st.chat_input(
    "请输入您的想法...",
    # 回答流式生成中禁用输入，避免误触打断（官方推荐的 chat 用法）
    submit_mode="disable",
)

if prompt:
    st.session_state.message.append({"role": "user", "content": prompt})  # 用户输入入列
    st.chat_message("user", avatar=images.current_avatar("user")).write(prompt)
    # 复用同一个占位符，避免每个 chunk 都重建组件
    ai.render_reply(st.empty())
