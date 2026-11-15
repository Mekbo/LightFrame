"""用户「我的空间」存储服务（阶段 6 前重要功能）。

设计要点（路径规划 v2）：
- 每位用户以 space/<uid>/ 为空间根目录，点开「我的空间」直接进入根目录。
- 系统自动创建作品根目录（默认「全部作品」），其下含图片/视频输出子目录
  OutPut_Image / OutPut_Video；所有产物统一落在作品根下，凡产物都在此目录中找。
- 作品根为系统保留目录：任何用户（含管理员）都不可改名 / 删除 / 移动；
  两个输出子目录名每用户可改（全局设置或空间中改名，两边自动同步），改名后
  磁盘目录原地重命名、旧产物保留；输出产物路径由系统自动填写，禁止用户改。
- 用户可在空间根目录自由创建素材目录（最多 2 层）；空间根目录不允许直接上传文件，
  作品目录为产物专用，同样不接受手动上传。
- 配额：默认值由全局设置控制（space_quota_admin_gb / space_quota_user_gb，
  默认管理员 50G、普通用户 10G）；管理员可随时调整任意用户配额。
- 上传限制（≤10 个文件 / ≤200M 总大小）由全局设置控制，默认即文档要求值。
- 分页：每目录默认 10 条，支持 10 / 20 / 50。
- 用量统计：增量维护（上传 +、删除 −、任务产出打脏标记），避免每次 info 都全盘扫描；
  缓存落盘 config/space_usage.json，脏标记时下次查询重扫一次自愈。
"""
from __future__ import annotations

import os
import re
import json
import mimetypes
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core.config import BASE_DIR, USERS_DIR, load_config, sanitize_uid

SPACE_ROOT = BASE_DIR / "space"
GIB = 1024 ** 3
USAGE_FILE = BASE_DIR / "config" / "space_usage.json"

# 允许上传 / 导入的扩展名（图片 / 视频 / 音频 / 文本），按应用统一约定分类
# 图片：png/jpg/jpeg/bmp/livp/heic
# 视频：mp4/mov/mkv/avi/wmv/flv
# 音频：mp3/wav/ogg/flac/aac
# 文本：txt/srt/md/ssr/ass/webvtt/doc/docx
IMAGE_EXT = {"png", "jpg", "jpeg", "bmp", "livp", "heic"}
VIDEO_EXT = {"mp4", "mov", "mkv", "avi", "wmv", "flv"}
AUDIO_EXT = {"mp3", "wav", "ogg", "flac", "aac"}
TEXT_EXT  = {"txt", "srt", "md", "ssr", "ass", "webvtt", "vtt", "doc", "docx"}
ALLOWED_EXT = IMAGE_EXT | VIDEO_EXT | AUDIO_EXT | TEXT_EXT

IMAGE_MIME_PREFIX = ("image/",)
VIDEO_MIME_PREFIX = ("video/",)
AUDIO_MIME_PREFIX = ("audio/",)
TEXT_MIME_PREFIX = ("text/",)

# 新建目录最多层数（相对空间根，作品根为系统保留，不计入用户自建上限）
MAX_FOLDER_DEPTH = 2

_NAME_RE = re.compile(r"^[^\x00-\x1f/\\:*?\"<>|]+$")


# ------------------------------ 路径与配置 ------------------------------

def quota_default(role: str) -> int:
    """默认配额：读全局设置（管理员 / 普通用户分别可调），异常回落 50G / 10G。"""
    try:
        cfg = load_config()
        key = "space_quota_admin_gb" if role == "admin" else "space_quota_user_gb"
        gb = float(cfg.get(key) or (50 if role == "admin" else 10))
        return int(gb * GIB)
    except Exception:
        return 50 * GIB if role == "admin" else 10 * GIB


def works_root_name(cfg: dict) -> str:
    return (cfg.get("space_root") or "全部作品").strip() or "全部作品"


def img_out_name(cfg: dict) -> str:
    return (cfg.get("space_img_out") or "OutPut_Image").strip() or "OutPut_Image"


def vid_out_name(cfg: dict) -> str:
    return (cfg.get("space_vid_out") or "OutPut_Video").strip() or "OutPut_Video"


def user_dir(uid: str) -> Path:
    return SPACE_ROOT / sanitize_uid(uid)


