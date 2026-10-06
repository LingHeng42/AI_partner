# 凌恒的酒馆 🐳

一个基于 **Streamlit + DeepSeek** 的角色扮演聊天应用：可以自定义角色的昵称、性格、角色简介和输出规则，对话自动存档在本地，随时切换历史会话。

## 功能

- **角色扮演对话**：昵称 / 性格 / 角色简介 / 输出规则四项人设实时生效，注入 system prompt
- **深度思考开关**：侧边栏一键切换，开启后走 `reasoning_effort=low` + `thinking=enabled`
- **流式输出**：边生成边显示，附光标提示
- **本地会话存档**：按时间戳命名，支持新建 / 切换 / 二次确认删除
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

两个测试都不联网：一个用假的 `streamlit`/`openai` 模块测纯逻辑，另一个用 Streamlit 官方 `AppTest` 跑真实页面（OpenAI 客户端被替换为假实现）。

```bash
.venv\Scripts\python.exe tests\test_logic.py    # 存档 / 上下文截断 / 对话流程
.venv\Scripts\python.exe tests\test_smoke.py    # 页面渲染 / 缺少 Key 的提示
```

## 会话存档格式

`sessions/<YYYY-MM-DD_HHMMSS_mmm>.json`：

```json
{
  "message": [{"role": "user", "content": "你好"}],
  "nickname": "溟月",
  "nature": "聪明但很懒，傲娇嘴甜，酷爱白米饭",
  "role_description": "溟月是一位拥有蓝色长发和蓝色眼睛的少女……",
  "output_rules": "请以第一人称的口吻回答问题……"
}
```

> 四个字段名与 `AI_partner.py` 里的 `DEFAULT_PROFILE` 一一对应；字段缺失时按默认值回填。昵称字段名为 `nickname`（历史版本里的拼写错误 `nike_name` 已废弃）。
