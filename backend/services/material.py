# -*- coding: utf-8 -*-
"""文案生产：选题库 / 脚本 / 配音 / 封面 / 素材包 的存储与生成。

背景（功能及流程优化 v2）：视频合成中把 ②~④ 物料生成（脚本 / 配音 / 封面）拆出来
独立生产，避免 AI 调用失败拖垮整条视频任务；全部物料备齐后打包成「素材包」，
在视频创作工作区一键导入。

数据按用户隔离，全部落在 BASE_DIR/data/materials/<uid>/：
  topics.json      选题库 [{id, name, prompt, created_at, topics:[{id, text, used}]}]
  scripts.json     脚本 [{id, topic_id, topic_text, subtitle, title, cover_copy,
                          created_at, voice:{file, model_id, voice_id, error, generated_at}}]
  covers_src.json  封面底图清单 [{id, file, name, size, created_at}]
  covers_out.json  生成封面清单 [{id, file, copy, script_id, src_id, template_id, created_at}]
  projects.json    素材包 [{id, name, created_at, script_ids, cover_ids, template_id, note}]
  voices/          配音音频（scripts.json 的 voice.file 指向这里）
  covers_src/      封面底图文件（用户上传）
  covers_out/      批量生成的封面文件
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image

from ..core.config import BASE_DIR, sanitize_uid

MAT_ROOT = BASE_DIR / "data" / "materials"
VOICE_SUBDIR = "voices"
COVER_SRC_SUBDIR = "covers_src"
COVER_OUT_SUBDIR = "covers_out"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class MaterialError(Exception):
    """面向用户的业务错误（message 直接展示给前端）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ------------------------------ 基础读写 ------------------------------

def mat_root(uid: str) -> Path:
    d = MAT_ROOT / sanitize_uid(uid or "")
    for sub in (VOICE_SUBDIR, COVER_SRC_SUBDIR, COVER_OUT_SUBDIR):
        try:
            (d / sub).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return d


def _load(uid: str, name: str, default):
    p = mat_root(uid) / name
    try:
        if p.exists():
            v = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(v, type(default)):
                return v
    except Exception:
        pass
    return default


def _save(uid: str, name: str, data) -> None:
    try:
        p = mat_root(uid) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _ai():
    """延迟导入 AI 网关，避免循环依赖。"""
    from ..api.ai import ai_service, fallback_model_id
    return ai_service, fallback_model_id


def _features() -> dict:
    try:
        from ..core.config import load_config
        return (load_config().get("ai", {}) or {}).get("features", {}) or {}
    except Exception:
        return {}


def _err_of(res: dict) -> str:
    return ((res or {}).get("error") or {}).get("message") or "未知错误"


def _pick_model(user: dict, model_id: str, capability: str) -> str:
    ai_service, fallback_model_id = _ai()
    mid, err = fallback_model_id(user, (model_id or "").strip(), capability)
    if err:
        raise MaterialError(_err_of(err))
    return mid


def _parse_lines(text: str) -> List[str]:
    """把 AI 返回的多行文本解析为「一行一条」清单（去序号、去空、去重）。"""
    seen, out = set(), []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^\d+\s*[.、）)]\s*", "", s)
        s = re.sub(r"^[-*·•]\s*", "", s)
        s = s.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# ------------------------------ ① 选题库 ------------------------------

def list_libraries(uid: str) -> list:
    return _load(uid, "topics.json", [])


def _save_libraries(uid: str, libs: list) -> None:
    _save(uid, "topics.json", libs)


def generate_library(user: dict, prompt: str, count: int,
                     model_id: str = "") -> dict:
    """①选题搭建：一次 AI 调用生成 count 条选题，保存为新选题库。"""
    uid = user.get("id", "")
    prompt = (prompt or "").strip()
    if not prompt:
        raise MaterialError("请先输入选题提示词")
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 10
    count = max(1, min(50, count))
    if _features().get("video_script") is False:
        raise MaterialError("管理员已关闭「视频脚本」AI 功能，选题生成暂不可用")
    mid = _pick_model(user, model_id, "text")
    ai_service, _ = _ai()
    system = ("你是短视频选题策划师。根据用户要求输出视频选题清单，"
              "每行一个选题，不要序号、不要引号、不要解释文字。"
              "选题要具体、有钩子、可直接拍成一条短视频。")
    res = ai_service.generate_text(user, f"{prompt}\n\n请输出 {count} 个选题。",
                                   mid, system=system, feature="video_script")
    if not res.get("ok"):
        raise MaterialError("选题生成失败：" + _err_of(res))
    topics = _parse_lines(res.get("data", {}).get("text") or "")
    if not topics:
        raise MaterialError("AI 未返回有效选题，请调整提示词后重试")
    lib = {"id": _id("lib"), "name": prompt[:24], "prompt": prompt,
           "created_at": _now(),
           "topics": [{"id": _id("tp"), "text": t, "used": False}
                      for t in topics]}
    libs = list_libraries(uid)
    libs.insert(0, lib)
    _save_libraries(uid, libs)
    return lib