def works_dir(uid: str) -> Path:
    return user_dir(uid) / works_root_name(load_config(uid))


def img_out_dir(uid: str) -> Path:
    return works_dir(uid) / img_out_name(load_config(uid))


def vid_out_dir(uid: str) -> Path:
    return works_dir(uid) / vid_out_name(load_config(uid))


def lan_mode() -> bool:
    try:
        return bool((load_config().get("server") or {}).get("lan_mode", False))
    except Exception:
        return False


def _safe_join(uid: str, rel: str) -> Path:
    """把相对路径安全拼到 空间/<uid>/ 之下，禁止任何越界。"""
    base = user_dir(uid)
    rel = (rel or "").strip().strip("/").replace("\\", "/")
    target = base
    for seg in rel.split("/"):
        if seg in ("", ".", ".."):
            continue
        target = target / seg
    # 必须在 base 之内
    try:
        target.resolve().relative_to(base.resolve())
    except Exception:
        raise ValueError("非法路径：超出用户空间范围")
    return target


def _depth(rel: str) -> int:
    return len([s for s in (rel or "").split("/") if s and s not in (".", "..")])


# ------------------------------ 空间初始化 ------------------------------

def ensure_space(uid: str) -> dict:
    """确保用户空间与系统目录存在；返回关键信息。"""
    cfg = load_config(uid)
    ud = user_dir(uid)
    wd = works_dir(uid)
    iod = img_out_dir(uid)
    vod = vid_out_dir(uid)
    for d in (ud, wd, iod, vod):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return {
        "uid": sanitize_uid(uid),
        "works_root": works_root_name(cfg),
        "image_out": img_out_name(cfg),
        "video_out": vid_out_name(cfg),
        "lan_mode": lan_mode(),
    }


# ------------------------------ 配额与用量 ------------------------------
# 用量增量缓存：{uid: {"bytes": int, "dirty": bool}}
# - 上传成功 add_usage(+n)、删除 sub_usage(-n)、改名/移动不变；
# - 任务产出 / 产物删除等后端直接写盘的场景 mark_dirty，下次查询重扫一次自愈；
# - 全部在锁内操作，缓存落盘，服务重启后仍有值（脏标记触发首查重扫兜底）。

_usage_lock = threading.Lock()
_usage_cache: Optional[dict] = None


def _load_usage() -> dict:
    global _usage_cache
    if _usage_cache is None:
        try:
            _usage_cache = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
            if not isinstance(_usage_cache, dict):
                _usage_cache = {}
        except Exception:
            _usage_cache = {}
    return _usage_cache


