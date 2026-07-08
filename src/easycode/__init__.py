"""EasyCode — 极简编程 agent（架构参考 badlogic pi）。

三层结构：providers（统一模型客户端，原生 tool_calls）→ core（手写极简循环，
steering 在 turn 边界注入）→ tools/session/web（四工具、会话与工作台）。
引擎层（runners 加固沙箱 / storage 会话原语 / model_config 牌价）与 agent 层同包维护。
"""
