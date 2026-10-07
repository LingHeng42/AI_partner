# 凌恒的酒馆 🐳

一个基于 **Streamlit + DeepSeek** 的角色扮演聊天应用：可以自定义角色的昵称、性格、角色简介和输出规则，对话自动存档在本地，随时切换历史会话。

## 功能

- **角色扮演对话**：昵称 / 性格 / 角色简介 / 输出规则四项人设实时生效，注入 system prompt
- **深度思考 + 可折叠思考过程**：侧边栏一键切换；开启后模型的推理内容流式显示在「🤔 思考过程」折叠面板里（思考中自动展开、开始作答自动折叠），推理内容随会话存档，重开对话后仍可展开回看
- **高级配置**：侧边栏 `⚙️ 高级配置` 按钮展开，可调温度、top_p、重复惩罚、话题新鲜度，并可选择是否限制单次回复长度（max_tokens）
- **流式输出**：边生成边显示，附光标提示
- **本地会话存档**：存档文件名是会话 ID（时间戳），显示用的是可自定义的**会话名称**，支持新建 / 切换 / 二次确认删除；**空对话不落盘也不进历史**，有第一条消息后才出现
- **界面**：标题「凌恒的酒馆」以小号字放在侧边栏 logo 右侧，主区域全留给对话；会话历史只列出已经聊过的会话（空会话不显示），当前会话带「（当前）」标记
- **上下文窗口保护**：自动只发送最近 40 条、且总字符数不超过 12000 的历史
- **异常兜底**：接口报错、缺少 API Key 都只显示提示，不会把页面打成 traceback

## 快速开始

```bash
# 1. 准备环境（Python 3.10+）
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. 安装依赖
python -m pip install -r requirements.txt

# 3. 配置 API Key（二选一）
#    a) 系统环境变量：DEEPSEEK_API_KEY=sk-xxxx
#    b) 项目根目录新建 .env 文件：DEEPSEEK_API_KEY=sk-xxxx

# 4. 启动
streamlit run AI_partner.py
```

启动后浏览器会打开 `http://localhost:8501`。

## 项目结构

```
AI_partner.py        应用主程序（单文件）
resources/           图标与示例图
sessions/            对话存档（自动生成，已 gitignore）
tests/               测试（不依赖网络）
requirements.txt     依赖清单
```

## 测试

三个测试都不联网，也不会碰真实的 `sessions/`（它们用 `AI_PARTNER_SESSIONS_DIR`
把存档目录指到 `tests/.tmp` 下的隔离目录）：

```bash
.venv\Scripts\python.exe tests\run_tests.py          # 一次跑完三个套件（各起独立进程）
.venv\Scripts\python.exe tests\test_logic.py         # 存档 / 上下文截断 / 对话流程 / 重命名与置顶
.venv\Scripts\python.exe tests\test_smoke.py         # 页面渲染 / 空会话不建档 / 新建会话高亮
.venv\Scripts\python.exe tests\test_row_render.py    # 会话条目与「⋯」操作菜单渲染 / 回车重命名
.venv\Scripts\python.exe tests\clean_tmp.py          # 清掉测试临时目录 tests/.tmp
```

为什么用 `run_tests.py` 而不是挨个跑：Streamlit 的 `AppTest` 会复用 `sys.modules` 里
已缓存的 `AI_partner`，同一个进程里只有第一次运行会真正执行应用脚本，连着跑后面的
套件会拿到上一次的模块状态（表现为"单独跑通过、连着跑失败"）。`run_tests.py`
让每个套件各起一个干净进程，避免这个问题。

`tests/.tmp` 每次运行开头都会被清理，所以不会累积；需要立刻清掉时跑 `clean_tmp.py`。

## 会话存档格式

`sessions/<会话ID>.json`，会话 ID 形如 `2026-01-01_120000_000`（毫秒级时间戳）：

```json
{
  "title": "第一次聊天",
  "pinned": false,
  "message": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "我在。", "reasoning_content": "（开启深度思考时的推理过程）"}
  ],
  "nickname": "溟月",
  "nature": "聪明但很懒，傲娇嘴甜，酷爱白米饭",
  "role_description": "溟月是一位拥有蓝色长发和蓝色眼睛的少女……",
  "output_rules": "请以第一人称的口吻回答问题……",
  "temperature": 1.0,
  "top_p": 1.0,
  "limit_tokens": false,
  "max_tokens": 4096,
  "frequency_penalty": 0.0,
  "presence_penalty": 0.0
}
```

### 什么时候写盘

- **改人设 / 高级参数后立刻写盘**（控件挂在 `on_change=save_session`），不用先聊一句；
  「恢复默认值」按钮同理
- **发出消息后**（含模型的回答）写盘
- **重命名 / 置顶 / 删除**只改对应字段，保留消息与人设
- 刚新建、还没产生对话的会话不会进会话历史，它的改动写在
  `sessions/drafts/<会话ID>.draft`（同一套 JSON 结构）；发出第一条消息时转正为正式存档，
  草稿随即删除

> - `title` 是**会话名称**（显示用），在侧边栏「会话名称」里改名只改这个字段，**不会重命名或移动 JSON 文件**；文件名始终是会话 ID。
> - 没有 `title` 的老存档，显示时自动回退成会话 ID。
> - `pinned` 为置顶标记，置顶的会话排在会话历史最前。
> - 四大人设字段与 `AI_partner.py` 里的 `DEFAULT_PROFILE` 一一对应，字段缺失时按默认值回填；昵称字段名为 `nickname`（历史版本里的拼写错误 `nike_name` 已废弃）。
> - 六个高级生成参数与 `DEFAULT_ADVANCED` 一一对应，**跟着会话走**：切回某个会话时会恢复它自己的这套参数；老存档没有这些字段就按默认值来。
> - `reasoning_content` 只在开启深度思考时出现，用于回放折叠的思考过程；请求时会一并回传给 API（服务端会忽略它，且不计入上下文长度）。

## 高级配置说明

| 参数 | 范围 | 说明 |
| --- | --- | --- |
| 温度 temperature | 0 – 2，步长 0.05 | 越高越发散；角色扮演想有个性可调高，想稳定复现可调低 |
| 核采样 top_p | 0 – 1，步长 0.05 | 与温度二选一调即可，一般保持 1.0 |
| 重复惩罚 frequency_penalty | -2 – 2，步长 0.1 | 正值降低重复用词概率 |
| 话题新鲜度 presence_penalty | -2 – 2，步长 0.1 | 正值鼓励引入新话题 |
| 限制单次回复长度 | 开关 + 256 – 32768 | 不勾选则由服务端决定上限 |

未启用的参数不会出现在请求体里；点「恢复默认值」可一键还原（默认值与不传参时的服务端默认行为一致）。
