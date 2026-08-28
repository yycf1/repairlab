# 开发与复验

```sh
uv sync --frozen --extra dev
uv run ruff check .
uv run pytest -q
```

测试使用脚本化模型客户端，无真实网络调用；docker roundtrip 用例在本机 Docker 引擎可用时自动启用（CI 的 docker 作业会构建 `easycode-python:latest` 镜像并运行它们）。

请勿将工作产物（`.easycode/`、`.env`、个人简历源码、真实仓库内容）提交到 Git。模型配置写 `.env`（键见 `.env.example`），任何密钥都不进日志、不进事件流。提交真实模型成绩时附固定任务、模型、预算、全部失败和运行条件，避免把脚本演示写成模型能力。

Python 3.11+，需要 Git；模型服务为可选项。Docker 隔离的实现与实测范围见 [Docker 容器隔离速记](docs/Docker容器隔离速记.md)。