def reset_library_used(uid: str, library_id: str) -> None:
    libs = list_libraries(uid)
    lib = next((l for l in libs if l.get("id") == library_id), None)
    if not lib:
        raise MaterialError("选题库不存在")
    for t in lib.get("topics", []):
        t["used"] = False
    _save_libraries(uid, libs)


def delete_library(uid: str, library_id: str) -> None:
    libs = [l for l in list_libraries(uid) if l.get("id") != library_id]
    _save_libraries(uid, libs)


# ------------------------------ ② 脚本 ------------------------------

_SCRIPT_SYS = (
    "你是短视频脚本写手。根据「选题」和用户要求，输出严格三行内容，"
    "每行用竖线分隔，不要输出其他任何内容。格式：\n"
    "脚本|一行口播文案，40~80字，口语化、有钩子、无AI味\n"
    "标题|一行吸睛标题，不超过14个字\n"
    "封面|一行封面文案，不超过12个字"
)


def _parse_script(text: str) -> Tuple[str, str, str]:
    sub = title = cover = ""
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if not sub:
            m = re.match(r"^(?:脚本|文案|口播)\s*[|｜:：]\s*(.+)$", s)
            if m:
                sub = m.group(1).strip()
                continue
        if not title:
            m = re.match(r"^标题\s*[|｜:：]\s*(.+)$", s)
            if m:
                title = m.group(1).strip()
                continue
        if not cover:
            m = re.match(r"^封\s*面(?:文\s*案)?\s*[|｜:：]\s*(.+)$", s)
            if m:
                cover = m.group(1).strip()
                continue
    if not sub:
        lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
        sub = lines[0] if lines else ""
        sub = re.sub(r"^(?:脚本|文案|口播)\s*[|｜:：]\s*", "", sub)
    sub = sub.strip("“”\"'").strip()
    title = (title or sub[:14]).strip("“”\"'。！！？!?")
    cover = (cover or sub[:12]).strip("“”\"'。！？!?")
    return sub, title, cover


def list_scripts(uid: str) -> list:
    return _load(uid, "scripts.json", [])


def _save_scripts(uid: str, scripts: list) -> None:
    _save(uid, "scripts.json", scripts)


def generate_scripts(user: dict, library_id: str, topic_ids: list,
                     prompt: str, model_id: str = "") -> list:
    """②脚本创作：对「用户勾选」的选题逐条生成 脚本+标题+封面文案。

    与旧逻辑（自动取未用选题）不同，这里完全由前端勾选决定要生成哪些选题，
    避免「全部默认全选」。成功后把对应选题标记为已用；失败的选题保持未用，可重试。
    返回新建脚本列表（可能少于请求数：已用选题会被跳过）。
    """
    uid = user.get("id", "")
    if _features().get("video_script") is False:
        raise MaterialError("管理员已关闭「视频脚本」AI 功能，脚本生成暂不可用")
    libs = list_libraries(uid)
    lib = next((l for l in libs if l.get("id") == library_id), None)
    if not lib:
        raise MaterialError("选题库不存在（可能已被删除）")
    topic_ids = [str(x) for x in (topic_ids or [])]
    if not topic_ids:
        raise MaterialError("请先在选题清单中勾选要生成脚本的选题")
    topics = [t for t in lib.get("topics", []) if t.get("id") in topic_ids]
    if not topics:
        raise MaterialError("勾选的选题均不存在于该选题库")
    mid = _pick_model(user, model_id, "text")
    ai_service, _ = _ai()
    created = []
    skipped = 0
    for topic in topics:
        if topic.get("used"):
            skipped += 1
            continue
        req = f"选题：{topic['text']}"
        p = (prompt or "").strip()
        if p:
            req += f"\n用户要求：{p}"
        res = ai_service.generate_text(user, req, mid, system=_SCRIPT_SYS,
                                       feature="video_script")
        if not res.get("ok"):
            raise MaterialError("脚本生成失败：" + _err_of(res))
        sub, title, cover = _parse_script(res.get("data", {}).get("text") or "")
        if not sub:
            raise MaterialError("AI 返回的脚本为空，请重试")
        script = {"id": _id("sc"), "topic_id": topic["id"],
                  "topic_text": topic["text"], "subtitle": sub, "title": title,
                  "cover_copy": cover, "created_at": _now(),
                  "voice": {"file": "", "model_id": "", "voice_id": "",
                            "error": "", "generated_at": ""}}
        scripts = list_scripts(uid)
        scripts.append(script)
        _save_scripts(uid, scripts)
        topic["used"] = True
        _save_libraries(uid, libs)
        created.append(script)
    if not created and skipped:
        raise MaterialError("勾选的选题已全部用过（可点「重置已用」后重新生成）")
    return created


