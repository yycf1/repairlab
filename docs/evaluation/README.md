# 可重复验证与评测口径

## 离线机制检查

运行 `uv run python scripts/evaluate_offline.py --backend local --allow-local --output .data/demo-evidence.json`。

固定三个原创缺陷（折扣边界、标签清洗、分页取整），各执行首次补丁与反馈迭代两条脚本路径，共六个场景。标签清洗的首次补丁故意遗漏空项，预期被验收拒收，反馈迭代应修正后通过。其余四个场景预期通过。六项预期符合表示机制检查通过，不等于模型在六个任务上取得修复成绩。

报告保存环境、任务文件 SHA-256（UTF-8 规范化换行）、策略、预期/实际状态、脚本调用数、Token、耗时、补丁摘要，以及验收/回归完整节点结果。使用临时数据目录，不修改工作台历史；本命令不读取模型密钥、不发送模型请求。

[本地记录](offline-2026-10-21.json) 是 Windows 实测。GitHub CI 在 Windows/Linux、Python 3.11/3.13 复跑；Docker 作业另保存容器约束检查和六场景报告，见运行记录附件。

## Docker 实机检查

```sh
docker build -f Dockerfile.runner -t repairlab-python:latest .
uv run python scripts/verify_docker.py --output .data/docker-checks.json
uv run python scripts/evaluate_offline.py --backend docker --output .data/docker-demo.json
```

实测非 root UID、源码只读、根文件系统只读、无外部网络接口、外网连接失败、超时终止与容器清理。报告记录镜像 ID 和 Docker 服务版本。此检查不声称覆盖容器逃逸、所有资源限制或任意不可信仓库。开发机未安装 Docker，2026-10-21 已在 [GitHub Linux runner](https://github.com/yycf1/repairlab/actions/runs/39576285462) 完成 Docker 作业；同次 Windows 检查发现目录验证兼容问题，随后修正，不将这次整个工作流说成成功。

公开原始记录：[Docker 约束检查](docker-checks-2026-10-21.json)、[Docker 六场景报告](docker-demo-2026-10-21.json)。镜像 ID `sha256:f604afe1b8aea012044ec806c2d4019f14d84d9ccd1c0f6a8e207362be87acd5`，Docker 服务版本 `28.0.4`。

## 真实模型评测仍需补齐

现存 [真实模型冒烟](../real-model-smoke.json) 为历史自建样例；不用于宣称外部仓库修复率。下一步固定外部仓库提交、问题与许可证、独立验收来源、环境镜像、模型版本及预算，完整记录成功、失败、环境故障和预算耗尽。首次补丁策略可以包含多次阅读，不应写成“单次模型调用”。

简历草稿中的“50 个任务、34 个通过、68%”没有对应实测数据，本仓库不将其作为成绩。扩展真实任务与收费模型调用前需要先明确样本和预算。
