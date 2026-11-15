# -*- coding: utf-8 -*-
"""文案生产路由（功能及流程优化 v2）。

①选题搭建 / ②脚本创作 / ③配音生成 / ④封面制作 / ⑤打包文案。
全部接口要求登录（游客在中间件层已被拦截）；数据按 owner 用户隔离。
生成类接口把同步 AI 调用丢进线程池执行，避免阻塞事件循环。
所有变更接口统一返回最新快照（{"ok":true,"data":snapshot}），前端一次渲染。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from .auth import current_user
from ..services import material as mat
from ..services import space as space_svc

router = APIRouter()


def _user(request: Request) -> dict:
    return current_user(request)


def _err(message: str):
    return {"ok": False, "error": message}


def _snap(uid: str) -> dict:
    return {"ok": True, "data": mat.snapshot(uid)}


async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args))


# ------------------------------ 状态 ------------------------------

@router.get("/api/material/state")
async def material_state(request: Request):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    return _snap(u["id"])


# ------------------------------ ① 选题库 ------------------------------

@router.post("/api/material/topics/generate")
async def topics_generate(request: Request, body: dict = None):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    b = body or {}
    try:
        await _run(mat.generate_library, u, b.get("prompt", ""),
                   b.get("count") or 10, b.get("model_id") or "")
    except mat.MaterialError as e:
        return _err(e.message)
    except Exception as e:
        return _err("生成失败：" + str(e)[:200])
    return _snap(u["id"])


@router.post("/api/material/topics/{lid}/reset-used")
async def topics_reset(request: Request, lid: str):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    try:
        await _run(mat.reset_library_used, u["id"], lid)
    except mat.MaterialError as e:
        return _err(e.message)
    return _snap(u["id"])


@router.delete("/api/material/topics/{lid}")
async def topics_delete(request: Request, lid: str):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    await _run(mat.delete_library, u["id"], lid)
    return _snap(u["id"])


# ------------------------------ ② 脚本 ------------------------------

@router.post("/api/material/scripts/generate-one")
async def scripts_generate_one(request: Request, body: dict = None):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    b = body or {}
    try:
        await _run(mat.generate_scripts, u, b.get("library_id") or "",
                   b.get("topic_ids") or [], b.get("prompt", ""),
                   b.get("model_id") or "")
    except mat.MaterialError as e:
        return {"ok": False, "error": e.message}
    except Exception as e:
        return {"ok": False, "error": "生成失败：" + str(e)[:200]}
    return _snap(u["id"])


@router.post("/api/material/scripts/{sid}")
async def script_update(request: Request, sid: str, body: dict = None):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    try:
        await _run(mat.update_script, u["id"], sid, body or {})
    except mat.MaterialError as e:
        return _err(e.message)
    return _snap(u["id"])


@router.delete("/api/material/scripts/{sid}")
async def script_delete(request: Request, sid: str):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    await _run(mat.delete_script, u["id"], sid)
    return _snap(u["id"])


@router.delete("/api/material/scripts")
async def scripts_delete_batch(request: Request, body: dict = None):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    ids = (body or {}).get("ids") or []
    if not ids:
        return _err("未指定要删除的脚本")
    try:
        await _run(mat.delete_scripts, u["id"], ids)
    except mat.MaterialError as e:
        return _err(e.message)
    return _snap(u["id"])


# ------------------------------ ③ 配音 ------------------------------

@router.post("/api/material/voices/generate-one")
async def voices_generate_one(request: Request, body: dict = None):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    b = body or {}
    try:
        await _run(mat.generate_voice, u, b.get("script_id") or "",
                   b.get("model_id") or "", b.get("voice_id"))
    except mat.MaterialError as e:
        # 失败也带最新快照：脚本上的失败原因要在界面展示
        return {"ok": False, "error": e.message, "data": mat.snapshot(u["id"])}
    except Exception as e:
        return {"ok": False, "error": "配音失败：" + str(e)[:200]}
    return _snap(u["id"])


# ------------------------------ ④ 封面 ------------------------------

@router.post("/api/material/covers/upload")
async def covers_upload(request: Request,
                        files: list[UploadFile] = File(default=None),
                        space_files: str = Form(default="")):
    """上传封面底图：本机用户直接传文件；在线用户传 space_files（我的空间 rel 数组，支持目录），
    服务端从空间读取后入库。两者可混用，都按「仅图片」过滤。"""
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    items = []
    for f in files or []:
        if not f.filename:
            continue
        items.append((f.filename, await f.read()))
    if space_files:
        import json as _json
        try:
            rels = _json.loads(space_files)
        except Exception:
            rels = []
        for name, data in space_svc.collect_space_files(
                u["id"], rels if isinstance(rels, list) else [], ["image"]):
            items.append((name, data))
    if not items:
        return _err("未选择图片文件（封面底图仅支持 jpg / png / webp / bmp）")
    try:
        await _run(mat.upload_cover_src, u["id"], items)
    except Exception as e:
        return _err("上传失败：" + str(e)[:200])
    return _snap(u["id"])


@router.post("/api/material/covers/generate")
async def covers_generate(request: Request, body: dict = None):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    b = body or {}
    try:
        await _run(mat.generate_covers, u["id"], b.get("template_id") or "")
    except mat.MaterialError as e:
        return _err(e.message)
    except Exception as e:
        return _err("封面生成失败：" + str(e)[:200])
    return _snap(u["id"])


@router.post("/api/material/covers/src/clear")
async def covers_src_clear(request: Request):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    await _run(mat.clear_cover_src, u["id"])
    return _snap(u["id"])


@router.post("/api/material/covers/out/clear")
async def covers_out_clear(request: Request):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    await _run(mat.clear_cover_out, u["id"])
    return _snap(u["id"])


@router.delete("/api/material/covers/src/{cid}")
async def covers_src_delete(request: Request, cid: str):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    await _run(mat.delete_cover_src, u["id"], cid)
    return _snap(u["id"])


@router.delete("/api/material/covers/out/{cid}")
async def covers_out_delete(request: Request, cid: str):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    await _run(mat.delete_cover_out, u["id"], cid)
    return _snap(u["id"])


# ------------------------------ ⑤ 素材包 ------------------------------

@router.post("/api/material/projects")
async def projects_create(request: Request, body: dict = None):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    try:
        await _run(mat.create_project, u["id"], body or {})
    except mat.MaterialError as e:
        return _err(e.message)
    except Exception as e:
        return _err("创建失败：" + str(e)[:200])
    return _snap(u["id"])


@router.get("/api/material/projects/{pid}")
async def projects_detail(request: Request, pid: str):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    try:
        d = mat.project_detail(u["id"], pid)
    except mat.MaterialError as e:
        return _err(e.message)
    return {"ok": True, "data": d}


@router.delete("/api/material/projects/{pid}")
async def projects_delete(request: Request, pid: str):
    u = _user(request)
    if not u.get("id"):
        return _err("请登录后使用文案生产")
    await _run(mat.delete_project, u["id"], pid)
    return _snap(u["id"])


# ------------------------------ 物料文件 ------------------------------

@router.get("/api/material/asset/{name}")
async def material_asset(name: str, request: Request):
    """物料文件（配音 mp3 / 封面底图 / 生成封面）：仅限本人目录内、禁止路径逃逸。"""
    u = _user(request)
    if not u.get("id"):
        return _err("请登录")
    if (not name or Path(name).name != name
            or name in (".", "..")):
        return _err("非法文件名")
    root = mat.mat_root(u["id"])
    for sub in (mat.VOICE_SUBDIR, mat.COVER_SRC_SUBDIR, mat.COVER_OUT_SUBDIR):
        p = root / sub / name
        if p.is_file():
            return FileResponse(str(p))
    return _err("文件不存在")