def update_script(uid: str, script_id: str, patch: dict) -> dict:
    scripts = list_scripts(uid)
    s = next((x for x in scripts if x.get("id") == script_id), None)
    if not s:
        raise MaterialError("脚本不存在")
    for k in ("subtitle", "title", "cover_copy"):
        if k in (patch or {}) and (patch.get(k) or "") != s.get(k, ""):
            s[k] = str(patch.get(k) or "").strip()
            # 脚本文本变了，旧配音可能与新文案不一致
            if k == "subtitle" and (s.get("voice") or {}).get("file"):
                s["voice"]["stale"] = True
    _save_scripts(uid, scripts)
    return s


def delete_script(uid: str, script_id: str) -> None:
    scripts = list_scripts(uid)
    s = next((x for x in scripts if x.get("id") == script_id), None)
    if not s:
        return
    vf = (s.get("voice") or {}).get("file")
    if vf:
        try:
            (mat_root(uid) / VOICE_SUBDIR / vf).unlink()
        except Exception:
            pass
    _save_scripts(uid, [x for x in scripts if x.get("id") != script_id])


def delete_scripts(uid: str, script_ids) -> int:
    """②批量删除脚本及其关联配音文件，返回实际删除条数。"""
    scripts = list_scripts(uid)
    ids = set(script_ids or [])
    if not ids:
        return 0
    kept, removed = [], 0
    for s in scripts:
        if s.get("id") in ids:
            vf = (s.get("voice") or {}).get("file")
            if vf:
                try:
                    (mat_root(uid) / VOICE_SUBDIR / vf).unlink()
                except Exception:
                    pass
            removed += 1
        else:
            kept.append(s)
    if removed:
        _save_scripts(uid, kept)
    return removed


# ------------------------------ ③ 配音 ------------------------------

def generate_voice(user: dict, script_id: str, model_id: str = "",
                   voice_id=None) -> dict:
    """③配音生成：把脚本口播文案合成为 mp3，保存并挂到脚本记录上。"""
    uid = user.get("id", "")
    if _features().get("video_tts") is False:
        raise MaterialError("管理员已关闭「视频配音」AI 功能，配音生成暂不可用")
    scripts = list_scripts(uid)
    s = next((x for x in scripts if x.get("id") == script_id), None)
    if not s:
        raise MaterialError("脚本不存在")
    text = (s.get("subtitle") or "").strip()
    if not text:
        raise MaterialError("该脚本没有口播文案，无法配音")
    mid = _pick_model(user, model_id, "tts")
    ai_service, _ = _ai()
    try:
        data = ai_service.generate_tts(user, text, mid, voice_id=voice_id,
                                       feature="video_tts")
    except Exception as e:
        msg = str(getattr(e, "message", e))[:200]
        s["voice"] = {"file": "", "model_id": mid, "voice_id": voice_id or "",
                      "error": msg, "generated_at": ""}
        _save_scripts(uid, scripts)
        raise MaterialError("配音失败：" + msg)
    if not data:
        raise MaterialError("配音返回为空，请重试")
    fname = f"{s['id']}.mp3"
    (mat_root(uid) / VOICE_SUBDIR / fname).write_bytes(data)
    s["voice"] = {"file": fname, "model_id": mid,
                  "voice_id": voice_id or "", "error": "",
                  "generated_at": _now()}
    _save_scripts(uid, scripts)
    return s


# ------------------------------ ④ 封面 ------------------------------

def list_covers_src(uid: str) -> list:
    return _load(uid, "covers_src.json", [])


def list_covers_out(uid: str) -> list:
    return _load(uid, "covers_out.json", [])


