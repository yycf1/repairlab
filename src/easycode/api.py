"""EasyCode web 工作台 API：会话 CRUD + SSE 事件流 + steering。

SSE 模式沿用已在本地压过的形态：0.4s 轮询事件游标、Last-Event-ID
断线续传、定期 heartbeat、终态且无新事件时关流；客户端断开由 starlette 以
GeneratorExit 终止生成器。
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from easycode.service import TERMINAL_STATUS, SessionService
from easycode.settings import Settings

WEB_DIR = Path(__file__).parent / "web"


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cwd: str = Field(min_length=1, max_length=500)
    task: str = Field(min_length=1, max_length=20000)
    backend: str = Field(default="docker", pattern="^(docker|local)$")
    tools: list[str] = Field(default_factory=list, max_length=8)
    max_steps: int = Field(default=200, ge=1, le=1000)
    max_cost: float | None = Field(default=None, gt=0, le=50)
    max_seconds: int = Field(default=1800, ge=10, le=7200)
    command_timeout: int = Field(default=60, ge=1, le=600)


class SteerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=6000)


class ContinueBody(SteerBody):
    pass


class ModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=300)
    api_key: str = Field(default="", max_length=300)


class PickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    initial: str = Field(default="", max_length=500)


class RenameBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


def create_app(settings=None):
    settings = settings or Settings()
    service = SessionService(settings)

    @asynccontextmanager
    async def lifespan(app):
        yield
        service.close()

    app = FastAPI(title="EasyCode", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/config")
    def config():
        return settings.public()

    @app.get("/api/sessions")
    def sessions():
        return service.store.list()

    @app.post("/api/sessions", status_code=201)
    def start(body: StartBody):
        try:
            return service.start(cwd=body.cwd, task=body.task, backend=body.backend,
                                 tools=body.tools or None, max_steps=body.max_steps,
                                 max_cost=body.max_cost, max_seconds=body.max_seconds,
                                 command_timeout=body.command_timeout)
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id: str):
        session = service.store.get(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在。")
        return session

    @app.delete("/api/sessions/{session_id}")
    def session_delete(session_id: str):
        service.cancel(session_id)
        if not service.store.delete(session_id):
            raise HTTPException(404, "会话不存在。")
        return {"deleted": True}

    @app.patch("/api/sessions/{session_id}")
    def session_rename(session_id: str, body: RenameBody):
        """会话重命名（pisesh 式自定义标题）；只改展示标题，不动事件流。"""
        title = body.title.strip()
        if not title:
            raise HTTPException(400, "标题不能为空。")
        try:
            service.store.update(session_id, title=title)
        except KeyError:
            raise HTTPException(404, "会话不存在。")
        return service.store.get(session_id)

    @app.get("/api/sessions/{session_id}/events")
    def session_events(session_id: str, after: int = 0):
        if service.store.get(session_id) is None:
            raise HTTPException(404, "会话不存在。")
        return service.store.events(session_id, after=after)

    @app.get("/api/sessions/{session_id}/stream")
    def session_stream(session_id: str, request: Request, after: int = 0):
        if service.store.get(session_id) is None:
            raise HTTPException(404, "会话不存在。")

        def generate():
            cursor, terminal_beats = after, 0
            while True:
                events = service.store.events(session_id, after=cursor)
                for event in events:
                    cursor = event["id"]
                    yield f"id: {event['id']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                row = service.store.get(session_id)
                if row is None:  # 会话已删除：立即收尾，不空转心跳
                    yield f"data: {json.dumps({'kind': 'close', 'data': {'reason': 'deleted'}}, ensure_ascii=False)}\n\n"
                    return
                status = row["status"]
                if events:
                    terminal_beats = 0
                elif status in TERMINAL_STATUS:
                    terminal_beats += 1
                    if terminal_beats >= 5:
                        yield f"data: {json.dumps({'kind': 'close', 'data': {}}, ensure_ascii=False)}\n\n"
                        return
                else:
                    yield f"data: {json.dumps({'kind': 'heartbeat', 'data': {}}, ensure_ascii=False)}\n\n"
                time.sleep(0.4)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/sessions/{session_id}/steer")
    def session_steer(session_id: str, body: SteerBody):
        try:
            depth = service.steer(session_id, body.text)
        except KeyError:
            raise HTTPException(404, "会话不存在。")
        except ValueError as error:
            raise HTTPException(400, str(error))
        return {"queued": True, "depth": depth}

    @app.post("/api/sessions/{session_id}/continue")
    def session_continue(session_id: str, body: ContinueBody):
        try:
            return service.continue_session(session_id, body.text)
        except KeyError:
            raise HTTPException(404, "会话不存在。")
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/sessions/{session_id}/export")
    def session_export(session_id: str):
        """JSONL 会话导出（pi 对齐：文档化格式，可自动后处理）。首行是会话元数据。"""
        from fastapi.responses import Response
        session = service.store.get(session_id)
        if session is None:
            raise HTTPException(404, "会话不存在。")
        lines = [json.dumps({"kind": "session", "data": session}, ensure_ascii=False)]
        for event in service.store.events(session_id):
            lines.append(json.dumps({"kind": event["kind"], "created": event["created"],
                                     "data": event["data"]}, ensure_ascii=False))
        return Response("\n".join(lines) + "\n", media_type="application/x-ndjson",
                        headers={"Content-Disposition": f'attachment; filename="{session_id}.jsonl"'})

    @app.post("/api/sessions/{session_id}/cancel")
    def session_cancel(session_id: str):
        if service.store.get(session_id) is None:
            raise HTTPException(404, "会话不存在。")
        service.cancel(session_id)
        return {"cancelling": True}

    @app.get("/api/model")
    def model_get():
        from easycode.model_config import public_model_settings
        return public_model_settings(settings)

    @app.get("/api/providers")
    def providers_get():
        from easycode.model_config import PROVIDERS
        return {"items": PROVIDERS}

    @app.put("/api/model")
    def model_put(body: ModelBody):
        """写入 .env 并即时生效；api_key 留空表示沿用已保存的密钥（页面只显示掩码）。"""
        from easycode.model_config import API_KEY_KEY, apply_to_process, public_model_settings, read_env_config, write_env_config
        current = read_env_config(settings.env_path)
        api_key = body.api_key.strip() or current[API_KEY_KEY] or settings.api_key
        if not api_key:
            raise HTTPException(400, "请填写 API Key。")
        values = write_env_config(settings.env_path, model=body.model.strip(),
                                  base_url=body.base_url.strip().rstrip("/"), api_key=api_key)
        apply_to_process(values)
        return public_model_settings(settings)

    @app.post("/api/model/test")
    def model_test(body: ModelBody):
        from easycode.model_config import test_connection
        return test_connection(body.base_url.strip().rstrip("/"), body.api_key.strip(), body.model.strip())

    @app.post("/api/pick-directory")
    def pick_directory(body: PickBody | None = None):
        """Windows 系统目录选择对话框（独立 STA 进程）；其他平台 501。"""
        import os
        from easycode import directory_picker
        try:
            return directory_picker.choose_directory((body or PickBody()).initial)
        except RuntimeError as error:
            raise HTTPException(501 if os.name != "nt" else 400, str(error))

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
