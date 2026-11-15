import io
import os
from pathlib import Path

from PIL import Image, ImageFilter, ImageDraw, ImageFont

from .fonts import get_font

RESIZE_ALGOS = {
    "lanczos": Image.LANCZOS,
    "bicubic": Image.BICUBIC,
    "bilinear": Image.BILINEAR,
    "nearest": Image.NEAREST,
}


def parse_ratio(s: str):
    if not s or s == "free":
        return None
    try:
        a, b = s.split(":")
        return float(a) / float(b)
    except Exception:
        return None


def crop_to_ratio(img, ratio):
    w, h = img.size
    cur = w / h
    if abs(cur - ratio) < 1e-3:
        return img
    if cur > ratio:
        new_w = int(h * ratio)
        x = (w - new_w) // 2
        return img.crop((x, 0, x + new_w, h))
    else:
        new_h = int(w / ratio)
        y = (h - new_h) // 2
        return img.crop((0, y, w, y + new_h))


def crop_to_size(img, tw, th):
    w, h = img.size
    scale = max(tw / w, th / h)
    nw, nh = int(w * scale), int(h * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    x = (nw - tw) // 2
    y = (nh - th) // 2
    return img.crop((x, y, x + tw, y + th))


def apply_resize(img, mode, value):
    """放大/缩小：按最长边、宽度或百分比。"""
    if not mode or mode == "none" or not value:
        return img
    w, h = img.size
    if mode == "longest":
        if max(w, h) == value:
            return img
        ratio = value / max(w, h)
    elif mode == "width":
        if w == value:
            return img
        ratio = value / w
    elif mode == "percent":
        ratio = value / 100.0
    else:
        return img
    nw, nh = max(1, int(w * ratio)), max(1, int(h * ratio))
    return img.resize((nw, nh), Image.LANCZOS)


def apply_watermark_cover(img):
    """简单去水印：右下角区域做高斯模糊覆盖（适用于角落 logo）。"""
    w, h = img.size
    bw = max(20, int(w * 0.25))
    bh = max(20, int(h * 0.12))
    box = (w - bw, h - bh, w, h)
    region = img.crop(box)
    region = region.filter(ImageFilter.GaussianBlur(15))
    img.paste(region, box)
    return img


def _hex_to_rgba(hex_color, alpha=255):
    h = (hex_color or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    h = (h + "000000")[:6]
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(alpha))


def _anchor_x(anchor, cx, tw):
    if anchor == "left":
        return cx
    if anchor == "right":
        return cx - tw
    return cx - tw / 2.0


def _wrap_text(text, font, max_width):
    """按最大宽度换行：中文逐字、英文按词；保留显式换行。"""
    lines = []
    for para in (text or "").split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for ch in para:
            test = cur + ch
            if font.getlength(test) <= max_width or not cur:
                cur = test
            else:
                lines.append(cur)
                cur = ch
        lines.append(cur)
    return lines


def _legacy_style(tc: dict) -> dict:
    """兼容旧版绝对像素配置，转换为相对样式。"""
    pos_map = {
        "top-left": (0.05, 0.06), "top-center": (0.5, 0.06), "top-right": (0.95, 0.06),
        "center": (0.5, 0.5), "bottom-left": (0.05, 0.92),
        "bottom-center": (0.5, 0.92), "bottom-right": (0.95, 0.92),
    }
    x, y = pos_map.get(tc.get("position", "bottom-center"), (0.5, 0.92))
    size = int(tc.get("size", 36)) or 36
    return {
        "spec": "9:16", "font": "思源黑体",
        "color": tc.get("color", "#FFFFFF"), "weight": 400,
        "stroke": {"width": 2, "color": "#000000"},
        "shadow": {"width": 2, "color": "#000000", "blur": 3},
        "opacity": 1.0, "bg": False,
        "pos": {"x": x, "y": y}, "anchor": "center",
        "size_ratio": 0.045, "stroke_ratio": 0.0025, "shadow_ratio": 0.0025,
        "max_width_ratio": 0.9, "line_spacing": 1.2,
        "_legacy_size_px": size,
    }


def _draw_text_layers(W: int, H: int, style: dict, content: str):
    """按模板样式绘制两张透明 RGBA 图层：(阴影层或 None, 主文字层)。

    图片加文字与视频硬字幕共用此逻辑，保证同一套模板在两种产物里效果完全一致。
    位置用归一化坐标（pos.x/pos.y 为宽/高比例），字号 = 高度 * size_ratio，
    描边/阴影 = 高度 * ratio（width<=0 时禁用，否则下限 1px）。不绘制字体背景。
    """
    if style.get("_legacy_size_px"):
        # 旧配置：用绝对字号，直接按高度比例换算以保持近似
        style = dict(style)
        style["size_ratio"] = max(0.02, min(0.2, style["_legacy_size_px"] / max(1, H)))
    size_ratio = float(style.get("size_ratio", 0.06))
    font_size = max(8, int(H * size_ratio))
    font = get_font(style.get("font"), font_size)

    opacity = max(0.1, min(1.0, float(style.get("opacity", 1.0))))
    anchor = style.get("anchor", "center")
    pos = style.get("pos", {"x": 0.5, "y": 0.86})
    cx = pos.get("x", 0.5) * W
    y0 = pos.get("y", 0.86) * H
    max_w = W * float(style.get("max_width_ratio", 0.9))
    line_spacing = float(style.get("line_spacing", 1.2))

    lines = _wrap_text(content, font, max_w)
    try:
        ascent, descent = font.getmetrics()
        lh = int((ascent + descent) * line_spacing)
    except Exception:
        lh = int(font_size * 1.3 * line_spacing)
    # 多行时上下行间距整体收紧 3px（字幕 / 顶部标题 / 图片加文字共用，仅多行时有效）；
    # 用字号下限兜底，避免行距过小导致文字重叠。
    lh = max(int(font_size), lh - 3)

    # 阴影层（width<=0 时禁用；否则按 ratio 换算，下限 1px 偏移/模糊）
    shadow = style.get("shadow", {}) or {}
    if int(shadow.get("width", 0)) >= 1:
        sh_off = max(1, int(H * float(style.get("shadow_ratio", 0.0025))))
        sh_blur = max(1, int(H * float(style.get("shadow_ratio", 0.0025)) * 1.5))
        shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow_layer)
        sh_color = _hex_to_rgba(shadow.get("color", "#000000"), int(255 * opacity))
        for i, line in enumerate(lines):
            tw = sd.textlength(line, font=font)
            x = _anchor_x(anchor, cx, tw)
            sd.text((x + sh_off, y0 + i * lh + sh_off), line, font=font, fill=sh_color)
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(sh_blur))
    else:
        shadow_layer = None

    # 主文字层（含描边：width<=0 时不描边；否则按 ratio 换算，下限 1px）
    main_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    md = ImageDraw.Draw(main_layer)
    text_color = _hex_to_rgba(style.get("color", "#FFFFFF"), int(255 * opacity))
    stroke = style.get("stroke", {}) or {}
    stroke_w = max(1, int(H * float(style.get("stroke_ratio", 0.0025)))) if int(stroke.get("width", 1)) >= 1 else 0
    stroke_color = _hex_to_rgba(stroke.get("color", "#000000"), int(255 * opacity))
    for i, line in enumerate(lines):
        tw = md.textlength(line, font=font)
        x = _anchor_x(anchor, cx, tw)
        md.text((x, y0 + i * lh), line, font=font, fill=text_color,
                stroke_width=stroke_w, stroke_fill=stroke_color)
    return shadow_layer, main_layer


