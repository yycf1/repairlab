"""Export a factual action/observation walkthrough without model private reasoning."""
import argparse
import json
from pathlib import Path
import sqlite3
from repairlab.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Settings().data_dir)
    parser.add_argument("--task-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database = args.data_dir / "repairlab.sqlite3"
    if not database.exists():
        raise SystemExit("Task database not found")
    with sqlite3.connect(database) as db:
        if args.task_id:
            row = db.execute("SELECT id,data,status FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        else:
            row = db.execute("SELECT id,data,status FROM tasks WHERE json_extract(data,'$.mode')='demo' ORDER BY created DESC LIMIT 1").fetchone()
        if not row:
            raise SystemExit("Task not found")
        task_id, raw, status = row
        task = json.loads(raw)
        events = [(kind, json.loads(data)) for kind, data in
                  db.execute("SELECT kind,data FROM events WHERE task_id=? ORDER BY id", (task_id,))]
    lines = [f"# {task['title']}：流程走查", "", f"任务 `{task_id}`，最终状态 `{status}`。",
             "", "以下来自实际动作及工具事件；脚本演示用于解释流程，不是模型修复能力评测。" if task["mode"] == "demo"
             else "以下来自真实模型动作及工具事件；仅代表这一项任务。", "",
             "| 步骤 | 动作 | 工具反馈 | 代码版本 |", "| ---: | --- | --- | ---: |"]
    action = {}
    workflow = {}
    findings = []
    for kind, data in events:
        if kind == "action":
            action = data
        elif kind == "workflow_state":
            workflow = data
        elif kind == "observation":
            label = data["category"]
            evidence = data.get("data", {}).get("evidence")
            if evidence:
                label += " / " + json.dumps(evidence["counts"], ensure_ascii=False)
            lines.append(f"| {action.get('step', '?')} | {data['tool']} | {label} | {workflow.get('revision', '?')} |")
            finding = data.get("data", {}).get("hypothesis")
            if finding:
                findings.append(f"- 版本 {finding['revision']}，关联测试 {finding['test_run_id']}：{finding['summary']}")
    final = task.get("workflow", {})
    verification = task.get("verification") or {}
    lines += ["", "## 假设如何变化", "", *findings, "", "## 核对重点", "",
              f"原始测试：`{(final.get('baseline_test') or {}).get('category', '未执行')}`；最近测试：`{(final.get('latest_test') or {}).get('category', '未执行')}`；代码版本：`{final.get('revision', '?')}`。",
              "", f"独立验收结果：`{verification.get('reason', '未进入验收')}`。请逐行核对修改前复现、假设更新和修改后重测的顺序；最终通过不能由模型自报替代。",
              "", "完整规则和源码阅读顺序见 [项目流程与实现思路](项目流程与实现思路.md)。", ""]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"task_id": task_id, "status": status, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