def _save_usage() -> None:
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(_usage_cache or {}, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        pass


def _scan_usage(uid: str) -> int:
    ud = user_dir(uid)
    total = 0
    if not ud.exists():
        return 0
    for root, _dirs, files in os.walk(str(ud)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total


def used_bytes(uid: str) -> int:
    uid = sanitize_uid(uid)
    with _usage_lock:
        cache = _load_usage()
        entry = cache.get(uid)
        if entry and not entry.get("dirty") and isinstance(entry.get("bytes"), int):
            return entry["bytes"]
        b = _scan_usage(uid)
        cache[uid] = {"bytes": b, "dirty": False}
        _save_usage()
        return b


def add_usage(uid: str, delta: int) -> None:
    if not delta:
        return
    uid = sanitize_uid(uid)
    with _usage_lock:
        cache = _load_usage()
        e = cache.get(uid) or {}
        if e.get("dirty"):
            return          # 反正要重扫，无需增量
        e["bytes"] = max(0, int(e.get("bytes") or 0) + int(delta))
        cache[uid] = e
        _save_usage()


def mark_dirty(uid: str) -> None:
    """标记用户用量待重扫（任务产出等后端直接写盘后调用）。"""
    if not uid:
        return
    uid = sanitize_uid(uid)
    with _usage_lock:
        cache = _load_usage()
        e = cache.get(uid) or {}
        e["dirty"] = True
        cache[uid] = e
        _save_usage()


def quota_bytes(uid: str) -> int:
    from ..core import auth  # 延迟导入，避免循环依赖
    u = auth.get_user(uid) or {}
    q = u.get("space_quota_bytes")
    if not q:
        q = quota_default(u.get("role", "user"))
    return int(q)


def space_info(uid: str) -> dict:
    cfg = load_config(uid)
    ensure_space(uid)
    used = used_bytes(uid)
    quota = quota_bytes(uid)
    return {
        "quota_bytes": quota,
        "used_bytes": used,
        "free_bytes": max(0, quota - used),
        "usage_ratio": round(used / quota, 4) if quota else 0,
        "warn": used >= quota or (quota and used / quota >= 0.9),
        "works_root": works_root_name(cfg),
        "image_out": img_out_name(cfg),
        "video_out": vid_out_name(cfg),
        "lan_mode": lan_mode(),
        "max_files": int(load_config().get("space_upload_max_files", 10)),
        "max_total_mb": int(load_config().get("space_upload_max_total_mb", 200)),
        "page_size": int(load_config().get("space_page_size", 10)),
    }


# ------------------------------ 目录列举与分页 ------------------------------

def _is_image(name: str, mime: str = "") -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in IMAGE_EXT or mime.startswith("image/")


def _is_video(name: str, mime: str = "") -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in VIDEO_EXT or mime.startswith("video/")


def _is_audio(name: str, mime: str = "") -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in AUDIO_EXT or mime.startswith("audio/")


def _is_text(name: str, mime: str = "") -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in TEXT_EXT or mime.startswith("text/")


def list_dir(uid: str, rel: str = "", page: int = 1, per_page: int = 10) -> dict:
    base = _safe_join(uid, rel)
    if not base.exists():
        base = ensure_dir = base
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    cfg = load_config(uid)
    root = works_root_name(cfg)
    img_rel = f"{root}/{img_out_name(cfg)}"
    vid_rel = f"{root}/{vid_out_name(cfg)}"
    entries = []
    try:
        for name in os.listdir(str(base)):
            p = base / name
            if p.is_dir():
                kind = "dir"
                size = 0
                mtime = p.stat().st_mtime
            elif p.is_file():
                kind = ("image" if _is_image(name) else
                        "video" if _is_video(name) else "file")
                size = p.stat().st_size
                mtime = p.stat().st_mtime
            else:
                continue
            rel_path = (rel.strip("/") + "/" + name).strip("/")
            # 系统产物目录（OutPut_Image / OutPut_Video）内的文件，是任务产出，
            # 不应作为「从空间添加」的源素材出现在选择器中
            in_sys_out = (kind != "dir" and (
                rel_path.startswith(img_rel + "/") or rel_path.startswith(vid_rel + "/")))
            entries.append({
                "name": name, "kind": kind, "size": size,
                "mtime": datetime.fromtimestamp(mtime).isoformat(),
                "rel": rel_path,
                # 作品根：任何用户（含管理员）不可改名 / 删除 / 移动
                "protected": (kind == "dir" and rel_path == root),
                # 系统输出子目录：可改名（自动同步配置），不可删除 / 移动
                "sysdir": (kind == "dir" and rel_path in (img_rel, vid_rel)) or in_sys_out,
            })
    except Exception:
        pass
    # 目录优先，再按名称
    entries.sort(key=lambda e: (0 if e["kind"] == "dir" else 1, e["name"].lower()))
    total = len(entries)
    per_page = per_page if per_page in (10, 20, 50) else 10
    page = max(1, page)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    return {
        "rel": rel.strip("/"),
        "entries": entries[start:start + per_page],
        "page": page, "per_page": per_page,
        "total": total, "pages": pages,
        "can_create_subdir": _depth(rel) < MAX_FOLDER_DEPTH,
        "can_upload": bool(rel.strip("/")) and not _in_works_root(uid, rel),
    }


# ------------------------------ 文件 / 目录操作 ------------------------------

def create_folder(uid: str, rel: str, name: str) -> dict:
    name = (name or "").strip()
    if not name or not _NAME_RE.match(name):
        raise ValueError("目录名不合法（不能含 / \\ : * ? \" < > | 及控制字符）")
    if _in_works_root(uid, rel):
        raise ValueError("作品目录为产物专用、由系统管理；请在空间根目录或自建素材目录中创建文件夹")
    parent = _safe_join(uid, rel)
    if _depth(rel) >= MAX_FOLDER_DEPTH:
        raise ValueError(f"素材目录最多 {MAX_FOLDER_DEPTH} 层，无法再建子文件夹")
    target = parent / name
    if target.exists():
        raise ValueError("该目录已存在")
    try:
        target.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        raise ValueError("创建失败：" + str(e)[:120])
    return {"name": name, "rel": (rel.strip("/") + "/" + name).strip("/"), "kind": "dir"}


def _validate_upload(uid: str, rel: str, items) -> Path:
    cfg = load_config()
    max_files = int(cfg.get("space_upload_max_files", 10))
    max_total_mb = int(cfg.get("space_upload_max_total_mb", 200))
    if len(items) > max_files:
        raise ValueError(f"一次最多上传 {max_files} 个文件")
    total = sum(len(b) for _n, b in items)
    if total > max_total_mb * 1024 * 1024:
        raise ValueError(f"单次上传总大小不可超过 {max_total_mb} MB")
    # 配额预判
    used = used_bytes(uid)
    quota = quota_bytes(uid)
    if used + total > quota:
        raise ValueError("空间不足：超出配额，请先删除不需要的素材或产物")
    return _safe_join(uid, rel)


def save_upload(uid: str, rel: str, items) -> list:
    """items: list of (filename, bytes)。返回写入结果列表。

    空间根目录不允许直接上传文件（请先进入或新建子目录）；
    作品目录为产物专用，同样不接受手动上传。
    """
    if not items:
        raise ValueError("未选择文件")
    base_rel = (rel or "").strip("/")
    if not base_rel:
        raise ValueError("空间根目录不允许直接上传文件，请先进入或新建子目录")
    if _in_works_root(uid, rel):
        raise ValueError("作品目录为产物专用；请上传到自建素材目录")
    base = _validate_upload(uid, rel, items)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    results = []
    for fname, data in items:
        fname = os.path.basename(str(fname or "")).strip()
        if not fname or fname in (".", ".."):
            continue
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        if ext and ext not in ALLOWED_EXT:
            results.append({"name": fname, "ok": False,
                            "error": "不支持的文件类型"})
            continue
        dest = base / fname
        # 重名不覆盖，自动加 (n)
        if dest.exists():
            stem, suffix = fname.rsplit(".", 1) if "." in fname else (fname, "")
            i = 1
            while dest.exists():
                dest = base / (f"{stem}({i}).{suffix}" if suffix else f"{stem}({i})")
                i += 1
        try:
            dest.write_bytes(data)
            results.append({"name": dest.name, "ok": True, "size": len(data)})
        except Exception as e:
            results.append({"name": fname, "ok": False, "error": str(e)[:120]})
    ok = [r for r in results if r["ok"]]
    if not ok:
        raise ValueError(results[0]["error"] if results else "上传失败")
    # 增量计用量：成功写入的字节数直接累加，避免下次查询全盘扫描
    add_usage(uid, sum(int(r.get("size") or 0) for r in ok))
    return results


def _is_protected(uid: str, rel: str) -> bool:
    """作品根目录：任何用户（含管理员）不可改名 / 删除 / 移动（不分单机 / 局域网）。"""
    return (rel or "").strip("/") == works_root_name(load_config(uid))


def _in_works_root(uid: str, rel: str) -> bool:
    """路径是否位于作品根目录之内（含作品根本身）。"""
    r = (rel or "").strip("/")
    if not r:
        return False
    root = works_root_name(load_config(uid))
    return r == root or r.startswith(root + "/")


def is_out_dir(uid: str, rel: str) -> bool:
    """系统输出子目录（OutPut_Image / OutPut_Video）：可改名（自动同步配置），不可删除 / 移动。"""
    cfg = load_config(uid)
    root = works_root_name(cfg)
    r = (rel or "").strip("/")
    return r in (f"{root}/{img_out_name(cfg)}", f"{root}/{vid_out_name(cfg)}")


def _set_user_key(uid: str, kv: dict) -> None:
    """只把指定键写入用户个人配置文件（不冻结全局快照，避免覆盖之后的全局修改）。"""
    uid = sanitize_uid(uid)
    p = USERS_DIR / f"{uid}.json"
    cur = {}
    try:
        if p.exists():
            cur = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        cur = {}
    cur.update(kv)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass


def rename_path(uid: str, rel: str, new_name: str) -> dict:
    new_name = (new_name or "").strip()
    if not new_name or not _NAME_RE.match(new_name):
        raise ValueError("名称不合法")
    if not rel.strip("/"):
        raise ValueError("不能重命名空间根")
    if _is_protected(uid, rel):
        raise ValueError("作品根目录为系统保留目录，不可改名")
    cfg = load_config(uid)
    root = works_root_name(cfg)
    r = rel.strip("/")
    out_key = None
    if r == f"{root}/{img_out_name(cfg)}":
        out_key = "space_img_out"
    elif r == f"{root}/{vid_out_name(cfg)}":
        out_key = "space_vid_out"
    src = _safe_join(uid, rel)
    if not src.exists():
        raise ValueError("目标不存在")
    dst = src.parent / new_name
    if dst.exists():
        raise ValueError("同名已存在")
    try:
        src.rename(dst)
    except Exception as e:
        raise ValueError("重命名失败：" + str(e)[:120])
    # 输出子目录改名 → 同步用户配置（全局设置与空间两处信息保持一致）
    if out_key:
        _set_user_key(uid, {out_key: new_name})
    return {"rel": (src.parent.relative_to(user_dir(uid)).as_posix() + "/" + new_name).strip("/"),
            "name": new_name, "synced_config": bool(out_key)}


def sync_out_dirs(uid: str, old_cfg: dict, new_cfg: dict) -> dict:
    """全局设置修改输出目录名后，把磁盘上的旧目录原地改名为新名（产物内容保留）。

    返回 {"renamed": ["OutPut_Image → 图片产物", ...], "locked": str}
    - 作品根目录名已锁定：old/new 不一致时忽略并说明，不做任何改动；
    - 只对同名差异做磁盘 rename；目录不存在（如尚无产物）则跳过。
    """
    renamed = []
    locked = ""
    old_root = works_root_name(old_cfg or {})
    new_root = works_root_name(new_cfg or {})
    if old_root != new_root:
        locked = "作品根目录名由系统锁定，未作修改"
    pairs = [
        (img_out_name(old_cfg or {}), img_out_name(new_cfg or {})),
        (vid_out_name(old_cfg or {}), vid_out_name(new_cfg or {})),
    ]
    ud = user_dir(uid)
    for old, new in pairs:
        if old == new:
            continue
        src = ud / old_root / old
        dst = ud / old_root / new
        if src.exists() and not dst.exists():
            try:
                src.rename(dst)
                renamed.append(f"{old} → {new}")
            except Exception:
                pass
    return {"renamed": renamed, "locked": locked}


def move_paths(uid: str, paths: list, dst_rel: str) -> dict:
    if _in_works_root(uid, dst_rel):
        raise ValueError("不能把文件移入作品目录（产物专用）；请移入素材目录")
    dst = _safe_join(uid, dst_rel)
    if not dst.is_dir():
        raise ValueError("目标必须是目录")
    done = 0
    for rel in (paths or []):
        if not rel or _is_protected(uid, rel) or is_out_dir(uid, rel):
            continue
        src = _safe_join(uid, rel)
        if not src.exists() or src.is_dir():
            continue
        try:
            dest = dst / src.name
            if dest.exists():
                continue
            src.rename(dest)
            done += 1
        except Exception:
            pass
    return {"moved": done}


def delete_paths(uid: str, paths: list) -> dict:
    done = 0
    removed = 0
    for rel in (paths or []):
        if not rel or _is_protected(uid, rel) or is_out_dir(uid, rel):
            continue
        if not rel.strip("/"):
            continue
        target = _safe_join(uid, rel)
        try:
            if target.is_dir():
                import shutil
                # 先统计将删除的体量（目录递归求和），删除后做减量
                for root, _ds, files in os.walk(str(target)):
                    for f in files:
                        try:
                            removed += os.path.getsize(os.path.join(root, f))
                        except Exception:
                            pass
                shutil.rmtree(str(target), ignore_errors=True)
            elif target.is_file():
                try:
                    removed += target.stat().st_size
                except Exception:
                    pass
                target.unlink(missing_ok=True)
            else:
                continue
            done += 1
        except Exception:
            pass
    if removed:
        add_usage(uid, -removed)
    return {"deleted": done}


# ------------------------------ 文件读取（预览 / 下载） ------------------------------

def resolve_file(uid: str, rel: str) -> Optional[Path]:
    p = _safe_join(uid, rel)
    if p.is_file():
        return p
    return None


def collect_space_files(uid: str, rels: list, kinds: list) -> list:
    """把用户选中的 rel（可为文件或目录）展开为 [(文件名, 字节), ...] 列表，供任务端点取用。

    - 文件：直接按其类型是否落在 kinds 内决定纳入；
    - 目录：递归收集其下所有文件，按 kinds 过滤（实现「从空间添加（文件/目录）」）；
    - 任何越界 / 不存在 / 读失败的条目都安全跳过，不中断整体。
    """
    accepted = set(kinds or ["image", "video", "file"])
    out = []
    seen = set()

    def walk(rel: str):
        try:
            base = _safe_join(uid, rel)
        except ValueError:
            return
        if not base.exists():
            return
        if base.is_file():
            meta = file_meta(uid, rel)
            if meta and meta["kind"] in accepted and rel not in seen:
                seen.add(rel)
                try:
                    out.append((meta["name"], base.read_bytes()))
                except Exception:
                    pass
            return
        if base.is_dir():
            # 递归收集时跳过系统产物目录（OutPut_Image / OutPut_Video），
            # 避免把任务产出当作源素材纳入任务（用户误选作品根/上层目录时也安全）
            if is_out_dir(uid, rel):
                return
            try:
                for name in sorted(os.listdir(str(base))):
                    child = (rel.strip("/") + "/" + name).strip("/")
                    walk(child)
            except Exception:
                pass

    for r in (rels or []):
        if r:
            walk(str(r))
    return out


def expand_dir(uid: str, rel: str, kinds: list) -> list:
    """把目录 rel 递归展开为文件元数据列表 [{rel,name,kind,size}, ...]，不读取字节。

    供前端「从空间目录添加」时把目录即时展开成真实文件卡片：用户选中目录后能
    在工作区看到里面实际的图片/视频缩略图与正确数量，而不是一个「文件夹」占位符。
    递归时跳过系统产物目录（OutPut_Image / OutPut_Video）。
    """
    accepted = set(kinds or ["image", "video", "file"])
    out = []
    seen = set()

    def walk(r: str):
        try:
            base = _safe_join(uid, r)
        except ValueError:
            return
        if not base.exists():
            return
        if base.is_file():
            meta = file_meta(uid, r)
            if meta and meta["kind"] in accepted and r not in seen:
                seen.add(r)
                out.append({"rel": r, "name": meta["name"],
                            "kind": meta["kind"], "size": meta.get("size", 0)})
            return
        if base.is_dir():
            # 递归展开时跳过系统产物目录（任务产出，不应作为源素材）
            if is_out_dir(uid, r):
                return
            try:
                for name in sorted(os.listdir(str(base))):
                    child = (r.strip("/") + "/" + name).strip("/")
                    walk(child)
            except Exception:
                pass

    walk(str(rel))
    return out


def read_space_file(uid: str, rel: str):
    """读取单个空间文件返回 (文件名, 字节)；非法 / 不存在 / 非文件返回 None。"""
    if not rel:
        return None
    meta = file_meta(uid, str(rel))
    # audio 必须放行：在线用户从「我的空间」添加的配音 / 背景乐 kind 为 audio，
    # 旧白名单只收 image/video/file，会把空间音频读成 None，触发「配音与脚本不一致」。
    if not meta or meta["kind"] not in ("image", "video", "audio", "text", "file"):
        return None
    p = resolve_file(uid, str(rel))
    if not p:
        return None
    try:
        return (meta["name"], p.read_bytes())
    except Exception:
        return None


def file_meta(uid: str, rel: str) -> Optional[dict]:
    p = resolve_file(uid, rel)
    if not p:
        return None
    name = p.name
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {
        "name": name, "rel": rel.strip("/"), "size": p.stat().st_size,
        "mime": mime, "kind": "image" if _is_image(name, mime) else
        "video" if _is_video(name, mime) else
        "audio" if _is_audio(name, mime) else
        "text" if _is_text(name, mime) else "file",
    }
