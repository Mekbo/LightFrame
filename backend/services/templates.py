"""文字模板存储与规范化。

模板以 JSON 文件存于 backend/templates/<id>.json。
注意：模板只保存「样式配置」，不保存任何预览底图（预览图仅用于创建时预览，不入库）。
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

TPL_DIR = Path(__file__).resolve().parent.parent / "templates"
TPL_DIR.mkdir(parents=True, exist_ok=True)


# 各规格的典型参考高度：用户输入的描边/阴影 px 会被换算成「相对比例」，
# 渲染时再乘以实际图片高度，从而保证同规格不同尺寸下效果一致，
# 且用户在参考高度下看到的 px 与填写值接近。
_REF_HEIGHT = {"9:16": 1600, "16:9": 1080}


def _ratio_from_px(width_px: float, spec: str) -> float:
    ref = _REF_HEIGHT.get(spec, 1280)
    return max(0.0, float(width_px)) / ref


def _default_style() -> dict:
    return {
        "spec": "9:16",
        "font": "思源黑体",
        "color": "#FFFFFF",
        "weight": 400,
        "stroke": {"width": 0, "color": "#000000"},   # 默认 0：无描边
        "shadow": {"width": 0, "color": "#000000", "blur": 0},  # 默认 0：无阴影
        "opacity": 1.0,           # 字体透明度（0~1），禁止字体背景
        "bg": False,              # 始终 False：不允许字体背景
        "pos": {"x": 0.5, "y": 0.86},   # 归一化坐标（相对宽/高），跨尺寸一致
        "anchor": "center",       # left / center / right
        "size_ratio": 0.06,       # 字号 = 图片高度 * 此比例
        "stroke_ratio": 0.0,      # 描边宽 = 图片高度 * 此比例（width=0 时禁用）
        "shadow_ratio": 0.0,      # 阴影偏移/模糊 = 图片高度 * 此比例（width=0 时禁用）
        "max_width_ratio": 0.9,   # 文字最大宽度比例，超出自动换行
        "line_spacing": 1.2,
    }


def normalize_style(data: dict) -> dict:
    """把用户输入的样式补全为合法模板样式（含跨尺寸的相对参数）。"""
    s = _default_style()
    if not isinstance(data, dict):
        data = {}
    for k in ("spec", "font", "color", "weight", "opacity", "anchor",
             "size_ratio",
             "max_width_ratio", "line_spacing"):
        if data.get(k) is not None:
            s[k] = data[k]
    s["spec"] = s["spec"] if s["spec"] in ("9:16", "16:9") else "9:16"
    s["weight"] = int(s["weight"]) if str(s["weight"]).isdigit() else 400
    s["opacity"] = max(0.1, min(1.0, float(s["opacity"])))
    s["bg"] = False  # 强制禁止字体背景
    # 位置
    pos = data.get("pos") or {}
    s["pos"] = {
        "x": max(0.0, min(1.0, float(pos.get("x", s["pos"]["x"])))),
        "y": max(0.0, min(1.0, float(pos.get("y", s["pos"]["y"])))),
    }
    # 描边：width<=0 表示禁用；否则按参考高度换算成相对比例（跨尺寸一致）
    st = data.get("stroke") or {}
    sw = int(st.get("width", s["stroke"]["width"]))
    if sw <= 0:
        s["stroke"] = {"width": 0}
    else:
        s["stroke"] = {"width": sw, "color": st.get("color", s["stroke"]["color"])}
        s["stroke_ratio"] = _ratio_from_px(sw, s["spec"])
    # 阴影：width<=0 表示禁用；否则按参考高度换算成相对比例
    sh = data.get("shadow") or {}
    shw = int(sh.get("width", s["shadow"]["width"]))
    if shw <= 0:
        s["shadow"] = {"width": 0}
    else:
        s["shadow"] = {
            "width": shw,
            "color": sh.get("color", s["shadow"]["color"]),
            "blur": max(1, int(sh.get("blur", shw * 1.5))),
        }
        s["shadow_ratio"] = _ratio_from_px(shw, s["spec"])
    return s


def list_templates() -> list:
    out = []
    for f in sorted(TPL_DIR.glob("*.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def get_template(tid: str) -> dict:
    p = TPL_DIR / f"{tid}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_template(data: dict, meta: dict = None) -> dict:
    """保存模板。归属字段由服务端 meta 注入，绝不信任前端 payload。

    meta: {owner, owner_name, shared}；编辑既有模板时保留原 owner/owner_name/created_at。
    """
    tid = data.get("id") or ("tpl_" + uuid.uuid4().hex[:8])
    existing = get_template(tid) if data.get("id") else None
    rec = dict(data)
    rec["id"] = tid
    rec["style"] = normalize_style(data.get("style") or data)
    rec.pop("preview_image", None)          # 绝不保存预览底图
    for k in ("owner", "owner_name", "shared", "created_at",
              "can_edit", "readonly", "is_mine"):
        rec.pop(k, None)                    # 防前端伪造归属 / 权限标记
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
    (TPL_DIR / f"{tid}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def delete_template(tid: str) -> bool:
    p = TPL_DIR / f"{tid}.json"
    if p.exists():
        p.unlink()
        return True
    return False
