# -*- coding: utf-8 -*-
"""视频创作「合成预设」：按用户隔离保存九步参数，不保存任何素材/脚本内容。

设计约定（与模板中心区分）：
- 模板中心 = 字幕/标题「样式」的唯一数据源，预设只保存 template_id 引用；
- 预设 = 一次合成的全部「参数组合」，便于批量出片时一键复用；
- 不存素材字节、不存脚本文本、不存配音/封面，落盘只有一个 JSON；
- uid 永远由服务端会话注入，service 不接收前端传来的身份字段。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from ..core.config import BASE_DIR, sanitize_uid

PRESET_ROOT = BASE_DIR / "data" / "presets"
MAX_PRESETS = 30
MAX_NAME = 60
MAX_CONFIG_BYTES = 64 * 1024

# 只保存这些参数键（白名单）；scripts / template / uid / owner 等一律剔除。
_ALLOWED = (
    "aspect", "resolution", "duration_strategy", "target_duration",
    "clip_duration", "transition", "transitions", "transition_duration",
    "blur_sigma", "cover", "cover_duration", "video_count", "mix_mode",
    "reshuffle", "short_video_mode", "motion", "template_id",
    "voice_volume", "bgm_volume", "bgm_fade", "subtitle_fade",
    "originality_enabled", "originality_k",
)


def _file(uid: str) -> Path:
    return PRESET_ROOT / (sanitize_uid(uid or "") + ".json")


def _load(uid: str) -> list:
    try:
        p = _file(uid)
        if p.exists():
            v = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(v, list):
                return v
    except Exception:
        pass
    return []


def _save(uid: str, rows: list) -> None:
    p = _file(uid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


class PresetError(Exception):
    pass


def _clean_config(cfg: dict) -> dict:
    video = (cfg or {}).get("video") if isinstance(cfg, dict) else None
    if not isinstance(video, dict):
        raise PresetError("配置格式不正确（缺少 video 参数）")
    out = {k: video[k] for k in _ALLOWED if k in video}
    # 转场集合可能是 JS 数组或 Set 序列化结果，统一成字符串数组
    tr = out.get("transitions")
    if isinstance(tr, str):
        try:
            parsed = json.loads(tr)
            out["transitions"] = parsed if isinstance(parsed, list) else []
        except Exception:
            out["transitions"] = []
    elif isinstance(tr, (list, tuple)):
        out["transitions"] = [str(x) for x in tr]
    # template_id 仅保存 id，绝不保存样式对象（样式仍以模板中心为准）
    tid = str(out.get("template_id") or "").strip()
    if tid and not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", tid):
        tid = ""
    out["template_id"] = tid
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > MAX_CONFIG_BYTES:
        raise PresetError("配置过大，无法保存为预设")
    return {"video": out}


def list_presets(uid: str) -> list:
    rows = _load(uid)
    rows.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return [{"id": r.get("id", ""), "name": r.get("name", ""),
             "created_at": r.get("created_at", ""),
             "summary": _summary(r.get("config") or {})} for r in rows]


def _summary(config: dict) -> str:
    v = (config or {}).get("video") or {}
    bits = []
    if v.get("aspect"):
        bits.append(str(v["aspect"]))
    if v.get("resolution"):
        bits.append(str(v["resolution"]))
    if v.get("target_duration"):
        bits.append(f"{v['target_duration']}s")
    if v.get("clip_duration"):
        bits.append(f"每段{v['clip_duration']}s")
    return " · ".join(bits)


def create_preset(uid: str, body: dict) -> list:
    name = str((body or {}).get("name") or "").strip()[:MAX_NAME]
    if not name:
        raise PresetError("请填写预设名称")
    config = _clean_config(body.get("config"))
    rows = _load(uid)
    if len(rows) >= MAX_PRESETS:
        raise PresetError(f"预设最多保存 {MAX_PRESETS} 个，请先删除不用的")
    row = {"id": "ps_" + uuid.uuid4().hex[:12],
           "name": name,
           "created_at": datetime.now().isoformat(timespec="seconds"),
           "config": config}
    rows.insert(0, row)
    _save(uid, rows)
    return list_presets(uid)


def delete_preset(uid: str, pid: str) -> list:
    if not re.fullmatch(r"ps_[A-Za-z0-9]{1,32}", pid or ""):
        raise PresetError("预设 ID 不合法")
    rows = [r for r in _load(uid) if r.get("id") != pid]
    _save(uid, rows)
    return list_presets(uid)


def get_preset(uid: str, pid: str) -> dict:
    if not re.fullmatch(r"ps_[A-Za-z0-9]{1,32}", pid or ""):
        raise PresetError("预设 ID 不合法")
    for r in _load(uid):
        if r.get("id") == pid:
            config = r.get("config") or {}
            video = dict(config.get("video") or {})
            # template_id 指向的模板可能已被删除；前端载入时自行校验下拉是否存在
            return {"id": r.get("id"), "name": r.get("name", ""), "config": {"video": video}}
    raise PresetError("预设不存在或已被删除")
