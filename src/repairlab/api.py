from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from repairlab.models import (ApplyRequest, CheckSessionRequest, DirectoryDiscovery, DirectoryPick, FollowUpRequest,
                              ModelSettingsUpdate, ProfileActivateRequest, RepoProfile, RepositoryCheck,
                              RepositoryRegistration, StartRequest)
from repairlab.directory_picker import choose_directory
from repairlab.discovery import discover_repository
from repairlab.model_config import (API_KEY_KEY, PROVIDERS, activate_profile, apply_to_process,
                                    public_model_settings, public_profiles, test_connection, upsert_profile,
                                    write_env_config)
from repairlab.repository import Repository
from repairlab.service import Service, TERMINAL
from repairlab.settings import Settings


class NoCacheStaticFiles(StaticFiles):
    """工作台资源随代码更新；no-cache 强制每次刷新前重新校验，否则浏览器启发式缓存会让打开中的页面在普通刷新后仍用旧样式。"""
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


def create_app(settings=None):
    settings = settings or Settings()
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(settings)
        yield
        app.state.service.close()

    app = FastAPI(title="RepairLab", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])

    @app.middleware("http")
    async def local_mutations(request, call_next):
        # Directory registration is a local operator action, not a cross-site browser capability.
        origin = request.headers.get("origin")
        if request.method in {"POST", "PUT", "DELETE"} and origin and origin != str(request.base_url).rstrip("/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "请在本机工作台页面中操作。"}, status_code=403)
        return await call_next(request)
    static = Path(__file__).parent / "web"
    app.mount("/static", NoCacheStaticFiles(directory=static), name="static")

    def service():
        return app.state.service

    def task_or_404(task_id):
        if not re.fullmatch(r"[a-f0-9]{16}", task_id):
            raise HTTPException(404, "任务不存在")
        task = service().store.get(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return task

    @app.get("/")
    def index():
        return FileResponse(static / "index.html", headers={"cache-control": "no-cache"})

    @app.get("/api/config")
    def config():
        return settings.public()

    @app.get("/api/settings/model")
    def model_settings():
        return public_model_settings(settings)

    @app.get("/api/settings/providers")
    def model_providers():
        return PROVIDERS

    @app.put("/api/settings/model")
    def save_model_settings(request: ModelSettingsUpdate):
        base_url = request.base_url.strip().rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise HTTPException(400, "接口地址必须以 http(s):// 开头。")
        stored = settings.api_key
        values = write_env_config(settings.env_path, model=request.model.strip(), base_url=base_url,
                                  api_key=request.api_key.strip() or stored)
        apply_to_process(values)
        # 保存即登记为一份可选模型档案（按 base_url+model 去重），供输入框选择器切换。
        upsert_profile(settings, base_url=base_url, model=request.model.strip(), api_key=values[API_KEY_KEY])
        return public_model_settings(settings)

    @app.get("/api/settings/models")
    def list_model_profiles():
        return public_profiles(settings)

    @app.post("/api/settings/models/activate")
    def activate_model_profile(request: ProfileActivateRequest):
        try:
            return activate_profile(settings, request.id)
        except ValueError as error:
            raise HTTPException(404, str(error))

    @app.post("/api/check-sessions")
    def create_check_session(request: CheckSessionRequest):
        try:
            return service().create_check_session(request.fixture, request.backend, request.collect, request.report)
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.post("/api/settings/model/test")
    def test_model_settings(request: ModelSettingsUpdate):
        api_key = request.api_key.strip() or settings.api_key
        if not api_key:
            raise HTTPException(400, "请先填写 API Key，或保存过已有配置。")
        return test_connection(request.base_url, api_key, request.model.strip())

    @app.get("/api/fixtures")
    def fixtures():
        return service().fixtures()

    @app.get("/api/repositories")
    def repositories():
        return service().repositories()

    def discover(source):
        try:
            return discover_repository(source, settings)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/repositories/discover")
    def discover_directory(request: DirectoryDiscovery):
        if not request.authorize_directory:
            raise HTTPException(400, "请通过目录选择或识别操作授权读取仓库。")
        return discover(request.source)

    @app.post("/api/repositories/pick-directory")
    def pick_directory(request: DirectoryPick):
        try:
            selected = choose_directory(request.initial)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from None
        return selected if selected["cancelled"] else {"cancelled": False, **discover(selected["source"])}

    @app.post("/api/repositories/preflight")
    def inspect_registration(request: RepositoryRegistration):
        try:
            return service().check_registration(request)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/repositories", status_code=201)
    def register(request: RepositoryRegistration):
        try:
            return service().register(request)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/repositories/{repository_id}")
    def repository(repository_id: str):
        registered = service().store.repository(repository_id)
        if registered is None:
            raise HTTPException(404, "已登记仓库不存在")
        return registered

    @app.put("/api/repositories/{repository_id}")
    def update_repository(repository_id: str, request: RepositoryRegistration):
        try:
            return service().register(request, repository_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/repositories/{repository_id}/preflight")
    def check_repository(repository_id: str, request: RepositoryCheck):
        try:
            return service().check_repository(repository_id, request)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/tasks")
    def tasks(fixture: str | None = None):
        items = service().store.list()
        if fixture:
            items = [task for task in items if task.get("fixture") == fixture]
        return items

    @app.post("/api/tasks", status_code=201)
    def start(request: StartRequest):
        try:
            return service().start(request)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/tasks/{task_id}")
    def get(task_id: str):
        return task_or_404(task_id)

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        task_or_404(task_id)
        try:
            service().delete_task(task_id)
        except ValueError as error:
            raise HTTPException(400, str(error))
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        task_or_404(task_id)
        return {"requested": service().cancel(task_id)}

    @app.post("/api/tasks/{task_id}/resume")
    def resume(task_id: str):
        task_or_404(task_id)
        try:
            return service().resume(task_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/tasks/{task_id}/follow-up")
    def follow_up(task_id: str, request: FollowUpRequest):
        task_or_404(task_id)
        try:
            return service().follow_up(task_id, request.text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def workspace_repository(task):
        profile_data = task.get("repository_profile")
        if not profile_data:
            raise HTTPException(404, "此任务创建于工作区快照功能之前，未记录文件清单；新建任务即可查看。")
        workspace = settings.data_dir / "tasks" / task["id"] / "workspace"
        if not workspace.exists():
            raise HTTPException(404, "工作区尚未创建")
        return Repository(workspace, RepoProfile.model_validate(profile_data))

    @app.get("/api/tasks/{task_id}/workspace/files")
    def workspace_files(task_id: str):
        task = task_or_404(task_id)
        repo = workspace_repository(task)
        files = [{"path": name, "size": repo.path(name).stat().st_size} for name in repo.files()]
        return {"files": files}

    @app.get("/api/tasks/{task_id}/workspace/file")
    def workspace_file(task_id: str, path: str):
        task = task_or_404(task_id)
        try:
            return workspace_repository(task).read(path, 1, 400)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/tasks/{task_id}/apply")
    def apply_patch(task_id: str, request: ApplyRequest):
        task_or_404(task_id)
        try:
            return service().apply_patch(task_id, confirm=request.confirm)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/tasks/{task_id}/apply/undo")
    def undo_apply_patch(task_id: str):
        task_or_404(task_id)
        try:
            return service().undo_apply(task_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/tasks/{task_id}/events")
    def events(task_id: str, after: int = 0):
        task_or_404(task_id)
        return service().store.events(task_id, max(0, after))

    @app.get("/api/tasks/{task_id}/stream")
    async def stream(task_id: str, request: Request, after: int = 0):
        task_or_404(task_id)
        try:
            cursor = max(0, after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "无效的事件游标") from None
        async def generate():
            nonlocal cursor
            heartbeat = 0
            while not await request.is_disconnected():
                rows = service().store.events(task_id, cursor)
                for event in rows:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                task = service().store.get(task_id)
                if task["status"] in TERMINAL and not rows:
                    break
                heartbeat += 1
                if heartbeat % 20 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.4)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/tasks/{task_id}/artifacts/{kind}")
    def artifact(task_id: str, kind: str):
        task_or_404(task_id)
        names = {"patch": "patch.diff", "report": "report.json"}
        if re.fullmatch(r"reproducer-c[1-8]", kind):
            names[kind] = kind + ".py"
        if kind not in names:
            raise HTTPException(404, "产物不存在")
        path = settings.data_dir / "tasks" / task_id / "artifacts" / names[kind]
        if not path.is_file():
            raise HTTPException(404, "产物尚未生成")
        return FileResponse(path, filename=names[kind])

    return app