def render_text_layer(w: int, h: int, style: dict, content: str):
    """生成一张「只有文字、其余全透明」的 RGBA 图层（视频硬字幕 / 顶部标题用）。

    视频侧把它作为 overlay 输入，按时间轴 enable 显示，即可实现与图片加文字
    完全相同的位置、字号、颜色、描边、阴影、透明度与自动换行效果。
    """
    out = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    if not content:
        return out
    shadow_layer, main_layer = _draw_text_layers(int(w), int(h), style, content)
    if shadow_layer is not None:
        out = Image.alpha_composite(out, shadow_layer)
    return Image.alpha_composite(out, main_layer)


def render_text_on_image(img, style: dict, content: str):
    """按模板样式在图片上加文字：所有尺寸均相对图片，确保同规格不同尺寸效果一致。"""
    if not content:
        return img
    base = img.convert("RGBA")
    W, H = base.size
    shadow_layer, main_layer = _draw_text_layers(W, H, style, content)
    out = base
    if shadow_layer is not None:
        out = Image.alpha_composite(out, shadow_layer)
    out = Image.alpha_composite(out, main_layer)
    return out.convert("RGB")


def save_with_constraint(img, fmt, quality, max_bytes, path):
    fmt_up = fmt.upper()
    if fmt_up == "BMP":
        img.save(path, format="BMP")
        return
    if fmt_up == "PNG":
        scale = 1.0
        cur = img
        for _ in range(20):
            buf = io.BytesIO()
            cur.save(buf, format="PNG")
            if buf.tell() <= max_bytes or scale <= 0.3:
                cur.save(path, format="PNG")
                return
            scale *= 0.9
            cur = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
        cur.save(path, format="PNG")
        return
    # JPG
    q = quality
    cur = img
    scale = 1.0
    for _ in range(25):
        buf = io.BytesIO()
        cur.save(buf, format="JPEG", quality=q)
        if buf.tell() <= max_bytes:
            cur.save(path, format="JPEG", quality=q)
            return
        if q > 30:
            q -= 10
        else:
            scale *= 0.85
            if scale < 0.3:
                cur.save(path, format="JPEG", quality=q)
                return
            cur = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)


