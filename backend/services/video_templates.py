"""视频模板：指定「字幕」与「视频顶部标题」的位置和样式。

设计对齐「图片加文字」模板：只保存样式配置 JSON，不保存任何预览底图。
模板以 JSON 存于 backend/video_templates/<id>.json，结构：

{
  "id": "vtpl_xxx",
  "name": "底部白字 + 顶部金标题",
  "spec": "9:16",
  "subtitle_enabled": true,
  "title_enabled": true,
  "subtitle": { ...templates.normalize_style 的样式... },
  "title":    { ...templates.normalize_style 的样式... }
}

字幕与标题共用图片加文字的样式字段（font/color/weight/stroke/shadow/opacity/
pos/anchor/size_ratio/max_width_ratio/line_spacing），因此同一套模板渲染逻辑
既能用于图片加文字，也能用于视频硬字幕，跨尺寸效果一致。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from .templates import normalize_style

VTPL_DIR = Path(__file__).resolve().parent.parent / "video_templates"
VTPL_DIR.mkdir(parents=True, exist_ok=True)


def default_subtitle_style() -> dict:
    """默认字幕样式：底部居中白字 + 黑描边。"""
    return normalize_style({
        "spec": "9:16", "font": "思源黑体", "color": "#FFFFFF",
        "weight": 700, "stroke": {"width": 2, "color": "#000000"},
        "shadow": {"width": 0}, "opacity": 1.0,
        "pos": {"x": 0.5, "y": 0.86}, "anchor": "center",
        "size_ratio": 0.042, "max_width_ratio": 0.86, "line_spacing": 1.25,
    })


def default_title_style() -> dict:
    """默认顶部标题样式：顶部居中金字 + 深色描边（红金高级感，可在模板里改）。"""
    return normalize_style({
        "spec": "9:16", "font": "思源黑体", "color": "#FFD700",
        "weight": 900, "stroke": {"width": 2, "color": "#3A2410"},
        "shadow": {"width": 0}, "opacity": 1.0,
        "pos": {"x": 0.5, "y": 0.07}, "anchor": "center",
        "size_ratio": 0.055, "max_width_ratio": 0.88, "line_spacing": 1.2,
    })


def default_template() -> dict:
    """未选择模板时使用的内置默认模板（保证不建模板也能加字幕/标题）。"""
    return {
        "id": "", "name": "内置默认模板", "spec": "9:16",
        "subtitle_enabled": True, "title_enabled": True,
        "subtitle": default_subtitle_style(), "title": default_title_style(),
    }


def normalize_video_template(data: dict) -> dict:
    """补全并规范化视频模板：两组样式都走图片加文字的 normalize_style。"""
    if not isinstance(data, dict):
        data = {}
    spec = data.get("spec") if data.get("spec") in ("9:16", "16:9") else "9:16"
    sub = dict(data.get("subtitle") or {})
    ttl = dict(data.get("title") or {})
    sub.setdefault("spec", spec)
    ttl.setdefault("spec", spec)
    return {
        "id": data.get("id", ""),
        "name": (data.get("name") or "未命名视频模板").strip() or "未命名视频模板",
        "spec": spec,
        "subtitle_enabled": bool(data.get("subtitle_enabled", True)),
        "title_enabled": bool(data.get("title_enabled", True)),
        "subtitle": normalize_style({**default_subtitle_style(), **sub, "spec": spec}),
        "title": normalize_style({**default_title_style(), **ttl, "spec": spec}),
    }


def list_templates() -> list:
    out = []
    for f in sorted(VTPL_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def get_template(tid: str):
    p = VTPL_DIR / f"{tid}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_template(data: dict, meta: dict = None) -> dict:
    """保存视频模板。归属字段由服务端 meta 注入（normalize 会丢弃额外字段，故在此补齐）。"""
    tid = data.get("id") or ("vtpl_" + uuid.uuid4().hex[:8])
    existing = get_template(tid) if data.get("id") else None
    rec = normalize_video_template(data)
    rec["id"] = tid
    rec.pop("preview_image", None)  # 不保存预览底图
    meta = meta or {}
    if existing:
        rec["owner"] = existing.get("owner", "")
        rec["owner_name"] = existing.get("owner_name", "")
        rec["created_at"] = existing.get("created_at", "")
        default_shared = bool(existing.get("shared", False))
    else:
        rec["owner"] = meta.get("owner", "")
        rec["owner_name"] = meta.get("owner_name", "")
        rec["created_at"] = datetime.now().isoformat(timespec="seconds")
        default_shared = False
    shared = meta.get("shared")
    rec["shared"] = default_shared if shared is None else bool(shared)
    (VTPL_DIR / f"{tid}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def delete_template(tid: str) -> bool:
    p = VTPL_DIR / f"{tid}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def resolve(video_cfg: dict) -> dict:
    """从视频任务配置里取出可用的模板样式对象（无模板则用内置默认）。"""
    tpl = (video_cfg or {}).get("template")
    if isinstance(tpl, dict) and tpl.get("subtitle"):
        return normalize_video_template(tpl)
    return default_template()
