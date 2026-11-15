import asyncio
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from PIL import Image, ImageDraw

from fastapi import FastAPI, File, UploadFile, Form, Body, HTTPException, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional
import ipaddress

from .core.config import (load_config, save_config, IMAGE_OUTPUT_DIR,
                          VIDEO_OUTPUT_DIR, output_dir_for)
from .core import auth, policy
from .api.auth import router as auth_router, current_user, fail
from .api.ai import router as ai_router
from .api.material import router as material_router
from .api.space import router as space_router
from .services import image as image_svc
from .services import templates as tpl_svc
from .services import video_templates as vtpl_svc
from .services import fonts as font_svc
from .services import video as video_svc
from .services import livephoto as live_svc
from .services import prompt_templates as ptpl_svc
from .services import app_update as update_svc
from .services import developer_contact as dev_svc
from .services import space as space_svc
from .services import material as mat_svc
from .services.taskmgr import TaskManager

BASE = Path(__file__).resolve().parent.parent
# 兼容旧版：仍确保「文档/LightFrame/OutPut_*」存在（老配置的绝对路径可能仍指向此处）
for _d in (IMAGE_OUTPUT_DIR, VIDEO_OUTPUT_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def out_base(kind: str = "image", uid: str = "") -> Path:
    """输出目录：按「空间/<uid>/<作品根>/<图片|视频输出名>」解析（相对路径、按用户隔离）；

    产物路径由系统按目录名自动推导填写，任何用户（含管理员）不可自定义绝对路径；
    输出子目录名每用户可在全局设置或空间中修改，两边自动同步。
    """
    cfg = load_config(uid)
    root = space_svc.works_root_name(cfg)
    sub = (space_svc.vid_out_name(cfg) if kind == "video"
           else space_svc.img_out_name(cfg))
    base = space_svc.user_dir(uid) / root / sub
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base


# 服务端物料引用前缀：mat:<文件名> 指向「文案生产」素材包里已存在的配音/封面。
# 在线用户导入文案包后用这种引用提交，不再把服务端已有文件重新上传一遍。
MAT_PREFIX = "mat:"


def _space_files(uid: str, raw: str, kinds: list) -> list:
    """解析 server_paths / server_covers / server_bgm（JSON 数组，元素为 rel 或 mat:<文件名>）。

    - rel：来自「我的空间」，可为文件或目录（递归展开、按 kinds 过滤）；
    - mat:<文件名>：文案生产素材包物料，直接按文件名从服务端读取。
    逐条展开以保持与传入顺序一致（封面/脚本按序号一一对应，顺序不能乱）。
    """
    rels = []
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                rels = [str(x) for x in v if x]
        except Exception:
            pass
    if not rels:
        return []
    out = []
    for r in rels:
        if r.startswith(MAT_PREFIX):
            item = mat_svc.read_material_asset(uid, r[len(MAT_PREFIX):])
            if item:
                out.append(item)
        else:
            out.extend(space_svc.collect_space_files(uid, [r], kinds))
    return out


def _space_voices(uid: str, raw: str) -> Optional[list]:
    """解析 server_voices（与脚本索引一一对应的数组，元素为 rel / mat:<文件名> / null）。
    返回按脚本顺序的 (文件名, 字节) 或 None；null / 读失败保留为 None 交由调用方校验。"""
    if not raw:
        return None
    try:
        arr = json.loads(raw)
        if not isinstance(arr, list):
            return None
    except Exception:
        return None
    out = []
    for x in arr:
        if not x:
            out.append(None)
            continue
        s = str(x)
        item = (mat_svc.read_material_asset(uid, s[len(MAT_PREFIX):])
                if s.startswith(MAT_PREFIX)
                else space_svc.read_space_file(uid, s))
        out.append(item)
    return out

app = FastAPI(title="光影工坊工作台")
app.include_router(auth_router)
app.include_router(ai_router)
app.include_router(material_router)
app.include_router(space_router)
mgr = TaskManager()
_startup_cfg = load_config()
mgr.semaphore.configure_sync(_startup_cfg.get("concurrency", 2),
                             _startup_cfg.get("max_concurrency", 5))

# 仅管理员可修改的全局配置字段（普通用户只能保存自己的加工参数）
# 注：output_dir / video_output_dir 已改为按用户「我的空间」目录名推导。
#     space_root（作品根目录名）与两个绝对路径键由系统锁定，
#     任何用户（含管理员）均不可修改；update_config 会强制丢弃这些键。
GLOBAL_ONLY_KEYS = {
    "port", "concurrency", "max_concurrency", "ffmpeg_path",
    "originality_enabled",
    "originality_k", "server", "ai", "preview_count", "update_check_cycle",
    "developer_wechat", "developer_wechat_link",
    "space_quota_admin_gb", "space_quota_user_gb",
    "space_root", "output_dir", "video_output_dir",
}
# 系统锁定键：产物路径由系统自动填写，任何用户（含管理员）不可修改
SYSTEM_LOCKED_KEYS = ("space_root", "output_dir", "video_output_dir")


# ------------------------------ 本机 / 在线用户识别 ------------------------------
# 素材添加方式按「访问来源」自动切换：仅通过 localhost / 127.0.0.1 / ::1 访问
# 视为服务器本机用户（直接读本地磁盘）；其余一律视为在线用户（从「我的空间」取素材）。
# 注意：从服务器本机用局域网 IP（如 192.168.1.10:2026）访问时，来源地址是本机网卡 IP
# 而非回环地址，按在线用户处理 —— 是否本机只看「怎么访问」而非「从哪台电脑访问」。


def is_local_client(request: Request) -> bool:
    """本地用户判定：仅回环地址（localhost / 127.0.0.1 / ::1）访问返回 True。"""
    try:
        host = ((request.client.host or "") if request.client else "").lower()
    except Exception:
        host = ""
    return host in ("127.0.0.1", "::1", "localhost")


def client_ip(request: Request, trust_proxy: bool = False) -> str:
    """取出访客 IP：信任代理时优先读 X-Forwarded-For 首段；否则取直连客户端。"""
    if trust_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    if request.client:
        return request.client.host or ""
    return ""


def ip_allowed(ip: str, allowlist) -> bool:
    """局域网白名单：loopback 永远放行；allowlist 支持 CIDR 与单 IP。空列表 = 不限制。"""
    if not allowlist:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    for entry in allowlist:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            try:
                if addr == ipaddress.ip_address(entry):
                    return True
            except ValueError:
                continue
    return False


def apply_cors(request: Request, resp: Response, allowed_origins) -> None:
    """同源策略（阶段 5）：allowed_origins 为空 = 纯同源（浏览器自动拦截跨域，不下发任何 CORS 头）；

    非空时仅反射显式列出的 Origin（绝不回 `*`，避免任意跨域读取凭据）。
    """
    if not allowed_origins:
        return
    origin = request.headers.get("origin")
    if not origin or origin not in allowed_origins:
        return
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Vary"] = "Origin"


@app.middleware("http")
async def security_guard(request: Request, call_next):
    """统一鉴权 + 局域网边界（阶段 5）：默认拒绝，白名单外一律要求登录；身份只从服务端会话解析。

    额外：lan_allow 非空时仅放行白名单网段（loopback 永远放行，保证本机管理不受限）。
    """
    user = auth.user_from_request(request)
    try:
        request.state.user = user
    except Exception:
        pass
    # 局域网访问控制
    _srv = (load_config().get("server") or {})
    if not ip_allowed(client_ip(request, _srv.get("trust_proxy_headers", False)),
                     _srv.get("lan_allow") or []):
        return JSONResponse(
            status_code=403,
            content={"ok": False,
                     "error": {"code": "LAN_BLOCKED",
                               "message": "该 IP 不在允许访问的局域网范围内"}})
    allowed, code, msg = policy.evaluate(
        request.method, request.url.path, request.query_params,
        user.get("role", "guest"))
    if not allowed:
        r = JSONResponse(
            status_code=403,
            content={"ok": False, "error": {"code": code, "message": msg}})
        apply_cors(request, r, (load_config().get("server") or {}).get("allowed_origins") or [])
        return r
    resp = await call_next(request)
    apply_cors(request, resp, (load_config().get("server") or {}).get("allowed_origins") or [])
    return resp


def cu(request: Request) -> dict:
    """当前用户（中间件注入，异常时回退 Cookie 解析）。"""
    return current_user(request)


def visible_tasks(user: dict) -> list:
    """管理员见全部（含 legacy），普通用户仅见 owner_id == 自己，游客不可见。

    先取快照再过滤：注册任务在事件循环、任务列表在线程池，避免并发改字典时迭代抛错。
    """
    if policy.is_guest(user):
        return []
    with mgr._lock:
        snapshot = list(mgr.tasks.values())
    if user.get("role") == "admin":
        return snapshot
    uid = user.get("id") or ""
    return [t for t in snapshot if t.get("owner_id") == uid]


def owned_task(tid: str, request: Request):
    """按 owner_id 精确校验归属；越权返回 None。"""
    t = mgr.tasks.get(tid)
    if not t or not policy.owns(t, cu(request)):
        return None
    return t


def allowed_roots(user: dict) -> list:
    """产物允许根：用户的「我的空间」输出目录 + 默认目录 + 全局/个人配置里的旧输出目录。

    空间迁移后产物统一落 space/<uid>/<作品根>/OutPut_*，必须纳入白名单，
    否则预览 / 打包 / 在线产物接口会误判越权（无权访问该任务产物）。
    """
    raw = [IMAGE_OUTPUT_DIR, VIDEO_OUTPUT_DIR]
    uid = user.get("id", "")
    if uid:
        try:
            raw.extend([out_base("image", uid), out_base("video", uid)])
        except Exception:
            pass
    for cfg in (load_config(), load_config(uid)):
        for k in ("output_dir", "video_output_dir"):
            if cfg.get(k):
                raw.append(Path(str(cfg[k])))
    roots = []
    for r in raw:
        try:
            roots.append(Path(r).resolve())
        except Exception:
            pass
    return roots


def path_allowed(p, user: dict) -> bool:
    """路径白名单：产物目录必须落在允许根之内，杜绝 .. 逃逸与任意路径写入。"""
    try:
        rp = Path(p).resolve()
        return any(rp == r or r in rp.parents
                   for r in allowed_roots(user))
    except Exception:
        return False


def basename_ok(name: str) -> bool:
    return (bool(name) and Path(name).name == name
            and name not in (".", "..")
            and "/" not in name and "\\" not in name)


def resolve_base(kind: str, user: dict, out_dir: str = "") -> Path:
    """任务输出根：前端传入的自定义目录只有在白名单内才生效。

    管理员（本机主人）不受限；普通用户越界路径静默回落到个人配置目录，
    避免局域网客户端把文件写到服务器任意位置。
    """
    if out_dir and user.get("role") == "admin":
        return Path(out_dir)
    if out_dir and path_allowed(out_dir, user):
        return Path(out_dir)
    return out_base(kind, user.get("id", ""))


def save_personal_config(uid: str, patch: dict) -> dict:
    """普通用户只保存自己的加工参数，不把全局配置快照写进个人文件。

    若沿用 save_config() 的全量写入，个人文件会冻结一份全局字段，
    之后管理员改并发/输出目录会被旧值覆盖，因此这里只写差异键。
    """
    from .core.config import USERS_DIR
    p = USERS_DIR / f"{auth.safe_uid(uid)}.json"
    cur = {}
    try:
        if p.exists():
            cur = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        cur = {}
    cur = {k: v for k, v in cur.items() if k not in GLOBAL_ONLY_KEYS}
    cur.update({k: v for k, v in (patch or {}).items()
                if k not in GLOBAL_ONLY_KEYS})
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass
    return load_config(uid)

def open_folder(path: str) -> bool:
    """在操作系统文件管理器中打开目录（本地优先：服务器与用户同机）。"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(os.path.abspath(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", os.path.abspath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.abspath(path)])
        return True
    except Exception as e:
        print("open_folder failed:", e)
        return False


app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.get("/")
def index():
    return FileResponse(str(BASE / "static" / "index.html"))


@app.get("/api/config")
def get_config(request: Request):
    # 不再接受 ?uid= 参数：身份只从会话解析，杜绝伪造
    return load_config(cu(request)["id"])


@app.post("/api/config")
async def update_config(request: Request, payload: dict = None):
    user = cu(request)
    patch = dict(payload or {})
    # 系统锁定键强制丢弃：作品根目录名与绝对输出路径一律由系统管理（含管理员）
    for k in SYSTEM_LOCKED_KEYS:
        patch.pop(k, None)
    # 记录改前配置，用于输出子目录改名后的磁盘同步
    old_cfg = load_config(user["id"])
    if user.get("role") == "admin":
        # 输出子目录名是「每用户」设置：即使管理员修改，也只写入自己的个人配置，
        # 不落全局（避免影响其他未单独设置过的账户）
        personal = {k: v for k, v in patch.items()
                    if k in ("space_img_out", "space_vid_out")}
        patch = {k: v for k, v in patch.items() if k not in personal}
        merged = save_config("", patch)
        if personal:
            space_svc._set_user_key(user["id"], personal)
            merged = {**merged, **personal}
        # 并发设置保存后即时生效，无需重启；上限提高会唤醒排队任务自动补位
        try:
            await mgr.semaphore.configure(
                limit=merged.get("concurrency", 2),
                ceiling=merged.get("max_concurrency", 5))
        except Exception as e:
            print("apply concurrency failed:", e)
    else:
        # 普通用户只能保存自己的加工参数，全局字段一律过滤
        merged = save_personal_config(user["id"], patch)
    # 全局设置中修改输出子目录名 → 磁盘目录原地改名（产物保留），与空间面板改名等价
    try:
        sync = space_svc.sync_out_dirs(user["id"], old_cfg, merged)
        if sync.get("renamed"):
            print("space out dirs renamed:", sync["renamed"])
        if sync.get("locked"):
            print("space root locked:", sync["locked"])
    except Exception as e:
        print("sync_out_dirs failed:", e)
    return merged


@app.get("/api/system/update-status")
def system_update_status(request: Request, force: int = 0):
    """「关于应用 → 系统更新」：当前版本、功能更新日志，以及按检查周期得出的版本状态。
    游客可只读；force=1 为手动「立即检查更新」，绕过周期立即联网核对。

    开源仓库地址（含 Release 页面链接）仅管理员可见：
    普通用户与游客连静态页里都不出现该地址，接口层同样不下发。
    """
    data = update_svc.get_status(force=bool(force))
    is_admin = cu(request).get("role") == "admin"
    data["repo_visible"] = is_admin
    if not is_admin:
        for k in ("repo_url", "repo_web", "release_url", "check_source"):
            data[k] = ""
        # 状态说明里也不出现「开源仓库」字样，避免向非管理员透露发布渠道
        data["status_hint"] = str(data.get("status_hint") or "").replace(
            "开源仓库暂未发布在线版本记录", "官方尚未发布在线版本记录").replace(
            "开源仓库", "官方发布渠道")
        data["release_notes"] = str(data.get("release_notes") or "").replace(
            "https://github.com/", "")
    return data


@app.post("/api/system/update-cycle")
def system_update_cycle(payload: dict = Body(default={})):
    """设置系统更新检查周期（week / month / off）；登录用户均可修改，游客由中间件拒绝。"""
    try:
        return update_svc.set_cycle((payload or {}).get("cycle"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------- 开发者微信：公开只读 + 仅管理员写入 / 删除 ----------------

@app.get("/api/public/developer-contact")
def developer_contact_get():
    """公开只读：开发者微信号与二维码地址（游客也能在「开发者说」看到联系方式）。"""
    return dev_svc.public_view()


@app.get("/api/public/developer-qr.png")
def developer_contact_qr():
    """返回管理员上传的微信官方二维码图片；未上传时返回 404（系统不再生成二维码）。"""
    try:
        png = dev_svc.qr_png()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="二维码读取失败：%s" % str(e)[:120])
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/api/admin/developer-contact")
def developer_contact_set(request: Request, payload: dict = Body(default={})):
    """设置 / 修改开发者微信号（仅管理员）。"""
    if cu(request).get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可设置开发者微信")
    try:
        return dev_svc.set_wechat((payload or {}).get("wechat"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/admin/developer-contact")
def developer_contact_del(request: Request):
    """删除开发者微信号（仅管理员）；官方二维码图片保留。"""
    if cu(request).get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可删除开发者微信")
    return dev_svc.delete_wechat()


@app.post("/api/admin/developer-qr-image")
def developer_qr_image_set(request: Request, payload: dict = Body(default={})):
    """上传微信官方「我的二维码」图片（仅管理员）——原样展示，扫码可直接添加。"""
    if cu(request).get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可上传微信二维码")
    try:
        return dev_svc.save_official_qr((payload or {}).get("image"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail="图片处理失败：%s" % str(e)[:120])


@app.delete("/api/admin/developer-qr-image")
def developer_qr_image_del(request: Request):
    """删除官方二维码图片（仅管理员）；删除后仅展示微信号文字。"""
    if cu(request).get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可删除微信二维码")
    return dev_svc.delete_official_qr()


@app.get("/api/concurrency")
def get_concurrency():
    """实时并发状态：当前并发数 / 最高限制 / 正在执行数。"""
    return mgr.semaphore.status()


def _task_created_at(t: dict) -> str:
    """兼容旧任务：无 created_at 时按 started_at → 产物目录时间戳推断创建日期。"""
    if t.get("created_at"):
        return str(t["created_at"])
    if t.get("started_at"):
        return str(t["started_at"])
    m = re.search(r"(\d{8})_(\d{6})", Path(t.get("out_dir") or "").name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2),
                                     "%Y%m%d%H%M%S").isoformat()
        except ValueError:
            pass
    return ""


@app.get("/api/tasks")
def list_tasks(request: Request):
    # 管理员见全部（含 legacy），普通用户仅见自己 owner_id 的任务
    user = cu(request)
    tasks = visible_tasks(user)
    snap = mgr.semaphore.snapshot()
    # 全局 FIFO 顺序（按提交序号），据此给出「前面有几位在排队」
    pending = mgr.queue_overview()
    ahead_map = {t["id"]: i for i, t in enumerate(pending)}
    uid = user.get("id") or ""
    my_queued = 0
    out = []
    for t in tasks:
        d = dict(t)
        d["created_at"] = _task_created_at(t)
        d["download_count"] = int(d.get("download_count") or 0)
        queued = t["id"] in ahead_map
        d["queued"] = queued
        d["queue_pos"] = ahead_map[t["id"]] + 1 if queued else 0
        d["queue_ahead"] = ahead_map[t["id"]] if queued else 0
        if queued:
            d["stage"] = "排队中"
            if t.get("owner_id") == uid:
                my_queued += 1
        out.append(d)
    # 告知前端访客是否为本机用户：本机「查看=打开目录」，局域网「查看=打包下载」
    srv = (load_config("").get("server") or {})
    try:
        local = ipaddress.ip_address(
            client_ip(request, srv.get("trust_proxy_headers", False))).is_loopback
    except ValueError:
        local = False
    resp = JSONResponse(out)
    resp.headers["X-Local-Client"] = "1" if local else "0"
    # 全局调度状态随任务列表一并返回，前端无需额外轮询
    resp.headers["X-Queue-Concurrency"] = str(snap["concurrency"])
    resp.headers["X-Queue-Max"] = str(snap["max_concurrency"])
    resp.headers["X-Queue-Active"] = str(snap["active"])
    resp.headers["X-Queue-Waiting"] = str(len(pending))
    resp.headers["X-Queue-My-Waiting"] = str(my_queued)
    return resp


@app.get("/api/tasks/queue")
def queue_status(request: Request):
    """全局调度概览：管理员并发设置、正在执行数、FIFO 队列与当前用户排队情况。"""
    user = cu(request)
    snap = mgr.semaphore.snapshot()
    pending = mgr.queue_overview()
    pos = {t["id"]: i for i, t in enumerate(pending)}
    uid = user.get("id") or ""
    mine = [pos[t["id"]] for t in pending if t.get("owner_id") == uid]
    return {
        "ok": True,
        "concurrency": snap["concurrency"],
        "max_concurrency": snap["max_concurrency"],
        "active": snap["active"],
        "waiting_count": len(pending),
        "my_queued": len(mine),
        "my_next_ahead": min(mine) if mine else 0,
        "queue": [{"id": t["id"], "pos": i + 1, "ahead": i,
                   "owner_id": t.get("owner_id", ""),
                   "kind": t.get("kind", ""), "label": t.get("label", "")}
                  for i, t in enumerate(pending)],
    }


@app.get("/api/tasks/{tid}/preview")
def preview_task(tid: str, request: Request):
    """成品预览：从任务产物里随机抽若干个（图片/视频混选，数量取全局设置 preview_count，默认 1，范围 1–6）供本机与局域网用户在线查看。

    产物经 /api/output/{name}?tid= 提供，沿用同一套归属与目录白名单校验。
    """
    t = owned_task(tid, request)
    if not t:
        return fail("FORBIDDEN", "无权访问该任务")
    user = cu(request)
    od = t.get("out_dir") or ""
    if not od or not path_allowed(od, user):
        return fail("FORBIDDEN", "无权访问该任务产物")
    VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv"}
    IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    pool = []
    for name in (t.get("outputs") or []):
        if not basename_ok(name):
            continue
        ext = Path(name).suffix.lower()
        kind = "video" if ext in VIDEO_EXT else ("image" if ext in IMAGE_EXT else "")
        if not kind:
            continue
        if (Path(od) / name).exists():
            pool.append({"name": name, "type": kind,
                         "url": f"/api/output/{quote(name)}?tid={quote(tid)}"})
    if not pool:
        return {"ok": False, "error": "该任务暂无成品（或产物已被删除）"}
    videos = [x for x in pool if x["type"] == "video"]
    images = [x for x in pool if x["type"] == "image"]
    # 随机预览数量取全局设置 preview_count（默认 1，范围 1–6），管理员/个人可配置
    preview_count = int((load_config("").get("preview_count")) or 1)
    preview_count = max(1, min(6, preview_count))
    # 有视频优先露脸 1 个视频；其余名额用图片/视频补足，凑不够就给现有的
    picks = []
    if videos:
        picks.append(videos.pop(random.choice(range(len(videos)))))
    rest = videos + images
    random.shuffle(rest)
    if len(picks) < preview_count:
        picks += rest[:preview_count - len(picks)]
    picks = picks[:preview_count]
    return {"ok": True, "data": {"items": picks, "total": len(pool),
                                 "kind": t.get("kind"), "task_id": tid}}


@app.post("/api/tasks/{tid}/cancel")
def cancel_task(tid: str, request: Request):
    if not owned_task(tid, request):
        return fail("FORBIDDEN", "无权操作该任务")
    mgr.cancel(tid)
    return {"ok": True}


@app.post("/api/tasks/{tid}/pause")
def pause_task_api(tid: str, request: Request):
    """暂停任务：保留已产出文件与进度，等待用户继续。"""
    if not owned_task(tid, request):
        return fail("FORBIDDEN", "无权操作该任务")
    mgr.pause(tid)
    return {"ok": True, "status": mgr.tasks.get(tid, {}).get("status")}


@app.post("/api/tasks/{tid}/resume")
async def resume_task_api(tid: str, request: Request):
    """继续被暂停的任务：从断点恢复处理，直至全部完成。"""
    if not owned_task(tid, request):
        return fail("FORBIDDEN", "无权操作该任务")
    if tid not in mgr.jobs:
        return {"ok": False, "error": "无可恢复上下文（任务可能已完成或已被删除）"}
    mgr.unpause(tid)
    mgr.renumber(tid)          # 继续也重新排到队尾，遵守先进先出
    # 按任务类型选恢复协程：旧版统一走 _run_job，Live/视频任务上下文结构不同会出错
    kind = (mgr.tasks.get(tid) or {}).get("kind")
    runner = (_run_live_job if kind == "live" else
              _run_video_job if kind == "video" else _run_job)
    asyncio.create_task(runner(tid))
    return {"ok": True, "status": "running"}


@app.post("/api/tasks/{tid}/retry")
async def retry_task_api(tid: str, request: Request):
    """重试失败的任务：沿用创建时的全部素材与配置原样重跑，无需回面板重新设置。

    典型场景：视频合成中 AI 脚本 / 配音等上游调用超时或服务异常导致任务失败。
    重试上下文（素材字节）仅存内存——服务重启或上下文被淘汰（保留最近 8 条）后
    无法原样重试，会返回明确提示让用户重新提交。
    """
    t = owned_task(tid, request)
    if not t:
        return fail("FORBIDDEN", "无权操作该任务")
    if t.get("status") != "failed":
        return {"ok": False, "error": "仅「失败」状态的任务可以重试"}
    job = mgr.jobs.get(tid)
    if not job:
        return {"ok": False,
                "error": "该任务的重试上下文已失效（服务重启过或已被新失败任务挤出），请重新提交任务"}
    kind = t.get("kind")
    mgr.cancelled.discard(tid)
    mgr.unpause(tid)
    mgr.renumber(tid)          # 重试按「新提交」排到队尾，不插队
    if kind == "video":
        # 视频合成是原子操作：整段重跑，清掉上次的产物清单与进度
        job["outputs"] = []
        processed, percent = 0, 0
    else:
        # 图片 / Live：从失败断点继续，已完成的部分不重复处理
        processed = int(job.get("processed", 0) or 0)
        total = int(t.get("total") or 0)
        percent = int(processed / total * 100) if total else 0
    mgr.progress(tid, status="pending", stage="重新排队", log="",
                 processed=processed, percent=percent,
                 outputs=list(job.get("outputs") or []),
                 started_at=None, originality=None)
    runner = (_run_live_job if kind == "live" else
              _run_video_job if kind == "video" else _run_job)
    asyncio.create_task(runner(tid))
    return {"ok": True, "status": "pending", "task_id": tid, "kind": kind}


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str, request: Request):
    """删除单个任务：移除任务记录并彻底删除其产物文件（删除前前端需二次确认）。"""
    t = owned_task(tid, request)
    if not t:
        return fail("FORBIDDEN", "无权操作该任务")
    od = t.get("out_dir")
    deleted = False
    if od and path_allowed(od, cu(request)):
        p = Path(od)
        if p.exists():
            try:
                shutil.rmtree(p, ignore_errors=True)
                deleted = True
            except Exception:
                pass
    mgr.remove(tid)
    return {"ok": True, "removed": True, "deleted_files": deleted, "out_dir": od}


@app.post("/api/tasks/{tid}/open-folder")
def open_folder_api(tid: str, request: Request):
    """点击「查看」：直接在文件管理器中打开该任务的产物目录。"""
    t = owned_task(tid, request)
    if not t:
        return fail("FORBIDDEN", "无权操作该任务")
    od = t.get("out_dir")
    if not od or not Path(od).exists() or not path_allowed(od, cu(request)):
        return {"ok": False, "error": "目录不存在或未生成"}
    ok = open_folder(od)
    return {"ok": ok, "out_dir": od}


@app.post("/api/tasks/delete-all")
def delete_all_files(request: Request):
    """删除当前登录用户生成的全部文件，并清除其关联任务记录。

    旧版用「uid 字符串是否出现在路径里」判断归属，可被伪造 uid 删除他人产物；
    现改为 owner_id 精确匹配 + 路径白名单，且不再接收任何 uid 参数。
    """
    user = cu(request)
    uid = user.get("id") or ""
    if not uid:
        return fail("FORBIDDEN", "请登录后操作")
    bases = list(dict.fromkeys([out_base("image", uid), out_base("video", uid)]))
    targets = []
    for b in bases:
        if b.exists() and path_allowed(b, user) and b not in targets:
            targets.append(b)
    for t in list(mgr.tasks.values()):
        if t.get("owner_id") != uid:
            continue
        p = Path(t.get("out_dir") or "")
        if p.exists() and path_allowed(p, user) and p not in targets:
            targets.append(p)
    deleted_files = 0
    for d in targets:
        for root, dirs, files in os.walk(str(d)):
            deleted_files += len(files)
        shutil.rmtree(str(d), ignore_errors=True)
    removed = 0
    gone = []
    for tid in [k for k, t in mgr.tasks.items() if t.get("owner_id") == uid]:
        mgr.tasks.pop(tid, None)
        mgr.cancelled.discard(tid)
        mgr.paused.discard(tid)
        mgr.jobs.pop(tid, None)
        gone.append(tid)
        removed += 1
    mgr.semaphore.cancel_wait_many(gone)   # 排队中的任务立即出队并让后续补位
    mgr.persist()
    space_svc.mark_dirty(uid)   # 产物已删，用量待重扫
    auth.audit(user, "file.delete_all", f"files={deleted_files},tasks={removed}")
    return {"ok": True, "deleted_files": deleted_files,
            "removed_tasks": removed, "dirs": [str(x) for x in targets]}


@app.post("/api/tasks/clear")
def clear_tasks(request: Request, ids: Optional[list] = Body(default=None)):
    """清除任务记录（保留文件）。ids 为空 = 清空全部，仅管理员可执行。"""
    user = cu(request)
    if ids is None:
        if not policy.allowed(user["role"], "task.clear_all"):
            return fail("FORBIDDEN", "仅管理员可清空全部任务")
        auth.audit(user, "task.clear_all")
        return {"ok": True, "removed": mgr.clear(None)}
    if any(not owned_task(str(tid), request) for tid in ids):
        return fail("FORBIDDEN", "包含无权操作的任务")
    return {"ok": True, "removed": mgr.clear(ids)}


@app.post("/api/tasks/delete")
def delete_tasks(request: Request, ids: Optional[list] = Body(default=None)):
    """批量删除：删除选中任务及其产物目录（单张或多张），删除前前端需确认。"""
    if not ids:
        return {"ok": True, "removed": 0, "deleted_files": 0}
    if any(not owned_task(str(tid), request) for tid in ids):
        return fail("FORBIDDEN", "包含无权操作的任务")
    user = cu(request)
    removed = 0
    deleted_files = 0
    for tid in ids:
        t = mgr.tasks.get(str(tid))
        od = (t or {}).get("out_dir")
        if od and path_allowed(od, user):
            p = Path(od)
            if p.exists():
                for root, dirs, files in os.walk(str(p)):
                    deleted_files += len(files)
                shutil.rmtree(str(p), ignore_errors=True)
        if str(tid) in mgr.tasks:
            mgr.remove(str(tid))
            removed += 1
    mgr.persist()
    space_svc.mark_dirty(user.get("id") or "")   # 产物已删，用量待重扫
    auth.audit(user, "file.delete_batch", f"count={removed}")
    return {"ok": True, "removed": removed, "deleted_files": deleted_files}


# ---------------- 字体 / 文字模板 ----------------


def _tpl_meta(request: Request, payload: dict) -> dict:
    """模板归属元信息：owner 只取服务端会话里的不可变 id，绝不信任 payload。"""
    u = cu(request)
    # shared 未提供时不改变原值（服务层据此保留既有共享状态）；新建则默认私有。
    return {"owner": u.get("id", ""), "owner_name": u.get("username", ""),
            "shared": (payload or {}).get("shared")}


@app.get("/api/fonts")
def list_fonts(request: Request, refresh: bool = False):
    """列出应用自带字体（仅 backend/fonts/，不加载系统字体）。

    refresh=1 时强制重新扫描目录；正常情况下目录内容变化也会自动感知，无需重启服务。
    游客只读名称，不返回服务端绝对路径。
    """
    if refresh:
        font_svc.refresh()
    items = font_svc.list_fonts()
    if policy.is_guest(cu(request)):
        items = [{k: v for k, v in x.items() if k != "path"} for x in items]
    return items


@app.get("/api/text-templates")
def list_text_templates(request: Request):
    """仅返回：自己创建的 + 别人共享的（只读）+ 管理员全部 + 存量公共模板。"""
    u = cu(request)
    return [policy.template_public(t, u) for t in tpl_svc.list_templates()
            if policy.template_visible(t, u)]


@app.post("/api/text-templates")
async def create_text_template(request: Request, payload: dict = Body(default={})):
    """新建/更新文字模板：编辑他人或未共享模板一律拒绝，归属由服务端写入。"""
    u = cu(request)
    payload = payload or {}
    tid = payload.get("id")
    if tid and not basename_ok(str(tid)):
        return fail("BAD_REQUEST", "模板 ID 不合法")
    existing = tpl_svc.get_template(str(tid)) if tid else None
    if existing and not policy.template_can_edit(existing, u):
        return fail("FORBIDDEN", "只读模板：仅创建者或管理员可修改")
    rec = tpl_svc.save_template(payload, _tpl_meta(request, payload))
    return {"ok": True, "template": policy.template_public(rec, u)}


@app.get("/api/text-templates/{tid}")
def get_text_template(tid: str, request: Request):
    if not basename_ok(tid):
        return fail("BAD_REQUEST", "模板 ID 不合法")
    t = tpl_svc.get_template(tid)
    if not t:
        return {"error": "not found"}
    u = cu(request)
    if not policy.template_visible(t, u):
        return fail("FORBIDDEN", "无权查看该模板")
    return policy.template_public(t, u)


@app.delete("/api/text-templates/{tid}")
def delete_text_template(tid: str, request: Request):
    if not basename_ok(tid):
        return fail("BAD_REQUEST", "模板 ID 不合法")
    u = cu(request)
    t = tpl_svc.get_template(tid)
    if not t:
        return {"ok": False}
    if not policy.template_can_edit(t, u):
        return fail("FORBIDDEN", "只读模板：仅创建者或管理员可删除")
    return {"ok": tpl_svc.delete_template(tid)}


@app.post("/api/text-templates/preview")
async def preview_text_template(
    style: str = Form("{}"),
    spec: str = Form("9:16"),
    text: str = Form("光影工坊 · 图片加文字"),
    bg: UploadFile = File(None),
):
    """预览模板效果：把样式渲染到上传底图（或按规格生成渐变底图）。
    预览底图仅用于本次预览，不会被保存到模板。"""
    import json
    try:
        st = json.loads(style) or {}
    except Exception:
        st = {}
    st.setdefault("spec", spec)
    from .services.templates import normalize_style
    raw = st.get("style") if isinstance(st.get("style"), dict) else st
    style_obj = normalize_style(raw)
    # 底图：上传优先，否则按规格生成占位渐变图
    if bg is not None and bg.filename:
        data = await bg.read()
        try:
            base_img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            base_img = None
    else:
        base_img = None
    if base_img is None:
        w, h = (1080, 1920) if style_obj["spec"] == "9:16" else (1920, 1080)
        base_img = _make_placeholder(w, h)
    out = image_svc.render_text_on_image(base_img, style_obj, text or "光影工坊")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


def _make_placeholder(w: int, h: int):
    """生成预览用渐变底图（带参考网格，方便观察文字位置）。"""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(38 + t * 90); g = int(52 + t * 70); b = int(86 + t * 60)
        for x in range(w):
            px[x, y] = (r, g, b)
    d = ImageDraw.Draw(img)
    step = w // 6
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(255, 255, 255, 40), width=1)
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(255, 255, 255, 40), width=1)
    return img


# ------------------------- 视频模板（字幕 / 顶部标题样式） -------------------------

@app.get("/api/video-templates")
def list_video_templates(request: Request):
    """与文字模板一致：自己创建的 + 共享只读 + 管理员全部 + 存量公共模板。"""
    u = cu(request)
    return [policy.template_public(t, u) for t in vtpl_svc.list_templates()
            if policy.template_visible(t, u)]


@app.post("/api/video-templates/preview")
async def preview_video_template(
    style: str = Form("{}"),
    spec: str = Form("9:16"),
    subtitle_text: str = Form("这里显示字幕内容"),
    title_text: str = Form("视频顶部标题"),
    bg: UploadFile = File(None),
):
    """预览视频模板：把顶部标题 + 底部字幕同时渲染到底图（占位渐变或上传底图）。

    预览底图只用于本次预览，不会随模板保存（与图片加文字模板一致）。
    """
    try:
        raw = json.loads(style) or {}
    except Exception:
        raw = {}
    raw = dict(raw) if isinstance(raw, dict) else {}
    raw.setdefault("spec", spec)
    tpl = vtpl_svc.normalize_video_template(raw)
    w, h = (540, 960) if tpl["spec"] == "9:16" else (960, 540)
    base = _make_placeholder(w, h).convert("RGBA")
    if bg is not None and bg.filename:
        try:
            user_bg = Image.open(io.BytesIO(await bg.read())).convert("RGBA").resize((w, h))
            base = user_bg
        except Exception:
            pass
    if tpl.get("title_enabled", True) and (title_text or "").strip():
        base = Image.alpha_composite(
            base, image_svc.render_text_layer(w, h, tpl["title"], title_text))
    if tpl.get("subtitle_enabled", True) and (subtitle_text or "").strip():
        base = Image.alpha_composite(
            base, image_svc.render_text_layer(w, h, tpl["subtitle"], subtitle_text))
    buf = io.BytesIO()
    base.convert("RGB").save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.post("/api/video-templates")
async def create_video_template(request: Request, payload: dict = Body(default={})):
    u = cu(request)
    payload = payload or {}
    tid = payload.get("id")
    if tid and not basename_ok(str(tid)):
        return fail("BAD_REQUEST", "模板 ID 不合法")
    existing = vtpl_svc.get_template(str(tid)) if tid else None
    if existing and not policy.template_can_edit(existing, u):
        return fail("FORBIDDEN", "只读模板：仅创建者或管理员可修改")
    rec = vtpl_svc.save_template(payload, _tpl_meta(request, payload))
    return {"ok": True, "template": policy.template_public(rec, u)}


@app.get("/api/video-templates/{tid}")
def get_video_template(tid: str, request: Request):
    if not basename_ok(tid):
        return fail("BAD_REQUEST", "模板 ID 不合法")
    t = vtpl_svc.get_template(tid)
    if not t:
        return {"error": "not found"}
    u = cu(request)
    if not policy.template_visible(t, u):
        return fail("FORBIDDEN", "无权查看该模板")
    return policy.template_public(t, u)


@app.delete("/api/video-templates/{tid}")
def delete_video_template(tid: str, request: Request):
    if not basename_ok(tid):
        return fail("BAD_REQUEST", "模板 ID 不合法")
    u = cu(request)
    t = vtpl_svc.get_template(tid)
    if not t:
        return {"ok": False}
    if not policy.template_can_edit(t, u):
        return fail("FORBIDDEN", "只读模板：仅创建者或管理员可删除")
    return {"ok": vtpl_svc.delete_template(tid)}


# ------------------------- AI 提示词模板（按场景 + 角色特点，{} 可替换块） -------------------------

@app.get("/api/prompt-templates")
def list_prompt_templates(request: Request):
    """与文字 / 视频模板一致：自己创建的 + 共享只读 + 管理员全部 + 存量公共模板。"""
    u = cu(request)
    return [policy.template_public(t, u) for t in ptpl_svc.list_templates()
            if policy.template_visible(t, u)]


@app.post("/api/prompt-templates")
async def create_prompt_template(request: Request, payload: dict = Body(default={})):
    """新建/更新 AI 提示词模板：编辑他人或未共享模板一律拒绝，归属由服务端写入。"""
    u = cu(request)
    payload = payload or {}
    tid = payload.get("id")
    if tid and not basename_ok(str(tid)):
        return fail("BAD_REQUEST", "模板 ID 不合法")
    existing = ptpl_svc.get_template(str(tid)) if tid else None
    if existing and not policy.template_can_edit(existing, u):
        return fail("FORBIDDEN", "只读模板：仅创建者或管理员可修改")
    rec = ptpl_svc.save_template(payload, _tpl_meta(request, payload))
    return {"ok": True, "template": policy.template_public(rec, u)}


@app.get("/api/prompt-templates/{tid}")
def get_prompt_template(tid: str, request: Request):
    if not basename_ok(tid):
        return fail("BAD_REQUEST", "模板 ID 不合法")
    t = ptpl_svc.get_template(tid)
    if not t:
        return {"error": "not found"}
    u = cu(request)
    if not policy.template_visible(t, u):
        return fail("FORBIDDEN", "无权查看该模板")
    return policy.template_public(t, u)


@app.delete("/api/prompt-templates/{tid}")
def delete_prompt_template(tid: str, request: Request):
    if not basename_ok(tid):
        return fail("BAD_REQUEST", "模板 ID 不合法")
    u = cu(request)
    t = ptpl_svc.get_template(tid)
    if not t:
        return {"ok": False}
    if not policy.template_can_edit(t, u):
        return fail("FORBIDDEN", "只读模板：仅创建者或管理员可删除")
    return {"ok": ptpl_svc.delete_template(tid)}


@app.post("/api/prompt-templates/render")
async def render_prompt_template(request: Request, payload: dict = Body(default={})):
    """试算「最终给 AI 的提示词」：把正文里的 {} 块替换为填充值（value 优先、缺省用 default）。

    仅做文本替换，不调用任何模型；供前端右侧实时预览与测试使用。
    传 body 时=编辑器实时预览（未填块保留 {} 提示）；传 id 时=按模板中心已存模板渲染，
    且先做可见性校验（自己创建的 / 别人共享的 / 管理员全部），与列表读取权限一致。
    """
    payload = payload or {}
    tid = (payload.get("id") or "").strip()
    if tid:
        if not basename_ok(tid):
            return fail("BAD_REQUEST", "模板 ID 不合法")
        rec = ptpl_svc.get_template(tid)
        if not rec:
            return fail("BAD_REQUEST", "模板不存在或已删除")
        if not policy.template_visible(rec, cu(request)):
            return fail("FORBIDDEN", "无权使用该模板")
        return ptpl_svc.render_by_id(tid, payload.get("values") or {},
                                    drop_unfilled=payload.get("drop_unfilled", True))
    rec = ptpl_svc.normalize(payload)
    return {"ok": True, "prompt": ptpl_svc.build_prompt(rec)}


@app.get("/api/template-center/all")
def template_center_all(request: Request):
    """模板中心 = 全应用模板的唯一数据源。

    一次返回三类模板（各自按权限过滤）+ 场景字典，供所有工作场景「自动加载」：
    图片加文字 / 封面模板、视频字幕标题模板、所有 AI 提示词入口。
    无论在模板中心还是工作区新建/修改，落盘都是同一批文件，此处始终读到最新。
    """
    u = cu(request)

    def visible(items):
        return [policy.template_public(t, u) for t in items
                if policy.template_visible(t, u)]

    return {
        "ok": True,
        "image": visible(tpl_svc.list_templates()),
        "video": visible(vtpl_svc.list_templates()),
        "prompt": visible(ptpl_svc.list_templates()),
        "scopes": [{"key": k, "label": ptpl_svc.SCOPE_LABELS[k]}
                   for k in ptpl_svc.SCOPES],
    }


def _write_video_materials(out_dir: str, scripts, covers, voices):
    """视频任务完成后，在产物目录写入 materials/ 子目录：封面图 + 配音 + 脚本文案.txt。

    便于用户下载/保存到本地时一并拿到成品的配套素材；该目录不计入 outputs，
    因此不影响成品预览。封面/配音与脚本按序号一一对应。
    """
    try:
        scripts = list(scripts or [])
        covers = list(covers or [])
        voices = list(voices or [])
        if not (scripts or covers or voices):
            return
        mdir = Path(out_dir) / "materials"
        mdir.mkdir(parents=True, exist_ok=True)
        AUDIO_EXTS = {"mp3", "wav", "m4a", "aac", "flac", "ogg", "mp4", "webm"}
        for i, item in enumerate(covers):
            name, data = item[0], item[1]
            ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "jpg")
            (mdir / f"封面_{i + 1:02d}.{ext}").write_bytes(data)
        for i, item in enumerate(voices):
            name, data = item[0], item[1]
            ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "mp3")
            if ext not in AUDIO_EXTS:
                ext = "mp3"
            (mdir / f"配音_{i + 1:02d}.{ext}").write_bytes(data)
        lines = []
        for i, s in enumerate(scripts):
            sub = str((s or {}).get("subtitle") or "").strip()
            title = str((s or {}).get("title") or "").strip()
            copy = str((s or {}).get("cover_copy") or "").strip()
            if not (sub or title or copy):
                continue
            lines.append(f"【第 {i + 1} 条】")
            lines.append(f"脚本：{sub}")
            if title:
                lines.append(f"标题：{title}")
            if copy:
                lines.append(f"封面文案：{copy}")
            lines.append("")
        if lines:
            (mdir / "脚本文案.txt").write_text(
                "\n".join(lines), encoding="utf-8")
    except Exception:
        pass


@app.post("/api/tasks/zip")
def zip_tasks(request: Request, ids: Optional[list] = Body(default=None)):
    """打包下载任务产物。ids 为 None 时打包当前可见的全部任务；否则打包指定任务。
    压缩包内按各任务输出目录名归并，文件名与该目录名对齐。"""
    user = cu(request)
    tasks = visible_tasks(user)
    if ids is not None:
        want = set(str(x) for x in ids)
        tasks = [t for t in tasks if t["id"] in want]
    tasks = [t for t in tasks if (t.get("outputs"))
             and path_allowed(t.get("out_dir") or "", user)]
    if not tasks:
        return {"error": "没有可打包的文件"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in tasks:
            od = t.get("out_dir") or ""
            dname = Path(od).name or t["id"]
            for name in t.get("outputs", []):
                fp = Path(od) / name
                if fp.exists():
                    z.write(fp, arcname=f"{dname}/{name}")
            # 附带 materials 子目录（封面图 / 配音 / 脚本文案），与成品视频一并打包
            mdir = Path(od) / "materials"
            if mdir.is_dir():
                for f in sorted(mdir.iterdir()):
                    if f.is_file():
                        z.write(f, arcname=f"{dname}/素材配套/{f.name}")
    buf.seek(0)
    if len(tasks) == 1:
        fname = f"{Path(tasks[0].get('out_dir','task')).name}.zip"
        legacy = fname
    else:
        d = datetime.now().strftime('%Y%m%d')
        fname = f"光影工坊产物_{d}.zip"
        legacy = f"outputs_{d}.zip"
    # 仅「在线用户」的打包下载计入次数；本机用户点「打开目录」不算下载
    if not is_local_client(request):
        mgr.bump_download([t["id"] for t in tasks])
    # legacy filename 必须为 ASCII；中文名走 filename*（百分号编码）
    disp = f"attachment; filename=\"{legacy}\"; filename*=UTF-8''{quote(fname)}"
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": disp})


@app.post("/api/tasks/image")
async def create_image_task(request: Request,
                            files: Optional[list[UploadFile]] = File(default=None),
                            out_dir: str = Form(""),
                            uid: str = Form(""),   # 兼容旧前端，完全忽略
                            config: str = Form(""),
                            server_paths: str = Form("")):
    user = cu(request)
    uid = user["id"]                # 身份只取服务端会话的不可变 id
    # 在线用户（非服务器本机访问）：禁止临时上传本地素材，必须走「从空间添加」；
    # 服务器本机用户（含管理员本机登录）可直接上传本地文件
    if not is_local_client(request) and files:
        raise HTTPException(status_code=400,
                            detail="在线用户请先把素材上传到「我的空间」，再用「从空间文件/目录添加」取用")
    items = []
    for f in (files or []):
        data = await f.read()
        items.append((f.filename, data))
    # 空间来源素材（可含目录，递归展开，仅取图片/视频）
    items += _space_files(uid, server_paths, ["image", "video"])
    if not items:
        raise HTTPException(status_code=400, detail="请先添加图片素材（本地或来自我的空间）")
    tid = uuid.uuid4().hex[:8]
    cfg = load_config(uid)
    # 前端提交时附带当前 UI 勾选的图像处理配置（图像工具开关/参数），
    # 优先级高于已保存的用户配置，确保「界面勾选即生效」，不必先点保存设置。
    if config:
        try:
            sub = json.loads(config)
            if isinstance(sub, dict):
                cfg = dict(cfg)
                cfg["image"] = sub
        except Exception:
            pass
    # 解析「图片加文字」所选模板：把模板样式固化进任务配置（与用户文案一起），便于暂停/恢复
    _resolve_text_template(cfg)
    # 产物目录：<基础目录>/<不可变用户id>/<时间戳_任务ID>/
    base = resolve_base("image", user, out_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = base / uid / f"{ts}_{tid}"
    target.mkdir(parents=True, exist_ok=True)
    mgr.register(tid, kind="image", total=len(items),
                 out_dir=str(target), owner_id=uid)
    mgr.tasks[tid]["label"] = build_op_label(cfg)
    asyncio.create_task(run_image_task(tid, items, cfg, str(target)))
    return {"task_id": tid, "total": len(items), "out_dir": str(target)}


@app.post("/api/tasks/video")
async def create_video_task(request: Request,
                            files: Optional[list[UploadFile]] = File(default=None),
                            covers: Optional[list[UploadFile]] = File(default=None),
                            voices: Optional[list[UploadFile]] = File(default=None),
                            bgm: Optional[list[UploadFile]] = File(default=None),
                            out_dir: str = Form(""),
                            uid: str = Form(""),   # 兼容旧前端，完全忽略
                            config: str = Form(""),
                            server_paths: str = Form(""),
                            server_covers: str = Form(""),
                            server_voices: str = Form(""),
                            server_bgm: str = Form("")):
    user = cu(request)
    uid = user["id"]
    """视频批加工：把图片/视频素材合成为指定画幅、分辨率、时长、带 xfade 转场的成品视频。

    files: 视频素材（图片/视频混合）；covers: 现成封面图；
    voices: 配音音频（与 config.video.scripts 按顺序一一对应）；bgm: 背景乐池（每条视频一首）。
    server_paths/server_covers/server_voices/server_bgm: 来自「我的空间」的素材 rel（支持目录递归展开）。
    config 携带前端「视频加工」面板配置（优先级高于已保存配置，界面即生效）。
    原创度校验在 video.process_video 内完成（分母按时长、跨任务复用 K=2，<50% 拦截）。
    """
    async def _read_all(uploads):
        out = []
        for f in (uploads or []):
            if not f.filename:
                continue
            out.append((f.filename, await f.read()))
        return out

    # 在线用户（非服务器本机访问）：禁止临时上传本地素材，必须走「从空间添加」；
    # 服务器本机用户（含管理员本机登录）可直接上传本地文件
    if not is_local_client(request) and (files or covers or voices or bgm):
        raise HTTPException(status_code=400,
                            detail="在线用户请先把素材上传到「我的空间」，再用「从空间文件/目录添加」取用")
    items = await _read_all(files)
    cover_items = await _read_all(covers)
    voice_items = await _read_all(voices)
    bgm_items = await _read_all(bgm)
    tid = uuid.uuid4().hex[:8]
    cfg = load_config(uid)
    if config:
        try:
            sub = json.loads(config)
            if isinstance(sub, dict):
                cfg = dict(cfg)
                if "video" in sub:
                    cfg["video"] = sub["video"]
        except Exception:
            pass
    vcfg = dict(cfg.get("video") or {})
    script_items = [x for x in (vcfg.get("scripts") or []) if isinstance(x, dict)]
    # 空间来源素材（含目录递归展开）：与本地上传合并
    items += _space_files(uid, server_paths, ["image", "video"])
    cover_items += _space_files(uid, server_covers, ["image"])
    bgm_items += _space_files(uid, server_bgm, ["file", "audio"])
    # 与图片 / Live 任务保持一致：空素材在 API 层直接拒绝，
    # 不创建任务、不占用全局 FIFO 信号量，避免任务列表里出现「没有素材」的失败记录。
    if not items:
        raise HTTPException(status_code=400,
                            detail="请先添加图片或视频素材（本地或来自我的空间）")
    sp_voices = _space_voices(uid, server_voices)
    if sp_voices is not None:
        if len(sp_voices) != len(script_items) or any(v is None for v in sp_voices):
            raise HTTPException(status_code=400,
                                detail="空间配音需与脚本一一对应：每条脚本都要指定一个空间音频，且数量与脚本一致")
        voice_items = sp_voices
    # 配音与字幕必须一一对应：在 API 层直接拒绝，不创建任务、不占用信号量
    ok, msg, _ = video_svc.check_script_pairs(script_items, voice_items)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    # 把所选视频模板的样式固化进任务配置（与图片加文字模板一致，便于任务持久化/复现）
    _resolve_video_template(cfg)
    vcfg = dict(cfg.get("video") or {})
    vcfg["cover_count"] = len(cover_items)
    vcfg["script_count"] = len(script_items)
    vcfg["bgm_count"] = len(bgm_items)
    cfg["video"] = vcfg
    # 视频产物落入「视频输出目录」（可在全局设置自定义；不沿用图片的 output_dir）
    base = resolve_base("video", user, out_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = base / uid / f"{ts}_{tid}"
    target.mkdir(parents=True, exist_ok=True)
    mgr.register(tid, kind="video", total=len(items),
                 out_dir=str(target), owner_id=uid)
    mgr.tasks[tid]["label"] = build_video_op_label(cfg, len(items))
    asyncio.create_task(run_video_task(tid, items, cfg, str(target),
                                       cover_items, script_items,
                                       voice_items, bgm_items, user=user))
    return {"task_id": tid, "total": len(items),
            "covers": len(cover_items), "scripts": len(script_items),
            "voices": len(voice_items), "bgm": len(bgm_items),
            "out_dir": str(target)}


def build_video_op_label(cfg, n_items: int = 0):
    """视频任务精简标签，例如「视频-9:16-30s-无配音-fade」「视频-16:9-25s-配音2套-随机3种」。

    结构：视频-规格-时长-配音情况-（转场/条数等附加项）。
    """
    import math
    v = (cfg or {}).get("video", {}) or {}
    parts = ["视频"]

    # 规格：画幅；非 1080P 时附加分辨率
    spec = str(v.get("aspect") or "")
    res = str(v.get("resolution") or "")
    if res and res.lower() != "1080p":
        spec = f"{spec}·{res.upper() if res == '4k' else res}"
    parts.append(spec or "合成")

    # 时长：按每条成品视频估算总时长（含片头封面秒数）
    try:
        clip = float(v.get("clip_duration", 5))
        t_dur = float(v.get("transition_duration", 0.5))
        cover_dur = float(v.get("cover_duration", 1.0)) if int(v.get("cover_count", 0) or 0) else 0.0
        if v.get("duration_strategy", "fixed") == "fixed":
            if n_items > 0:
                vc = max(1, int(v.get("video_count", 1) or 1))
                m = max(1, math.ceil(n_items / vc))
                total = m * clip - (m - 1) * t_dur + cover_dur
            else:
                total = clip
        else:
            total = float(v.get("target_duration", 30)) + cover_dur
        total = round(total)
        parts.append(f"{total}s")
    except Exception:
        parts.append("合成")

    # 配音情况
    ns = int(v.get("script_count", 0) or 0)
    parts.append("配音%d套" % ns if ns else "无配音")

    # 附加：转场（单一用名称，多个用随机N种）；多条视频时追加条数
    trs = v.get("transitions") or []
    if isinstance(trs, str):
        trs = [trs]
    if len(trs) > 1:
        parts.append(f"随机{len(trs)}种")
    else:
        parts.append(str(trs[0] if trs else v.get("transition", "fade")))
    vc = int(v.get("video_count", 1) or 1)
    if vc > 1:
        parts.append(f"x{vc}条")

    return "-".join(parts)


def _resolve_text_template(cfg):
    """若文字功能启用且指定了 template_id，则把模板样式固化进配置。"""
    img = (cfg or {}).get("image", {}) or {}
    tc = img.get("text", {}) or {}
    if not tc.get("enabled"):
        return
    tid = tc.get("template_id")
    if not tid:
        return
    t = tpl_svc.get_template(tid)
    if not t:
        return
    style = dict(t.get("style", {}))
    # 用户当前界面的微调（颜色/内容等）以 UI 提交为准，覆盖模板默认
    for k in ("color", "opacity", "anchor"):
        if tc.get(k) is not None:
            style[k] = tc[k]
    tc["style"] = style
    tc["template_name"] = t.get("name", tid)


def _resolve_video_template(cfg):
    """把选中的视频模板（字幕 / 顶部标题样式）固化进任务配置；未选择时用内置默认模板。"""
    v = dict((cfg or {}).get("video") or {})
    tid = v.get("template_id")
    t = vtpl_svc.get_template(tid) if tid else None
    if t:
        v["template"] = t
        v["template_name"] = t.get("name", tid)
    else:
        v["template"] = vtpl_svc.default_template()
        v["template_name"] = "内置默认模板"
    cfg["video"] = v


def _split_text_lines(content: str) -> list:
    """把多行用户文案按行拆分、去空、去重（保留首次出现顺序）。

    用于「图片加文字」多行循环：每行对应一张图，行数不足时自动循环；
    重复的文案去重后只保留一条，避免不必要地重复同一句文案。
    """
    seen = set()
    out = []
    for raw in (content or "").split("\n"):
        s = (raw or "").strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def build_op_label(cfg):
    """根据图像配置生成任务的操作标签，例如「裁切·缩放·去水印」。"""
    img = (cfg or {}).get("image", {}) or {}
    names = []
    conv = img.get("convert", {}) or {}
    if conv.get("enabled"):
        fmt = (conv.get("format") or "").upper()
        names.append(("转" + fmt) if fmt else "格式转换")
    if (img.get("crop", {}) or {}).get("enabled"):
        names.append("裁切")
    if (img.get("watermark", {}) or {}).get("enabled"):
        names.append("去水印")
    if (img.get("resize", {}) or {}).get("enabled"):
        names.append("缩放")
    if (img.get("text", {}) or {}).get("enabled"):
        names.append(img["text"].get("template_name") or "加文字")
    if not names:
        names.append("原图输出")
    return "·".join(names)


async def run_image_task(tid, items, cfg, out_dir):
    """启动任务：保存运行上下文（含原始字节）到 mgr.jobs，便于暂停后继续。"""
    mgr.jobs[tid] = {"items": list(items), "cfg": cfg, "out_dir": out_dir,
                     "processed": 0, "outputs": []}
    await _run_job(tid)


def _prune_failed_jobs(limit: int = 8):
    """失败任务的重试上下文（含原始素材字节）只存内存，限制保留条数防止无限增长。"""
    ids = [tid for tid, t in mgr.tasks.items()
           if t.get("status") == "failed" and tid in mgr.jobs]
    for tid in ids[:max(0, len(ids) - limit)]:
        mgr.jobs.pop(tid, None)


async def _run_job(tid):
    """占用信号量执行任务循环；完成后（或暂停/取消/删除）释放并清理上下文。

    失败的上下文保留在内存：供「重试」直接沿用原素材与配置重新执行。
    """
    t0 = mgr.tasks.get(tid) or {}
    granted = await mgr.semaphore.acquire(
        tid=tid, seq=t0.get("seq"),
        on_wait=lambda: mgr.progress(tid, stage="排队中"))
    if not granted:
        return   # 排队期间被取消 / 删除：未占用名额，直接退出
    try:
        if mgr.is_cancelled(tid) or tid not in mgr.tasks:
            return
        try:
            await _process_loop(tid)
        except Exception as e:
            # 处理循环级异常（如素材损坏 / 执行器崩溃）：标记失败并保留上下文供重试
            mgr.progress(tid, status="failed", stage="处理失败", log=str(e))
    finally:
        await mgr.semaphore.release()
        t = mgr.tasks.get(tid)
        if not t or t.get("status") in ("done", "cancelled") or tid not in mgr.tasks:
            mgr.jobs.pop(tid, None)
        _prune_failed_jobs()


async def _process_loop(tid):
    """处理循环：逐项处理；遇暂停/取消/删除则安全退出并保留进度。"""
    job = mgr.jobs.get(tid)
    if not job:
        return
    items = job["items"]
    cfg = job["cfg"]
    out_dir = job["out_dir"]
    total = len(items)
    # 多行文案：拆行 + 去重，逐图循环使用（不改动原始 cfg，保证暂停/恢复正确）
    _tc = (cfg.get("image", {}) or {}).get("text", {}) or {}
    _text_on = bool(_tc.get("enabled"))
    _lines = _split_text_lines(_tc.get("content", "")) if _text_on else []
    mgr.progress(tid, status="running", stage="处理中")
    while job["processed"] < total:
        if mgr.is_cancelled(tid):
            mgr.progress(tid, status="cancelled", stage="已取消",
                         outputs=list(job["outputs"]))
            return
        if mgr.is_paused(tid):
            mgr.progress(tid, status="paused", stage="已暂停",
                         outputs=list(job["outputs"]))
            return
        if tid not in mgr.tasks:
            return
        name, data = items[job["processed"]]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        # 视频部分（含 Live 图的 .mov）在图片任务中跳过
        if ext in ("mov", "mp4", "webm", "mkv"):
            job["processed"] += 1
            mgr.progress(tid, processed=job["processed"],
                         percent=int(job["processed"] / total * 100),
                         stage=f"跳过视频 {name}",
                         outputs=list(job["outputs"]))
            continue
        try:
            _per = None
            if _text_on:
                # 按当前图序号循环取文案（行数不足自动循环；_lines 已去重）
                _per = _lines[job["processed"] % len(_lines)] if _lines else ""
            out = await asyncio.get_event_loop().run_in_executor(
                None, lambda p=_per: image_svc.process_image(data, name, cfg, out_dir, text_override=p)
            )
            if out:
                job["outputs"].append(os.path.basename(out))
        except Exception as e:
            mgr.progress(tid, log=f"{name} 失败: {e}")
        job["processed"] += 1
        if tid not in mgr.tasks:
            return
        mgr.progress(tid, processed=job["processed"],
                     percent=int(job["processed"] / total * 100),
                     status="running", stage=f"处理 {name}",
                     outputs=list(job["outputs"]))
    mgr.progress(tid, status="done", percent=100, stage="完成",
                 outputs=list(job["outputs"]))


async def run_video_task(tid, items, cfg, out_dir, covers=None,
                         scripts=None, voices=None, bgm=None, user=None):
    """启动视频任务：保存运行上下文到 mgr.jobs（视频合成原子操作，不支持中途暂停续跑）。"""
    mgr.jobs[tid] = {"items": list(items), "cfg": cfg, "out_dir": out_dir,
                     "outputs": [], "covers": list(covers or []),
                     "scripts": list(scripts or []), "voices": list(voices or []),
                     "bgm": list(bgm or []), "user": user}
    await _run_video_job(tid)


async def _run_video_job(tid):
    """占用信号量执行视频合成；完成后释放并清理上下文。"""
    t0 = mgr.tasks.get(tid) or {}
    granted = await mgr.semaphore.acquire(
        tid=tid, seq=t0.get("seq"),
        on_wait=lambda: mgr.progress(tid, stage="排队中"))
    if not granted:
        return   # 排队期间被取消 / 删除：未占用名额，直接退出
    try:
        if mgr.is_cancelled(tid):
            mgr.progress(tid, status="cancelled", stage="已取消")
            return
        job = mgr.jobs.get(tid)
        if not job:
            return
        mgr.progress(tid, status="running", stage="处理中")

        def _cb(**kw):
            # 转发进度回调：stage 文案 + percent 百分比（percent 供前端倒计时估算剩余时间）
            mgr.progress(tid, **{k: v for k, v in kw.items() if k in ("stage", "percent")})

        out = await asyncio.get_event_loop().run_in_executor(
            None, lambda: video_svc.process_video(
                job["items"], job["cfg"], job["out_dir"], _cb,
                covers=job.get("covers"), scripts=job.get("scripts"),
                voices=job.get("voices"), bgm=job.get("bgm"),
                user=job.get("user"))
        )
        if mgr.is_cancelled(tid):
            mgr.progress(tid, status="cancelled", stage="已取消")
            return
        if out.get("ok"):
            # 合成成功：在产物目录写入「素材配套」（封面图 / 配音 / 脚本文案.txt），
            # 与成品视频一起供本机打开目录或局域网打包下载时一并获取
            _write_video_materials(job["out_dir"], job.get("scripts"),
                                   job.get("covers"), job.get("voices"))
            mgr.progress(tid, status="done", percent=100, stage="完成",
                         outputs=list(out.get("outputs", [])),
                         meta=out.get("video_meta"),
                         originality=out.get("originality"))
        else:
            # 原创度不达标 / 处理异常：停止执行并给出原因
            mgr.progress(tid, status="failed", stage=out.get("error", "处理失败")[:60],
                         log=out.get("error", ""), originality=out.get("originality"))
    except Exception as e:
        mgr.progress(tid, status="failed", stage="处理失败", log=str(e))
    finally:
        await mgr.semaphore.release()
        t = mgr.tasks.get(tid)
        # 失败任务保留上下文（items/cfg/covers/scripts/voices/bgm/user）供「重试」原样重跑
        if not t or t.get("status") in ("done", "cancelled") or tid not in mgr.tasks:
            mgr.jobs.pop(tid, None)
        _prune_failed_jobs()


@app.post("/api/tasks/live")
async def create_live_task(request: Request,
                           files: Optional[list[UploadFile]] = File(default=None),
                           out_dir: str = Form(""),
                           uid: str = Form(""),   # 兼容旧前端，完全忽略
                           config: str = Form(""),
                           server_paths: str = Form("")):
    user = cu(request)
    uid = user["id"]
    """Live 图提取：从动态照片 / iPhone 配对（图+同名mov）/ 视频文件中提取静态帧，
    转为 png / jpg / bmp（单图默认 ≤3MB 可调）。

    files: Live 图相关文件（图片 / 视频均可，按文件名主干自动配对）。
    server_paths: 来自「我的空间」的素材 rel（支持目录递归展开）。
    产物落入「图片输出目录」（与图片批处理共用，可在全局设置自定义）。
    config 携带前端「Live图提取」面板配置（优先级高于已保存配置）。
    """
    # 在线用户（非服务器本机访问）：禁止临时上传本地素材，必须走「从空间添加」；
    # 服务器本机用户（含管理员本机登录）可直接上传本地文件
    if not is_local_client(request) and files:
        raise HTTPException(status_code=400,
                            detail="在线用户请先把素材上传到「我的空间」，再用「从空间文件/目录添加」取用")
    items = []
    for f in (files or []):
        data = await f.read()
        items.append((f.filename or "live.bin", data))
    # 空间来源素材（可含目录，递归展开，仅取图片/视频）
    items += _space_files(uid, server_paths, ["image", "video"])
    if not items:
        raise HTTPException(status_code=400, detail="请先添加 Live 图文件（本地或来自我的空间）")
    tid = uuid.uuid4().hex[:8]
    cfg = load_config(uid)
    if config:
        try:
            sub = json.loads(config)
            if isinstance(sub, dict):
                cfg = dict(cfg)
                if "live" in sub:
                    cfg["live"] = sub["live"]
        except Exception:
            pass
    # 同名配对分组：图片+视频 → 一个单元；无法识别的扩展名记录为忽略
    units, ignored = live_svc.plan_units(items)
    if not units:
        raise HTTPException(status_code=400, detail="未找到可提取的 Live 图文件（需图片或 mov/mp4 视频文件）")
    # 产物目录：<图片输出目录>/<不可变用户id>/<时间戳_任务ID>/
    base = resolve_base("image", user, out_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = base / uid / f"{ts}_{tid}"
    target.mkdir(parents=True, exist_ok=True)
    mgr.register(tid, kind="live", total=len(units),
                 out_dir=str(target), owner_id=uid)
    mgr.tasks[tid]["label"] = live_svc.build_live_label(cfg, len(units), len(ignored))
    asyncio.create_task(run_live_task(tid, units, ignored, cfg, str(target)))
    return {"task_id": tid, "total": len(units), "ignored": len(ignored),
            "out_dir": str(target)}


async def run_live_task(tid, units, ignored, cfg, out_dir):
    """启动 Live 提取任务：保存运行上下文到 mgr.jobs（支持暂停/继续，同图片任务）。"""
    mgr.jobs[tid] = {"units": list(units), "ignored": list(ignored), "cfg": cfg,
                     "out_dir": out_dir, "processed": 0, "outputs": []}
    await _run_live_job(tid)


async def _run_live_job(tid):
    """占用信号量执行 Live 提取循环；完成后（或暂停/取消/删除）释放并清理上下文。

    失败的上下文保留在内存：供「重试」从断点继续（已完成的部分不重复处理）。
    """
    t0 = mgr.tasks.get(tid) or {}
    granted = await mgr.semaphore.acquire(
        tid=tid, seq=t0.get("seq"),
        on_wait=lambda: mgr.progress(tid, stage="排队中"))
    if not granted:
        return   # 排队期间被取消 / 删除：未占用名额，直接退出
    try:
        job = mgr.jobs.get(tid)
        if not job:
            return
        if mgr.is_cancelled(tid):
            mgr.progress(tid, status="cancelled", stage="已取消")
            return
        mgr.progress(tid, status="running", stage="处理中")
        try:
            await _live_process_loop(tid)
        except Exception as e:
            mgr.progress(tid, status="failed", stage="处理失败", log=str(e))
    finally:
        await mgr.semaphore.release()
        t = mgr.tasks.get(tid)
        if not t or t.get("status") in ("done", "cancelled") or tid not in mgr.tasks:
            mgr.jobs.pop(tid, None)
        _prune_failed_jobs()


async def _live_process_loop(tid):
    """Live 提取循环：逐单元处理；遇暂停/取消/删除则安全退出并保留进度。"""
    job = mgr.jobs.get(tid)
    if not job:
        return
    units = job["units"]
    cfg = job["cfg"]
    out_dir = job["out_dir"]
    total = len(units)
    mgr.progress(tid, status="running", stage="处理中")
    while job["processed"] < total:
        if mgr.is_cancelled(tid):
            mgr.progress(tid, status="cancelled", stage="已取消",
                         outputs=list(job["outputs"]))
            return
        if mgr.is_paused(tid):
            mgr.progress(tid, status="paused", stage="已暂停",
                         outputs=list(job["outputs"]))
            return
        if tid not in mgr.tasks:
            return
        unit = units[job["processed"]]
        stem = unit.get("stem", "?")
        try:
            out = await asyncio.get_event_loop().run_in_executor(
                None, lambda u=unit: live_svc.process_unit(u, cfg, out_dir))
            if out.get("ok"):
                job["outputs"].append(out["output"])
            else:
                mgr.progress(tid, log=f"{stem} 失败: {out.get('error', '')}")
        except Exception as e:
            mgr.progress(tid, log=f"{stem} 失败: {e}")
        job["processed"] += 1
        if tid not in mgr.tasks:
            return
        mgr.progress(tid, processed=job["processed"],
                     percent=int(job["processed"] / total * 100),
                     status="running", stage=f"提取 {stem}",
                     outputs=list(job["outputs"]))
    mgr.progress(tid, status="done", percent=100, stage="完成",
                 outputs=list(job["outputs"]))


@app.get("/api/output/{name}")
def get_output(name: str, request: Request, tid: str = ""):
    """产物下载：文件名白名单 + owner_id 归属 + 输出目录白名单。

    旧版会遍历全部任务目录并在根输出目录里按名字搜文件，可跨用户读取；
    现在只从当前用户可见任务中定位，不再做全局回落。
    """
    if not basename_ok(name):
        return fail("INVALID_PATH", "非法文件名", 400)
    user = cu(request)
    # 带 tid 时按任务精确定位：多个视频任务都输出「合成视频.mp4」，按名字搜会串文件
    if tid:
        t = owned_task(tid, request)
        if not t:
            return fail("FORBIDDEN", "无权访问该文件")
        od = t.get("out_dir") or ""
        if od and path_allowed(od, user) and name in (t.get("outputs") or []):
            p = Path(od) / name
            if p.exists():
                return FileResponse(str(p))
        return {"error": "not found"}
    candidates = []
    for t in visible_tasks(user):
        od = t.get("out_dir") or ""
        if name in (t.get("outputs") or []) and od and path_allowed(od, user):
            candidates.append(Path(od) / name)
    for p in candidates:
        if p.exists():
            return FileResponse(str(p))
    return {"error": "not found"}