def _out_suffix_and_fmt(conv_enabled, conv_cfg, filename):
    """根据是否启用格式转换，决定输出格式与扩展名。"""
    if conv_enabled:
        fmt = (conv_cfg.get("format") or "png").lower()
    else:
        # 未启用转换：保持原格式（仅支持 png/jpg/bmp，其余回退 png）
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
        fmt = "jpg" if ext in ("jpg", "jpeg") else (ext if ext in ("png", "bmp") else "png")
    suffix = {"png": ".png", "jpg": ".jpg", "bmp": ".bmp"}.get(fmt, ".png")
    return fmt, suffix


def process_image(data: bytes, filename: str, cfg: dict, out_dir: str, text_override=None) -> str:
    """处理单张图片：仅应用 config['image'] 中 enabled=True 的工具。
    管线顺序：缩放 → 裁切 → 去水印 → 文字 → 格式转换/保存。
    text_override：当「图片加文字」启用且传入时，用此单行文案覆盖配置的 content（用于多行循环）。
    """
    img_cfg = cfg.get("image", {})
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")

    # 1) 缩放（放大 / 缩小）
    rc = img_cfg.get("resize", {})
    if rc.get("enabled"):
        img = apply_resize(img, rc.get("mode", "none"), int(rc.get("value", 0) or 0))

    # 2) 裁切
    cc = img_cfg.get("crop", {})
    if cc.get("enabled"):
        mode = cc.get("mode", "9:16") or "9:16"
        if mode == "free":
            fw = int(cc.get("free_w", 0) or 0)
            fh = int(cc.get("free_h", 0) or 0)
            if fw and fh:
                img = crop_to_size(img, fw, fh)
        else:
            ratio = parse_ratio(mode)
            if ratio:
                img = crop_to_ratio(img, ratio)

    # 3) 去水印
    wc = img_cfg.get("watermark", {})
    if wc.get("enabled") and wc.get("mode", "cover") == "cover":
        img = apply_watermark_cover(img)

    # 4) 图片加文字（模板样式，尺寸相对以保证同规格跨尺寸一致）
    tc = img_cfg.get("text", {})
    if tc.get("enabled"):
        style = tc.get("style") or _legacy_style(tc)
        # text_override 优先（多行循环时逐图单独传），否则用配置里的 content
        content = text_override if text_override is not None else tc.get("content", "")
        img = render_text_on_image(img, style, content)

    # 5) 保存（格式转换 + 大小约束）
    conv = img_cfg.get("convert", {})
    conv_enabled = conv.get("enabled", False)
    fmt, suffix = _out_suffix_and_fmt(conv_enabled, conv, filename)

    stem = Path(filename).stem
    out_name = f"{stem}_out{suffix}"
    out_path = os.path.join(out_dir, out_name)

    if conv_enabled:
        max_mb = float(conv.get("max_size_mb", 3))
        quality = int(conv.get("quality", 90))
        max_bytes = int(max_mb * 1024 * 1024)
        save_with_constraint(img, fmt, quality, max_bytes, out_path)
    else:
        # 不转换：原格式保存，无大小约束
        pil_fmt = "JPEG" if fmt == "jpg" else fmt.upper()
        img.save(out_path, format=pil_fmt)

    return out_path
