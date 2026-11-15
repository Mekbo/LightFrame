"""AI 提示词模板：按场景创建带角色特点的提示词，用 {} 标识可替换块。

设计对齐「图片加文字 / 视频模板」：模板只保存「内容配置」JSON，不保存任何预览底图。
模板以 JSON 存于 backend/prompt_templates/<id>.json，结构：

{
  "id": "ptpl_xxx",
  "name": "小红书瓷砖种草",
  "scene": "小红书种草笔记",        # 场景
  "role": "资深家居装修博主",        # 角色特点（本身也可作为可替换块 {role}）
  "body": "你是{role}，请为{product}写一篇{platform}种草文案……",  # 正文，含 {} 可替换块
  "blocks": [{"key":"role","label":"角色","default":"","value":""}, ...],  # 由 body 解析 + 用户填充
  "owner": "...", "owner_name": "...", "shared": false, "created_at": "..."
}

{} 块规则：
- 正文里用 {key} 标识「可替换块」，编辑时高亮显示、点击弹窗修改其 label/default/value。
- 这些块是给用户填的「占位脚手架」，最终给 AI 的提示词 = 正文把每个 {key} 替换为填充值（value 优先、缺省用 default）。
- 渲染（build_prompt）只返回「填好的最终提示词」，正文里的 {} 标记本身不会发给 AI。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path

PTPL_DIR = Path(__file__).resolve().parent.parent / "prompt_templates"
PTPL_DIR.mkdir(parents=True, exist_ok=True)

# 匹配 {中文/英文/数字/下划线}，块名允许中文（如 {产品名}）
_BLOCK_RE = re.compile(r"\{\s*([A-Za-z0-9_\u4e00-\u9fa5]+)\s*\}")

# 应用场景标签：模板中心是唯一数据源，各工作场景按 scopes 自动加载供点选。
# 存量模板 scopes 为空 = 通用（所有场景都能选用），保证「自动加载」不漏模板。
SCOPES = ("image_text", "topic", "script", "title", "cover")
SCOPE_LABELS = {
    "image_text": "图片文案",
    "topic": "选题提示词",
    "script": "口播脚本",
    "title": "视频标题",
    "cover": "封面文案",
}


def applies_to(rec: dict, scope: str) -> bool:
    """模板是否适用于某个工作场景：未打标签（存量/通用）一律适用。"""
    tags = (rec or {}).get("scopes") or []
    if not isinstance(tags, (list, tuple)):
        tags = [tags]
    if not tags:
        return True
    return scope in tags


def parse_blocks(body: str) -> list:
    """从模板正文提取 {} 块，返回有序去重列表 [{key, label}]。"""
    seen = set()
    out = []
    for m in _BLOCK_RE.findall(body or ""):
        if m not in seen:
            seen.add(m)
            out.append({"key": m, "label": m})
    return out


def normalize(data: dict) -> dict:
    """补全并规范化 AI 提示词模板：以 body 中的 {} 为准，合并既有块的填充值。"""
    if not isinstance(data, dict):
        data = {}
    body = str(data.get("body") or "")
    parsed = parse_blocks(body)
    existing = {b.get("key"): b for b in (data.get("blocks") or [])
                if isinstance(b, dict)}
    blocks = []
    for p in parsed:
        k = p["key"]
        eb = existing.get(k) or {}
        blocks.append({
            "key": k,
            "label": eb.get("label") or k,
            "default": str(eb.get("default") or ""),
            "value": str(eb.get("value") or ""),
        })
    # 适用场景标签：只允许白名单值；空 = 通用（所有工作场景可选）
    raw_scopes = data.get("scopes") or []
    if isinstance(raw_scopes, str):
        raw_scopes = [raw_scopes]
    scopes = []
    for s in raw_scopes:
        s = str(s).strip()
        if s in SCOPES and s not in scopes:
            scopes.append(s)
    return {
        "name": (data.get("name") or "").strip() or "未命名提示词模板",
        "scene": (data.get("scene") or "").strip(),
        "role": (data.get("role") or "").strip(),
        "body": body,
        "blocks": blocks,
        "scopes": scopes,
    }


def build_prompt(rec: dict, drop_unfilled: bool = False) -> str:
    """把模板正文里的 {} 块替换为填充值（value 优先，缺省用 default），返回最终给 AI 的提示词。

    drop_unfilled=False（编辑器预览）：未填充块保留 {} 标记，提示用户仍未填。
    drop_unfilled=True（工作场景真正发给 AI）：未填充块直接移除，
    确保 {} 标记本身绝不进入 AI 请求。
    """
    if not isinstance(rec, dict):
        return ""

    def repl(m: "re.Match") -> str:
        key = m.group(1)
        b = None
        for x in (rec.get("blocks") or []):
            if isinstance(x, dict) and x.get("key") == key:
                b = x
                break
        if b is None:
            return "" if drop_unfilled else m.group(0)
        val = (b.get("value") or b.get("default") or "").strip()
        if val:
            return val
        return "" if drop_unfilled else m.group(0)

    return _BLOCK_RE.sub(repl, rec.get("body") or "")


def render_by_id(tid: str, overrides: dict = None, drop_unfilled: bool = True) -> dict:
    """按模板中心里的已存模板渲染最终提示词（工作场景点选导入用）。

    overrides: {块key: 本次填充值}，只覆盖 value，不改模板落盘内容；
    这样「临时创建/修改」的填充值属于本次使用，模板文件仍由模板中心统一保管。
    """
    rec = get_template(tid)
    if not rec:
        return {"ok": False, "error": "模板不存在或已删除"}
    merged = json.loads(json.dumps(rec))
    ov = overrides or {}
    for b in merged.get("blocks") or []:
        k = b.get("key")
        if k in ov:
            b["value"] = str(ov.get(k) or "")
    return {
        "ok": True,
        "name": merged.get("name", ""),
        "scene": merged.get("scene", ""),
        "prompt": build_prompt(merged, drop_unfilled=drop_unfilled),
    }


def list_templates() -> list:
    out = []
    for f in sorted(PTPL_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def get_template(tid: str):
    p = PTPL_DIR / f"{tid}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_template(data: dict, meta: dict = None) -> dict:
    """保存 AI 提示词模板。归属字段由服务端 meta 注入（同文字 / 视频模板）。"""
    tid = data.get("id") or ("ptpl_" + uuid.uuid4().hex[:8])
    existing = get_template(tid) if data.get("id") else None
    rec = normalize(data)
    rec["id"] = tid
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
    (PTPL_DIR / f"{tid}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def delete_template(tid: str) -> bool:
    p = PTPL_DIR / f"{tid}.json"
    if p.exists():
        p.unlink()
        return True
    return False
