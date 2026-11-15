"""开发者微信联系方式：官方二维码图片 + 微信号两种方式。

设计（2026-11-14 精简）：
- 不再由系统生成二维码（已移除 OpenCV / qrcode / segno 生成链路，瘦身）。
- 仅保留两种联系方式，优先级从高到低：
    1. official：管理员上传微信「我的二维码」官方图片 → 原样展示，可扫码直接添加好友；
    2. text：只填微信号 → 页面展示微信号文字，访客复制后到微信「添加朋友 → 搜索」。
- 说明：微信加好友二维码解码后是微信服务器签名的名片链接，第三方无法凭微信号
  反向生成；把微信号编成普通二维码，微信「扫一扫」也只会显示一串文字，
  因此索性不生成，直接展示文字并如实标注。
- 唯一数据源：全局配置 developer_wechat + 官方二维码文件。
- 读：公开只读；写 / 删：仅管理员（/api/admin/ 前缀中间件 + 路由二次校验）。
"""
from __future__ import annotations

import base64
import io
import re

from ..core.config import CONFIG_DIR, load_config, save_config

KEY = "developer_wechat"          # 微信号 / 展示用文本
LINK_KEY = "developer_wechat_link"  # 旧版名片链接键（已废弃，仅写入空值清理残留）
OFFICIAL_QR = CONFIG_DIR / "developer_wechat_qr.png"  # 微信官方「我的二维码」图片
MAX_LEN = 300
MAX_IMAGE_BYTES = 3 * 1024 * 1024
QR_URL = "/api/public/developer-qr.png"


def _clean(value) -> str:
    """去掉首尾空白与控制字符，避免把换行 / 零宽字符存进配置。"""
    s = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\ufeff]", "", str(value or ""))
    return s.strip()


def get_wechat() -> str:
    try:
        return _clean(load_config("").get(KEY))
    except Exception:
        return ""


def has_official_qr() -> bool:
    return OFFICIAL_QR.exists() and OFFICIAL_QR.stat().st_size > 0


def qr_mode() -> str:
    """返回当前联系方式模式：official（官方二维码图） / text（仅微信号） / none。"""
    if has_official_qr():
        return "official"
    if get_wechat():
        return "text"
    return "none"


def addable() -> bool:
    """是否可扫码直接添加好友：只有官方二维码图片可以。"""
    return qr_mode() == "official"


def public_view() -> dict:
    """公开只读视图：不含任何路径、密钥、并发等内部配置。"""
    w = get_wechat()
    mode = qr_mode()
    return {
        "ok": True,
        "wechat": w,
        "has_wechat": bool(w) or mode == "official",
        "qr_url": QR_URL,
        "qr_mode": mode,
        "qr_can_add": addable(),
        "has_official": has_official_qr(),
    }


def set_wechat(value) -> dict:
    """保存微信号（管理员）；顺带清空旧版名片链接残留。"""
    w = _clean(value)
    if len(w) > MAX_LEN:
        raise ValueError("微信号不能超过 %d 个字符" % MAX_LEN)
    save_config("", {KEY: w, LINK_KEY: ""})
    return public_view()


def delete_wechat() -> dict:
    """删除微信号（管理员）；官方二维码图片保留，可在上传区单独删除。"""
    save_config("", {KEY: "", LINK_KEY: ""})
    return public_view()


def save_official_qr(data_url) -> dict:
    """保存微信官方「我的二维码」图片（管理员）。

    只接受 data:image/…;base64,…，用 Pillow 解码校验并统一重编码为 PNG，
    去掉原图 EXIF / 元数据，限定尺寸与体积，避免任意文件写入与超大图撑爆配置。
    """
    from PIL import Image

    raw = str(data_url or "")
    m = re.match(r"^data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=\s]+)$", raw)
    if not m:
        raise ValueError("请上传 PNG / JPG 格式的微信二维码图片")
    try:
        blob = base64.b64decode(m.group(1), validate=True)
    except Exception:
        raise ValueError("图片内容不是有效的 base64 编码")
    if not blob or len(blob) > MAX_IMAGE_BYTES:
        raise ValueError("图片大小不能超过 3MB")

    try:
        img = Image.open(io.BytesIO(blob))
        img.load()
    except Exception:
        raise ValueError("无法读取该图片，请重新从微信保存二维码后上传")
    if img.width < 80 or img.height < 80 or img.width > 4000 or img.height > 4000:
        raise ValueError("图片尺寸需在 80×80 到 4000×4000 之间")

    img = img.convert("RGB")
    if max(img.size) > 1200:  # 等比缩小，避免文件过大
        img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OFFICIAL_QR.write_bytes(buf.getvalue())
    return public_view()


def delete_official_qr() -> dict:
    """删除官方二维码图片（管理员）；删除后仅展示微信号文字。"""
    try:
        OFFICIAL_QR.unlink(missing_ok=True)
    except Exception:
        pass
    return public_view()


def qr_png() -> bytes:
    """返回官方二维码图片；未上传时抛 ValueError（路由层转 404）。

    系统不再生成二维码：仅有官方图片时才提供本接口。
    """
    if has_official_qr():
        return OFFICIAL_QR.read_bytes()
    raise ValueError("尚未上传开发者微信二维码")
