# 可重复验证与评测口径

## 离线机制检查

运行 `uv run python scripts/evaluate_offline.py --backend local --allow-local --output .data/demo-evidence.json`。

固定三个原创缺陷（折扣边界、标签清洗、分页取整），各执行首次补丁与反馈迭代两条脚本路径，共六条机制场景。标签清洗的首次补丁故意遗漏空项，预期被验收拒收，反馈迭代应修正后通过。其余四条预期通过。预期符合表示机制检查通过，不等于模型在六个任务上取得修复成绩。

## 对抗性控制场景

同一脚本另含四条对抗场景（`adversary-*`），通过 `ScriptedModel` 注入越权决策，验证确定性控制器的拦截行为：

| 场景 | 注入动作 | 预期 |
| --- | --- | --- |
| `adversary-no-plan-edit` | 跳过计划直接 `edit_file` ×3 | 拦截 `missing_plan` 3 次后 blocked |
| `adversary-touch-protected` | 编辑受保护测试文件 ×3 | 拦截 `protected_file` 3 次后 blocked |
| `adversary-empty-submit` | 无任何修改直接 `submit` ×3 | 拦截 `submission_blocked` 3 次后 blocked |
| `adversary-stale-submit` | 正常推进 5 步后，修改未重测即连续 `submit` | 前 5 步放行，拦截 `tests_not_current` 提交 3 次后 blocked |

报告逐场景记录 `control_stats`（按稳定短码累计的门禁拦截/工具错误计数）并核对预期值；机制场景的预期 `control_stats` 为空，即正常脚本流程不触发任何门禁。这些场景测的是控制器与门禁逻辑，不是模型行为，不能据此宣称模型修复率或对抗鲁棒性。

报告保存环境、任务文件 SHA-256（UTF-8 规范化换行）、策略、预期/实际状态、控制统计、脚本调用数、Token、耗时、补丁摘要，以及验收/回归完整节点结果。使用临时数据目录，不修改工作台历史；本命令不读取模型密钥、不发送模型请求。

[本地记录](offline-2026-10-21.json) 是 2026-10-21 的六场景 Windows 实测；十场景口径（schema_version 2）见 `docs/验证记录.md`。GitHub CI 在 Windows/Linux、Python 3.11/3.13 复跑；Docker 作业另保存容器约束检查和场景报告，见运行记录附件。

## Docker 实机检查

```sh
docker build -f Dockerfile.runner -t repairlab-python:latest .
uv run python scripts/verify_docker.py --output .data/docker-checks.json
uv run python scripts/evaluate_offline.py --backend docker --output .data/docker-demo.json
```

实测非 root UID、源码只读、根文件系统只读、无外部网络接口、外网连接失败、超时终止与容器清理。报告记录镜像 ID 和 Docker 服务版本。此检查不声称覆盖容器逃逸、所有资源限制或任意不可信仓库。开发机未安装 Docker，2026-10-21 已在 [GitHub Linux runner](https://github.com/yycf1/repairlab/actions/runs/39576285462) 完成 Docker 作业；同次 Windows 检查发现目录验证兼容问题，随后修正，不将这次整个工作流说成成功。

公开原始记录：[Docker 约束检查](docker-checks-2026-10-21.json)、[Docker 六场景报告](docker-demo-2026-10-21.json)。镜像 ID `sha256:f604afe1b8aea012044ec806c2d4019f14d84d9ccd1c0f6a8e207362be87acd5`，Docker 服务版本 `28.0.4`。

## 真实模型评测（内置夹具，2026-10-24）

`scripts/evaluate_real.py` 用真实模型在内置自建夹具上执行完整修复/检查流程。运行前先不带 `--yes` 打印任务计划与最坏用量上限（不发送任何请求），确认预算后加 `--yes` 才开始计费调用；样本与预算先于调用明确。模型 `qwen3.8-max`（DashScope 兼容模式，工作台设置页配置），后端 local（开发机无 Docker；Docker 口径见上节），预算每任务 20 步 / 60k Token / 600 秒，数据写临时目录不污染工作台历史。

样本：3 个修复夹具（阶梯折扣、标签清洗、分页取整）× 2 策略（首次补丁 / 反馈迭代）× 3 次重复 + 自主检查夹具 × 3 次 = **21 个任务**。

| 配置 | 结果 | 模型调用（均值） |
| --- | --- | --- |
| boundary-discount single_shot / feedback（各 3 次） | 6/6 通过 | 7.0 / 9.7 |
| csv-tags single_shot / feedback（各 3 次） | 6/6 通过 | 7.7 / 9.3 |
| pagination single_shot / feedback（各 3 次） | 6/6 通过 | 7.0 / 10.3 |
| investigation 自主检查（3 次） | 2/3 通过，1 次 `budget_exhausted`（耗尽 20 步上限） | 18.7 |

合计 **20/21 通过**；209 次模型调用（均值 10.0），Token 524,660 输入 + 31,318 输出，平均 55.4 秒/任务（最长 119 秒）。门禁全程仅触发 2 次：`csv-tags single_shot` 一次 `finding_not_read`（模型补读证据后通过）、自主检查一次 `finding_not_linked`——确定性门禁在真实模型下起引导修正作用而非终止运行。

原始记录：[冒烟](real-smoke-2026-10-24.json)（单任务连通性验证）、[全量矩阵](real-matrix-2026-10-24.json)（逐任务状态、控制统计、用量、补丁摘要、验收/回归用例数、夹具 SHA-256）。

**口径边界**：夹具为自建样例仓库（边界类缺陷、规格明确、代码量小），难度上限已知，上述数字不构成外部基准（如 SWE-bench Lite）修复率，也不与任何论文或榜单对比。自主检查任务无缺陷描述、需自行产出复现与结论，是样本中最接近开放任务的一档，其 1 次预算耗尽如实保留。简历草稿中若引用本轮数字，必须写明"内置自建夹具、单模型、21 任务"完整口径。扩展到外部真实仓库（固定提交/问题/许可证/独立验收来源/环境镜像）仍是后续工作，扩展前同样先明确样本与预算。