def upload_cover_src(uid: str, items: List[Tuple[str, bytes]]) -> list:
    """④封面制作：保存用户上传的封面底图（仅图片）。"""
    srcs = list_covers_src(uid)
    for name, data in items or []:
        ext = Path(name or "").suffix.lower()
        if ext not in IMG_EXTS:
            continue
        fid = _id("cs")
        fname = fid + ext
        (mat_root(uid) / COVER_SRC_SUBDIR / fname).write_bytes(data)
        srcs.append({"id": fid, "file": fname, "name": name,
                     "size": len(data), "created_at": _now()})
    _save(uid, "covers_src.json", srcs)
    return srcs


def delete_cover_src(uid: str, src_id: str) -> None:
    srcs = list_covers_src(uid)
    c = next((x for x in srcs if x.get("id") == src_id), None)
    if not c:
        return
    try:
        (mat_root(uid) / COVER_SRC_SUBDIR / c["file"]).unlink()
    except Exception:
        pass
    _save(uid, "covers_src.json", [x for x in srcs if x.get("id") != src_id])


def clear_cover_src(uid: str) -> None:
    for c in list_covers_src(uid):
        p = mat_root(uid) / COVER_SRC_SUBDIR / c["file"]
        if not p.exists():
            continue
        try:
            p.unlink()
        except Exception:
            pass
    _save(uid, "covers_src.json", [])


def generate_covers(uid: str, template_id: str) -> list:
    """④封面制作：仅对「脚本 + 配音都已就绪」的脚本批量出图，保持一一对应。

    只处理带封面文案且已生成配音的脚本（与所选底图顺序循环取用）；
    草稿态（未配音）的脚本不会出封面，避免封面与成品脚本错位。
    """
    from . import image as image_svc
    from . import templates as tpl_svc
    srcs = list_covers_src(uid)
    if not srcs:
        raise MaterialError("请先上传封面底图（从文件添加或从目录导入）")
    tid = (template_id or "").strip()
    tpl = tpl_svc.get_template(tid) if tid else None
    if not tpl:
        raise MaterialError("请选择封面模板（可在「图片处理 → 图片加文字」中创建）")
    style = tpl_svc.normalize_style(tpl.get("style") or {})
    # 只取「有封面文案 且 已生成配音」的脚本（保持脚本/配音/标题/封面一一对应）
    scripts = [s for s in list_scripts(uid)
               if (s.get("cover_copy") or "").strip()
               and (s.get("voice") or {}).get("file")]
    if not scripts:
        raise MaterialError("暂无「已配音」的脚本：请先在「②脚本创作」生成脚本、"
                            "「③配音生成」完成配音后再制作封面")
    out_dir = mat_root(uid) / COVER_OUT_SUBDIR
    # 重新生成 = 整体对齐：先清掉旧的生成封面
    for old in list_covers_out(uid):
        try:
            (out_dir / old["file"]).unlink()
        except Exception:
            pass
    outs = []
    for i, s in enumerate(scripts):
        src = srcs[i % len(srcs)]
        img = Image.open(mat_root(uid) / COVER_SRC_SUBDIR / src["file"])
        img = img.convert("RGB")
        img = image_svc.render_text_on_image(img, style, s["cover_copy"])
        fname = f"cover_{i + 1:02d}_{s['id']}.jpg"
        img.save(out_dir / fname, format="JPEG", quality=92)
        outs.append({"id": _id("cv"), "file": fname, "copy": s["cover_copy"],
                     "script_id": s["id"], "src_id": src["id"],
                     "template_id": tid, "created_at": _now()})
    _save(uid, "covers_out.json", outs)
    return outs


def delete_cover_out(uid: str, cover_id: str) -> None:
    outs = list_covers_out(uid)
    c = next((x for x in outs if x.get("id") == cover_id), None)
    if not c:
        return
    try:
        (mat_root(uid) / COVER_OUT_SUBDIR / c["file"]).unlink()
    except Exception:
        pass
    _save(uid, "covers_out.json", [x for x in outs if x.get("id") != cover_id])


def clear_cover_out(uid: str) -> None:
    for c in list_covers_out(uid):
        try:
            (mat_root(uid) / COVER_OUT_SUBDIR / c["file"]).unlink()
        except Exception:
            pass
    _save(uid, "covers_out.json", [])


# ------------------------------ ⑤ 素材包 ------------------------------

def list_projects(uid: str) -> list:
    return _load(uid, "projects.json", [])


