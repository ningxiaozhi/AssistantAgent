"""HTTP 接口。

设计：POST /api/chat 立即返回 task_id，不在请求里等模型跑完。
原因：**只有异步化才可能取消**。同步接口里用户点"停"时，手上根本没有可停的东西。
"""
from __future__ import annotations

import io
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.runner import TaskConflict

router = APIRouter()


# ---------------------------------------------------------------- 模型
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None
    channel: str = "api"
    sensitive: bool = Field(
        default=False, description="敏感任务：路由层会强制走本地模型，并抬高确认等级"
    )


class ChatResponse(BaseModel):
    task_id: str
    state: str
    message: str


class CancelRequest(BaseModel):
    reason: str | None = None


class TaskOut(BaseModel):
    id: str
    state: str
    input: str
    output: str | None = None
    error: str | None = None
    steps: int = 0
    version: int = 1
    cancel_requested_at: Any | None = None
    created_at: Any | None = None
    updated_at: Any | None = None


# ---------------------------------------------------------------- 依赖
def ctx(request: Request) -> Any:
    return request.app.state.ctx


# ---------------------------------------------------------------- 路由
@router.get("/health", tags=["ops"])
async def health(request: Request) -> dict[str, Any]:
    c = ctx(request)
    return {
        "status": "ok",
        "db": "up" if c.db.available else "down(内存模式)",
        "agent": c.runtime.name,
        "roles": c.router.all_roles(),
        "running_tasks": len(c.runner.running),
    }


@router.post("/api/chat", response_model=ChatResponse, status_code=202, tags=["chat"])
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    c = ctx(request)
    task = await c.runner.submit(
        user_input=body.message,
        session_id=body.session_id or uuid.uuid4().hex,
        channel=body.channel,
        model_version=c.model_version,
        sensitive=body.sensitive,
    )
    return ChatResponse(
        task_id=task["id"],
        state=task["state"],
        message="已受理。用 GET /api/tasks/{id} 查结果，POST /api/tasks/{id}/cancel 可随时停止。",
    )


@router.get("/api/tasks", tags=["tasks"])
async def list_tasks(request: Request, limit: int = 20) -> list[dict[str, Any]]:
    return await ctx(request).repo.list(limit=min(limit, 100))


@router.get("/api/tasks/{task_id}", tags=["tasks"])
async def get_task(task_id: str, request: Request) -> dict[str, Any]:
    task = await ctx(request).repo.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


@router.post("/api/tasks/{task_id}/cancel", tags=["tasks"])
async def cancel_task(task_id: str, request: Request, body: CancelRequest | None = None) -> dict[str, Any]:
    c = ctx(request)
    try:
        task = await c.runner.cancel(task_id, body.reason if body else None)
    except KeyError:
        raise HTTPException(404, "任务不存在") from None
    except TaskConflict as e:
        raise HTTPException(409, str(e)) from None
    return {
        "task_id": task["id"],
        "state": task["state"],
        "message": "已受理取消，将在下一个安全点停止",
    }


@router.get("/api/tasks/{task_id}/trace", tags=["tasks"])
async def get_trace(task_id: str, request: Request) -> dict[str, Any]:
    """复盘用：一条任务的全部事件。"""
    c = ctx(request)
    if not c.db.available:
        return {"task_id": task_id, "events": [], "note": "内存模式不落 trace，接上数据库后可用"}
    rows = await c.db.fetch(
        "SELECT step_no, kind, payload, at_ms FROM app.trace_event WHERE task_id = $1 ORDER BY id",
        uuid.UUID(task_id),
    )
    return {"task_id": task_id, "events": [dict(r) for r in rows]}


@router.get("/api/tools", tags=["tools"])
async def list_tools(request: Request) -> dict[str, Any]:
    """工具库全景：有哪些库、每库几个工具、权限上限。"""
    c = ctx(request)
    if c.catalog is None:
        return {"available": False, "note": "工具配置未加载"}
    return {
        "available": True,
        "stats": c.catalog.stats(),
        "libraries": c.catalog.summary(),
        "profiles": c.catalog.profiles,
    }


@router.get("/api/tools/route", tags=["tools"])
async def route_tools(
    request: Request,
    q: str,
    profile: str | None = None,
    libraries: str | None = None,
) -> dict[str, Any]:
    """预览工具路由：这句话会命中哪个库、挂哪些工具。

    排查"它为什么没用那个工具"的第一步 —— 先确认那个工具压根没被挂上，
    还是挂上了但模型没选。
    """
    c = ctx(request)
    if c.tool_router is None:
        raise HTTPException(503, "工具配置未加载")
    explicit = [x.strip() for x in libraries.split(",") if x.strip()] if libraries else None
    try:
        selection = c.tool_router.route(q, profile=profile, explicit=explicit)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    return selection.model_dump()


class MemoryWriteRequest(BaseModel):
    text: str = Field(min_length=1)
    kb: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    source: str | None = None


@router.get("/api/memory", tags=["memory"])
async def list_memory(request: Request) -> dict[str, Any]:
    """知识库全景：有哪些库、常驻与否、写入策略。"""
    c = ctx(request)
    if c.memory is None:
        return {"available": False, "note": "记忆配置未加载"}
    return {
        "available": True,
        "libraries": c.memory.config.summary(),
        "profiles": c.memory.config.profiles,
        "defaults": c.memory.config.defaults.model_dump(),
        "stats": c.wiki_store.stats() if hasattr(c.wiki_store, "stats") else {},
    }


@router.get("/api/memory/search", tags=["memory"])
async def search_memory(
    request: Request, q: str, profile: str | None = None, kbs: str | None = None
) -> dict[str, Any]:
    """两段式检索：先定库，再定页。返回的是页面指针，不是正文。"""
    c = ctx(request)
    if c.memory is None:
        raise HTTPException(503, "记忆配置未加载")
    explicit = [x.strip() for x in kbs.split(",") if x.strip()] if kbs else None
    try:
        selection = await c.memory.route(q, profile=profile, explicit=explicit)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    return selection.model_dump()


@router.get("/api/memory/index", tags=["memory"])
async def memory_index(request: Request, kb: str) -> dict[str, Any]:
    """一个库的索引：注入 prompt 的就是这段（不含正文）。"""
    c = ctx(request)
    if c.memory is None:
        raise HTTPException(503, "记忆配置未加载")
    if kb not in c.memory.config.libraries:
        raise HTTPException(404, f"未知的库: {kb}")
    return {"kb": kb, "index": await c.memory.index_for(kb)}


@router.post("/api/memory/pages", status_code=201, tags=["memory"])
async def write_memory(request: Request, body: MemoryWriteRequest) -> dict[str, Any]:
    """写入：能并进已有页面就绝不新建（避免同一件事有五个版本）。"""
    c = ctx(request)
    if c.memory_writer is None:
        raise HTTPException(503, "记忆配置未加载")
    try:
        return await c.memory_writer.write(
            text=body.text,
            kb=body.kb,
            title=body.title,
            description=body.description,
            tags=body.tags,
            source=body.source,
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "name": "AssistantAgent",
        "docs": "/docs",
        "health": "/health",
        "chat": "POST /api/chat",
        "cancel": "POST /api/tasks/{id}/cancel",
        "tools": "GET /api/tools",
        "route": "GET /api/tools/route?q=...",
        "memory": "GET /api/memory",
        "memory_search": "GET /api/memory/search?q=...",
    }
