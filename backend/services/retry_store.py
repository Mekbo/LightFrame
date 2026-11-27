# -*- coding: utf-8 -*-
"""失败任务的重试上下文持久化（服务重启后仍可原样重试）。

设计要点：
- 只在任务「失败」时落盘，成功 / 取消 / 删除任务时立即清理；
- 素材字节写成独立二进制文件（而非塞进 JSON），避免大型视频把 tasks.json 撑爆；
- context.json 记录 owner_id，恢复时与任务记录二次校验，防篡改 / 跨用户串用；
- 保留最近 N 条且有总容量上限，防止长期积累占满磁盘；
- 本模块只做存取，不 import main，避免循环依赖。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from ..core.config import BASE_DIR

ROOT = BASE_DIR / "config" / "retry_context"
KEEP = 8
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024   # 2GB 总上限
SLOTS = ("items", "covers", "voices", "bgm")


def _dir(tid: str) -> Path:
    return ROOT / (re.sub(r"[^A-Za-z0-9_\-]", "", tid or "")[:40] or "_invalid")


def _safe_name(name: str, i: int) -> str:
    base = re.sub(r"[^A-Za-z0-9._\-]", "_", os.path.basename(str(name or "")))[:80] or "file"
    return "%03d_%s" % (i, base)


def save(tid: str, owner_id: str, kind: str, job: dict) -> bool:
    """把 mgr.jobs[tid] 中的素材字节与配置落盘。成功返回 True。"""
    d = _dir(tid)
    tmp = ROOT / ("_" + d.name + ".tmp")
    try:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        ctx = {"tid": tid, "owner_id": owner_id or "", "kind": kind or "video",
               "out_dir": str((job or {}).get("out_dir") or ""),
               "created_at": datetime.now().isoformat(timespec="seconds"),
               "slots": {}}
        for slot in SLOTS:
            vals = list((job or {}).get(slot) or [])
            names = []
            for i, val in enumerate(vals):
                if val is None:          # server_voices 的 null 占位
                    names.append(None)
                    continue
                try:
                    name, data = val
                except Exception:
                    names.append(None)
                    continue
                sub = tmp / slot
                sub.mkdir(parents=True, exist_ok=True)
                (sub / _safe_name(name, i)).write_bytes(bytes(data))
                names.append(str(name))
            ctx["slots"][slot] = names
        # cfg / user 等纯结构化数据直接存 JSON；不可序列化的字段降级为字符串
        ctx["cfg"] = (job or {}).get("cfg") or {}
        ctx["user"] = (job or {}).get("user") or {}
        ctx["processed"] = int((job or {}).get("processed") or 0)
        (tmp / "context.json").write_text(
            json.dumps(ctx, ensure_ascii=False, default=str), encoding="utf-8")
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        tmp.replace(d)
        prune()
        return True
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return False


def load(tid: str, expected_owner: str) -> dict | None:
    """恢复上下文；owner 不匹配或文件损坏返回 None。"""
    d = _dir(tid)
    try:
        ctx = json.loads((d / "context.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if str(ctx.get("owner_id") or "") != str(expected_owner or ""):
        return None
    if ctx.get("tid") != tid:
        return None
    job = {"cfg": ctx.get("cfg") or {}, "out_dir": ctx.get("out_dir") or "",
           "user": ctx.get("user") or {}, "processed": int(ctx.get("processed") or 0),
           "outputs": []}
    try:
        for slot in SLOTS:
            names = (ctx.get("slots") or {}).get(slot) or []
            vals = []
            sub = d / slot
            for i, name in enumerate(names):
                if name is None:
                    vals.append(None)
                    continue
                p = sub / _safe_name(name, i)
                vals.append((str(name), p.read_bytes()))
            job[slot] = vals
    except Exception:
        return None
    return job


def remove(tid: str) -> None:
    shutil.rmtree(_dir(tid), ignore_errors=True)


def prune(keep: int = KEEP, max_bytes: int = MAX_TOTAL_BYTES) -> None:
    """按创建时间保留最近 keep 条，且总容量不超过 max_bytes。"""
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        rows = []
        for d in ROOT.iterdir():
            if not d.is_dir() or d.name.startswith("_"):
                continue
            try:
                ctx = json.loads((d / "context.json").read_text(encoding="utf-8"))
                rows.append((str(ctx.get("created_at") or ""),
                             sum(p.stat().st_size for p in d.rglob("*") if p.is_file()), d))
            except Exception:
                shutil.rmtree(d, ignore_errors=True)
        rows.sort(key=lambda x: x[0])
        total = sum(r[1] for r in rows)
        while len(rows) > keep or (rows and total > max_bytes):
            _ts, size, d = rows.pop(0)
            shutil.rmtree(d, ignore_errors=True)
            total -= size
    except Exception:
        pass