def create_project(uid: str, payload: dict) -> dict:
    """⑤打包文案：把「封面 / 配音 / 字幕（脚本）/ 顶部标题」一一对齐保存为配置。"""
    p = payload or {}
    name = (p.get("name") or "").strip()
    if not name:
        name = "素材包 " + _now()[:16].replace("T", " ")
    ids = set(p.get("script_ids") or [])
    scripts = list_scripts(uid)
    chosen = [s for s in scripts if s.get("id") in ids]
    if not chosen:
        raise MaterialError("请选择至少一条脚本进入素材包")
    chosen_ids = {s["id"] for s in chosen}
    use_covers = bool(p.get("use_covers", True))
    # 封面必须与本次勾选的脚本一一对齐：只取 script_id 属于勾选脚本的封面，
    # 并按脚本顺序排列，绝不把未勾选脚本的封面打包进来（旧实现取了全量）。
    cover_by_script = {}
    for c in list_covers_out(uid):
        sid = c.get("script_id")
        if sid and sid not in cover_by_script:
            cover_by_script[sid] = c["id"]
    cover_ids = ([cover_by_script[s["id"]] for s in chosen
                  if s["id"] in cover_by_script]
                 if use_covers else [])
    proj = {"id": _id("pj"), "name": name, "created_at": _now(),
            "script_ids": [s["id"] for s in chosen],
            "cover_ids": cover_ids,
            "template_id": (p.get("template_id") or "").strip(),
            "note": (p.get("note") or "").strip(),
            "voice_ready": sum(1 for s in chosen
                               if (s.get("voice") or {}).get("file")),
            "cover_ready": len(cover_ids)}
    projs = list_projects(uid)
    projs.insert(0, proj)
    _save(uid, "projects.json", projs)
    return proj


def project_detail(uid: str, pid: str) -> dict:
    """素材包详情：脚本（字幕/标题/配音地址）+ 封面地址，供视频创作一键导入。"""
    proj = next((p for p in list_projects(uid) if p.get("id") == pid), None)
    if not proj:
        raise MaterialError("素材包不存在（可能已被删除）")
    smap = {s["id"]: s for s in list_scripts(uid)}
    allowed = set(proj.get("cover_ids") or [])
    # 按 script_id 建立「脚本 → 封面」映射：素材包里的每条脚本只带自己的封面，
    # 不按数组下标配对，部分勾选也不会错位。兼容旧素材包（旧包 cover_ids 为全量）。
    by_script = {}
    for c in list_covers_out(uid):
        sid = c.get("script_id")
        if sid and sid not in by_script:
            by_script[sid] = c
    scripts = []
    for sid in proj.get("script_ids") or []:
        s = smap.get(sid)
        if not s:
            continue
        v = s.get("voice") or {}
        c = by_script.get(sid)
        if c and c["id"] not in allowed:
            c = None
        cover = None
        if c:
            cover = {"id": c["id"], "name": c["file"],
                     "url": f"/api/material/asset/{c['file']}",
                     "copy": c.get("copy", "")}
        scripts.append({
            "id": s["id"], "subtitle": s.get("subtitle", ""),
            "title": s.get("title", ""),
            "cover_copy": s.get("cover_copy", ""),
            "voice_url": f"/api/material/asset/{v['file']}" if v.get("file") else "",
            "voice_name": v.get("file") or "",
            "cover": cover,
        })
    out = dict(proj)
    out["scripts"] = scripts
    out["covers"] = [s["cover"] for s in scripts if s["cover"]]
    return out


def read_material_asset(uid: str, name: str):
    """按文件名读取本人物料（配音 mp3 / 封面图），返回 (文件名, 字节)。

    供视频合成端点以服务端引用的方式取用素材包物料：在线用户导入文案包后
    无需把服务端已有的文件再上传一遍（那会触发「在线用户请先上传到我的空间」拦截）。
    严格限定为纯文件名，禁止任何路径穿越。
    """
    if not name or Path(name).name != name or name in (".", ".."):
        return None
    root = mat_root(uid)
    for sub in (VOICE_SUBDIR, COVER_SRC_SUBDIR, COVER_OUT_SUBDIR):
        p = root / sub / name
        if p.is_file():
            try:
                return (p.name, p.read_bytes())
            except Exception:
                return None
    return None


def delete_project(uid: str, pid: str) -> None:
    projs = [p for p in list_projects(uid) if p.get("id") != pid]
    _save(uid, "projects.json", projs)


# ------------------------------ 汇总 ------------------------------

def snapshot(uid: str) -> dict:
    """前端一次拉全：选题库 / 脚本 / 封面底图 / 生成封面 / 素材包。"""
    return {
        "libraries": list_libraries(uid),
        "scripts": list_scripts(uid),
        "covers_src": list_covers_src(uid),
        "covers_out": list_covers_out(uid),
        "projects": list_projects(uid),
    }
