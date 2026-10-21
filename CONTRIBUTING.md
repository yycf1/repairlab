# 开发与复验

```sh
uv sync --frozen --extra dev
uv run ruff check .
uv run pytest -q
uv run python scripts/evaluate_offline.py --backend local --allow-local --output .data/demo-evidence.json
```

测试只使用本项目原创可信夹具和模型替身。请勿将工作台 `.data`、`.env`、个人简历、真实仓库快照或供应商响应提交到 Git。需要调整验收时补充行为测试，并说明原来为什么会误判；保留上游版本与许可记录。

Python 3.11+，需要 Git；模型服务为可选项。Docker 验证命令及范围见 [评测说明](docs/evaluation/README.md)。提交真实模型成绩时附固定任务、模型、预算、全部失败和运行条件，避免把脚本演示写成模型能力。
