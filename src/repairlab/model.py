from __future__ import annotations

import json
import re
import time
from typing import Callable
import httpx
from pydantic import ValidationError

from repairlab.models import Decision
from repairlab._vendor.mini.exceptions import FormatError


def parse_decision(content):
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return Decision.model_validate_json(text)


class ModelBase:
    def __init__(self):
        self.tokens = {"prompt": 0, "completion": 0}
        self.model_calls = 0
        self.state = {}

    def format_message(self, **kwargs):
        return kwargs

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": json.dumps({"observation": output}, ensure_ascii=False)}
                for output in outputs]

    def get_template_vars(self, **kwargs):
        return {}

    def serialize(self):
        return {"model": {"kind": type(self).__name__, "tokens": dict(self.tokens), "calls": self.model_calls}}

    @staticmethod
    def message(decision):
        return {"role": "assistant", "content": json.dumps(decision.model_dump(exclude_defaults=True), ensure_ascii=False),
                "extra": {"actions": [decision.action.model_dump(exclude_defaults=True)], "cost": 0, "note": decision.note}}


class CompatibleModel(ModelBase):
    def __init__(self, settings, limits, *, client=None):
        super().__init__()
        if not settings.model_ready:
            raise ValueError("请在项目2的 .env 中配置模型、接口地址和密钥。")
        self.settings, self.limits = settings, limits
        self.client = client or httpx.Client(timeout=httpx.Timeout(45, connect=10))

    def context(self, messages):
        cap = self.limits.context_chars
        system = messages[0].get("content", "")
        # State is structured memory, not an arbitrary prefix of a JSON string.
        memory = dict(self.state)
        memory["plan"] = [str(item)[:160] for item in memory.get("plan", [])[:12]]
        memory["inspected_files"] = memory.get("inspected_files", [])[-12:]
        for key in ("baseline_test", "latest_test"):
            if memory.get(key):
                memory[key] = {k: v for k, v in memory[key].items() if k not in {"source_digest", "hint"}}
        state = json.dumps({"controller_state": memory}, ensure_ascii=False)
        remaining = cap - len(system) - len(state)
        if remaining < 500:
            raise ValueError("Context budget too small for the required tool schema")
        def clip(content, size):
            if len(content) <= size:
                return content
            marker = "\n[中段内容已压缩]\n"
            if size <= len(marker):
                return content[:max(0, size)]
            side = max(0, (size - len(marker)) // 2)
            return content[:side] + marker + content[-side:]
        task = clip(messages[1].get("content", ""), remaining - min(1000, remaining // 3))
        initial = [{"role": "system", "content": system}, {"role": "user", "content": task},
                   {"role": "user", "content": state}]
        recent = messages[2:][-8:]
        remaining = cap - sum(len(m["content"]) for m in initial)
        share = remaining // max(1, len(recent))
        for m in recent:
            content = m.get("content", "")
            content = clip(content, share)
            initial.append({"role": m["role"], "content": content})
        return initial

    def query(self, messages):
        self.model_calls += 1
        response = None
        for attempt in range(2):
            try:
                response = self.client.post(self.settings.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.api_key},
                    json={"model": self.settings.model, "messages": self.context(messages),
                          "temperature": 0, "max_tokens": 1600})
            except httpx.HTTPError:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                raise RuntimeError("模型连接失败；请检查接口、网络与超时配置。") from None
            if response.status_code in {429, 500, 502, 503} and attempt == 0:
                time.sleep(1)
                continue
            break
        if response is None or response.is_error:
            # Never persist provider bodies/headers: they can echo credentials or account details.
            raise RuntimeError(f"模型接口返回 HTTP {response.status_code if response is not None else 'unknown'}；未保存响应正文。")
        body = response.json()
        usage = body.get("usage") or {}
        self.tokens["prompt"] += usage.get("prompt_tokens", 0) or 0
        self.tokens["completion"] += usage.get("completion_tokens", 0) or 0
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        try:
            return self.message(parse_decision(content))
        except ValidationError as error:
            # Explain schema violations without echoing input, provider bodies or credentials.
            problems = [{"field": ".".join(str(p) for p in item["loc"]), "type": item["type"],
                         "message": item["msg"] if item["type"] != "json_invalid" else "Invalid JSON"}
                        for item in error.errors(include_input=False, include_context=False)[:6]]
            raise FormatError({"role": "user", "content": "动作格式不合法；按以下字段错误修正，工具名称及参数必须严格符合 Schema："
                               + json.dumps(problems, ensure_ascii=False), "extra": {"cost": 0}}) from None
        except ValueError:
            raise FormatError({"role": "user", "content": "动作格式不合法。只输出一个符合 Schema 的 JSON 对象。", "extra": {"cost": 0}}) from None

    def close(self):
        self.client.close()


class DemoModel(ModelBase):
    """Deterministic teaching fixture. This is explicitly NOT a real LLM or effectiveness evidence."""
    def __init__(self, demo_config, strategy="feedback"):
        super().__init__()
        self.demo = demo_config
        self.stage = 0
        self.strategy = strategy
        self.revised = False
        self.reviewed = False

    def query(self, messages):
        self.model_calls += 1
        tools = [
            {"tool": "update_plan", "plan": ["查看失败涉及的函数", "运行开发测试复现", "修改源代码", "测试通过后提交补丁"]},
            {"tool": "list_files"},
            {"tool": "symbols", "path": self.demo["file"]},
            {"tool": "read_file", "path": self.demo["file"]},
            {"tool": "run_tests"},
            {"tool": "record_finding", "paths": [self.demo["file"]],
             "summary": self.demo.get("hypothesis", "基于已读代码和原始测试提出可验证的边界修复。")},
            {"tool": "edit_file", "path": self.demo["file"], **self.demo["first_patch"]},
            {"tool": "run_tests"},
        ]
        notes = ["先记录可检查的修复步骤。", "查看仓库文件范围。", "定位相关函数。", "读取当前实现。",
                 "用开发测试确认缺陷。", "记录与源文件和测试关联的修复假设。", "应用第一版局部修改。", "检查修改后的开发测试。"]
        if self.stage < len(tools):
            action, note = tools[self.stage], notes[self.stage]
            self.stage += 1
        else:
            try:
                observation = json.loads(messages[-1]["content"]).get("observation", {})
            except (ValueError, KeyError):
                observation = {}
            if (observation.get("category") == "test_failure" and self.demo.get("refinement")
                    and not self.revised and self.strategy == "feedback"):
                self.revised = True
                action = {"tool": "update_plan", "plan": ["第一版修改未覆盖全部输入", "根据失败断言补齐边界条件", "重新测试后提交"]}
                self.stage = 100
                note = "开发测试仍失败，更新修复计划。"
            elif self.stage == 100:
                action = {"tool": "record_finding", "paths": [self.demo["file"]],
                          "summary": "当前版本仍未过滤空项，开发测试失败；补上空标签过滤应消除剩余失败。"}
                self.stage = 101
                note = "依据本轮失败修订假设，保留原始复现证据。"
            elif self.stage == 101:
                action = {"tool": "edit_file", "path": self.demo["file"], **self.demo["refinement"]}
                self.stage = 102
                note = "根据失败反馈修正遗漏的边界条件。"
            elif self.stage == 102:
                action = {"tool": "run_tests"}
                self.stage = 103
                note = "再次运行开发测试。"
            elif not self.reviewed:
                action = {"tool": "get_patch"}
                self.reviewed = True
                note = "检查完整候选补丁，确认只改变预期的源代码行为。"
            else:
                action = {"tool": "submit", "summary": "提交候选补丁，由独立验证器决定最终结果。"}
                note = "提交候选补丁，测试成功不替代最终验收。"
        return self.message(Decision.model_validate({"note": note, "action": action}))


class ScriptedModel(ModelBase):
    """Injection seam for protocol and failure tests, not a production model."""
    def __init__(self, decisions: list[dict] | Callable):
        super().__init__()
        self.decisions = decisions

    def query(self, messages):
        if callable(self.decisions):
            value = self.decisions(messages)
        else:
            value = self.decisions[self.model_calls]
        self.model_calls += 1
        return self.message(Decision.model_validate(value))
