# EasyCode

[![Checks](https://github.com/yycf1/easycode/actions/workflows/ci.yml/badge.svg)](https://github.com/yycf1/easycode/actions/workflows/ci.yml)

**pi 式极简编程 agent**（架构学习参考 [badlogic/pi-mono](https://github.com/badlogic/pi-mono)）：给它一个目录和一句任务，它读代码、改文件、在加固容器里跑命令，做完用中文向你总结。手写极简 agent 循环、原生 tool_calls、steering 插话、AGENTS.md 分层定制、JSONL 会话导出。

- **极简循环**（`core/loop.py`，~110 行）：模型回复不含工具调用即完成；所有事件外发，没有任何背后注入的隐藏内容——上下文里有什么，你都能在工作台看到。
- **四工具面**（`tools/`）：read（offset/limit 翻页）/ bash（command + timeout）/ edit（old 精确唯一匹配）/ write（新建或整体重写），与 pi 同名同参；ls/grep 是可选加速器，`--tools` 显式开启。
- **steering**：agent 运行中随时插话，消息排队，当前工具轮结束、下一次模型调用前自动注入（turn 边界，pi 语义）；停止会话会一并清空队列。
- **continue**：会话结束后在同一会话继续追加指令，完整上下文保留（round+1，预算沿用）；中断会话续跑时自动补齐协议序列。
- **沙箱 bash**：默认 Docker 一次性加固容器（断网、非 root、系统层只读、CPU/内存/进程数受限）；可信目录可切本机直跑。这是相对 pi 默认 YOLO 的刻意差异——pi 作者的建议是"不放心就进容器"，我们直接把容器做成了默认项。
- **AGENTS.md**：全局 `~/.easycode/AGENTS.md` → 项目 `工作区/AGENTS.md` 逐层追加进系统提示；系统提示本体保持 <1000 tokens 的极简。

## 快速运行

```powershell
git clone https://github.com/yycf1/easycode.git
cd easycode
uv sync --frozen --extra dev
uv run easycode serve --port 8767
```

浏览器打开 <http://127.0.0.1:8767>：左侧「＋ 新会话」→ 填工作目录（agent 在这个**真实目录**里读写与执行）与任务 → 发送。运行中输入框直接发送即插话；结束后继续发送即 continue。

无界面入口：

```powershell
uv run easycode run "修复 utils.py 里所有拼写错误的函数名并运行测试验证" --cwd E:\my-project --backend docker
uv run easycode run "只读检查这个目录" --cwd . --backend local --tools read,ls,grep
```

模型配置：`.env` 里填 `EASYCODE_MODEL / EASYCODE_BASE_URL / EASYCODE_API_KEY`（任何 OpenAI 兼容 Chat Completions 接口，要求支持原生 tool calling）。

## 架构

三层分包，与 pi-mono 一一对应：

| pi 包 | EasyCode | 内容 |
|---|---|---|
| pi-ai | `src/easycode/providers/` | OpenAI 兼容客户端：原生 tool_calls（流式参数拼装）、取消返回部分结果、错误不携带供应商正文、token/费用尽力估算 |
| pi-agent-core | `src/easycode/core/` | 手写极简循环 + 事件外发 + steering 注入 + 显式预算保险丝（steps/cost/seconds，默认宽松） |
| pi-coding-agent | `src/easycode/tools/` `session.py` `prompt.py` `cli.py` | 四工具 + 会话存储（SQLite 事件流，SSE 断线续传）+ AGENTS.md + run/serve CLI |
| pi-web-ui | `src/easycode/web/` | chat-first 工作台（无框架无构建，纯函数渲染 `render.js` 与编排 `app.js` 分离）：亮暗双主题（三态切换/防闪白）、手写语法高亮（py/js/json/bash/html/css/yaml/diff）、流式 markdown、可折叠工具块 + 行级/词级 diff、插话排队泡、会话搜索/行内重命名、用量明细（逐轮 tokens/成本/步数进度）、SSE 断线指示、Toast |

引擎层（加固 Docker runner、SQLite 会话存储与 steering 原语、供应商牌价表）与 agent 层同仓维护，全部落在 `src/easycode` 一个包里，没有额外依赖或外部服务。

## 明确不做（与 pi 同款取舍）

MCP、子代理、内置 todo/plan、后台任务管理、会话分支树与自动压缩（backlog）——"if I don't need it, it won't be built"。

## 测试

```powershell
uv run pytest -q
```

EasyCode 57 项（循环/协议/工具/会话/续跑/导出/模型设置/会话重命名 + 引擎 runner/storage/牌价）；`ruff check`、`node --check` 通过。docker roundtrip 用例在本机 Docker 引擎可用时自动启用，CI 的 docker 作业会构建 `easycode-python:latest` 镜像并运行它们。
