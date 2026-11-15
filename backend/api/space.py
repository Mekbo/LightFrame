"""我的空间 API（阶段 6 前重要功能）。

所有接口要求登录；资源严格按 uid 隔离（服务端会话解析，不接收任何 uid 参数）。
管理员额外可调整任意用户配额。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request, File, UploadFile, Form
from fastapi.responses import FileResponse, JSONResponse

from ..core import auth, policy
from ..core.config import load_config, save_config
from ..services import space as svc

router = APIRouter()


def _u(request: Request) -> dict:
    u = auth.user_from_request(request)
    if policy.is_guest(u):
        return {}
    return u


def _uid(request: Request) -> str:
    return _u(request).get("id") or ""


def _ok(**kw):
    return {"ok": True, **kw}


def _err(msg, status=400):
    return JSONResponse(status_code=status,
                        content={"ok": False, "error": msg})


@router.get("/api/space/info")
def space_info(request: Request):
    uid = _uid(request)
    if not uid:
        return _err("请登录后使用我的空间")
    from ..main import is_local_client
    info = svc.space_info(uid)
    # 访问来源：服务器本机（直接读本地磁盘）or 在线用户（一律从空间取素材）
    info["local_client"] = bool(is_local_client(request))
    return _ok(**info)


@router.get("/api/space/list")
def space_list(request: Request, path: str = "", page: int = 1,
               per_page: int = 10):
    uid = _uid(request)
    if not uid:
        return _err("请登录后使用我的空间")
    try:
        return _ok(**svc.list_dir(uid, path, int(page), int(per_page)))
    except ValueError as e:
        return _err(str(e))


@router.get("/api/space/expand")
def space_expand(request: Request, path: str = "", kinds: str = "image,video,file"):
    uid = _uid(request)
    if not uid:
        return _err("请登录后使用我的空间")
    try:
        ks = [k.strip() for k in kinds.split(",") if k.strip()]
        items = svc.expand_dir(uid, path, ks)
        return _ok(items=items, dir=path, count=len(items))
    except ValueError as e:
        return _err(str(e))


@router.post("/api/space/mkdir")
def space_mkdir(request: Request, payload: dict = Body(default={})):
    uid = _uid(request)
    if not uid:
        return _err("请登录后使用我的空间")
    try:
        return _ok(**svc.create_folder(uid, payload.get("path", ""),
                                       payload.get("name", "")))
    except ValueError as e:
        return _err(str(e))


@router.post("/api/space/upload")
async def space_upload(request: Request,
                       path: str = Form(""),
                       files: list[UploadFile] = File(...)):
    uid = _uid(request)
    if not uid:
        return _err("请登录后使用我的空间")
    items = []
    try:
        for f in files or []:
            if not f.filename:
                continue
            items.append((f.filename, await f.read()))
    except Exception as e:
        return _err("读取上传失败：" + str(e)[:120])
    try:
        results = svc.save_upload(uid, path, items)
    except ValueError as e:
        return _err(str(e))
    return _ok(results=results)


@router.post("/api/space/rename")
def space_rename(request: Request, payload: dict = Body(default={})):
    uid = _uid(request)
    if not uid:
        return _err("请登录后使用我的空间")
    try:
        return _ok(**svc.rename_path(uid, payload.get("path", ""),
                                     payload.get("name", "")))
    except ValueError as e:
        return _err(str(e))


@router.post("/api/space/move")
def space_move(request: Request, payload: dict = Body(default={})):
    uid = _uid(request)
    if not uid:
        return _err("请登录后使用我的空间")
    try:
        return _ok(**svc.move_paths(uid, payload.get("paths", []),
                                    payload.get("dst", "")))
    except ValueError as e:
        return _err(str(e))


@router.post("/api/space/delete")
def space_delete(request: Request, payload: dict = Body(default={})):
    uid = _uid(request)
    if not uid:
        return _err("请登录后使用我的空间")
    try:
        return _ok(**svc.delete_paths(uid, payload.get("paths", [])))
    except ValueError as e:
        return _err(str(e))


@router.get("/api/space/file")
def space_file(request: Request, path: str = "", inline: int = 1):
    uid = _uid(request)
    if not uid:
        return _err("请登录后使用我的空间")
    meta = svc.file_meta(uid, path)
    if not meta:
        return _err("文件不存在或无权限", status=404)
    p = svc.resolve_file(uid, path)
    if not p:
        return _err("文件不存在", status=404)
    return FileResponse(
        str(p), media_type=meta["mime"], filename=meta["name"],
        content_disposition_type="inline" if inline else "attachment")


# ------------------------------ 管理员：配额 ------------------------------

@router.put("/api/admin/users/{uid}/quota")
def admin_set_quota(uid: str, request: Request, payload: dict = Body(default={})):
    actor = _u(request)
    if actor.get("role") != "admin":
        return _err("仅管理员可调整配额", status=403)
    target = auth.get_user(uid)
    if not target:
        return _err("账户不存在", status=404)
    q = payload.get("quota_bytes")
    if q is None:
        q = payload.get("quota_gb")
        if q is None:
            return _err("缺少 quota_bytes 或 quota_gb")
        try:
            q = int(q) * (1024 ** 3)
        except Exception:
            return _err("quota_gb 必须是数字")
    try:
        q = int(q)
    except Exception:
        return _err("配额必须是数字（字节）")
    if q < 0:
        return _err("配额不能为负")
    try:
        auth.update_user(uid, space_quota_bytes=q)
    except ValueError as e:
        return _err(str(e))
    auth.audit(actor, "admin.space.quota", uid)
    return _ok(user=auth.public_user(auth.get_user(uid) or {}))


@router.get("/api/admin/space/quotas")
def admin_quotas(request: Request):
    actor = _u(request)
    if actor.get("role") != "admin":
        return _err("仅管理员可查看配额总览", status=403)
    rows = []
    for u in auth.list_users():
        uid = u.get("id")
        info = svc.space_info(uid)
        rows.append({
            "id": uid, "username": u.get("username"), "role": u.get("role"),
            "quota_bytes": info["quota_bytes"], "used_bytes": info["used_bytes"],
            "usage_ratio": info["usage_ratio"],
        })
    return _ok(rows=rows)
