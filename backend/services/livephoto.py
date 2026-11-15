# -*- coding: utf-8 -*-
"""Live 图提取：从各种「实况 / 动态照片」形态中提取静态帧并转为常规图片格式。

支持的输入形态（自动识别，无需用户区分）：
1. 安卓动态照片（Motion Photo）：单张 JPG 内嵌 MP4 视频（XMP 标记偏移），
   提取静态帧 = 剥离内嵌视频后的 JPEG 本体；
2. iPhone Live 图配对：同名「图片(jpg/heic/png...) + 视频(.mov)」两个文件
   （IMG_1234.HEIC + IMG_1234.MOV），静态帧即图片本身，丢弃视频部分；
3. 仅视频文件（.mov / .mp4 / .webm / .mkv）：用 ffmpeg 提取首帧作为静态帧
   （Apple Live 图的 .mov 首帧与其静态照片一致）。
4. 小米 LIVP 实况：.livp 单文件（本质为 ZIP 容器，内含 image.xxx 静态帧 + video.mp4），
   解包后取出内部静态帧图片即可（HEIC 静态帧仍需 pillow-heif 解码）。

输出：png / jpg / bmp；单图大小默认不超过 3MB（可调）：
- jpg：逐步降质量再降分辨率；
- png：逐步降分辨率；
- bmp：无压缩，超限时按字节预算（W*H*3）自动降采样，保证仍满足上限。

HEIC / HEIF 依赖 pillow-heif（缺失时自动降级为报错提示，不影响其余格式）。
"""
import io
import os
import re
import subprocess
import tempfile
import zipfile
from typing import List, Optional, Tuple

from PIL import Image, ImageOps

from . import image as image_svc
from .video import get_ffmpeg

# HEIC 解码：可选依赖，缺失不影响 jpg/png/bmp
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:
    HEIC_OK = False

IMG_EXTS = ("jpg", "jpeg", "png", "webp", "bmp", "gif", "tif", "tiff",
            "heic", "heif", "avif", "livp")
VID_EXTS = ("mov", "mp4", "webm", "mkv", "avi", "m4v", "flv")

_OUT_SUFFIX = {"png": ".png", "jpg": ".jpg", "bmp": ".bmp"}

MP4_BOX_TYPES = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"styp")


def ext_of(name: str) -> str:
    return (name.rsplit(".", 1)[-1].lower() if "." in name else "").strip()


def is_image_name(name: str) -> bool:
    return ext_of(name) in IMG_EXTS


def is_video_name(name: str) -> bool:
    return ext_of(name) in VID_EXTS


# ---------------- 动态照片（Motion Photo）内嵌视频检测 ----------------

def _is_mp4_at(data: bytes, off: int) -> bool:
    """检查 data[off:] 处是否是一个合法的 MP4 box 起点（size + 类型）。"""
    if off < 0 or off + 8 > len(data):
        return False
    size = int.from_bytes(data[off:off + 4], "big")
    box = data[off + 4:off + 8]
    if box == b"ftyp":
        # ftyp 的 major brand 必须是可打印字符（如 isom / mp42 / heic）
        brand = data[off + 8:off + 12]
        return bool(re.match(rb"^[ -~]{2,4}$", brand))
    return size >= 8 and off + size <= len(data) + 8 and box in MP4_BOX_TYPES


def find_embedded_video_offset(data: bytes) -> int:
    """在动态照片（单 JPG 内嵌 MP4）里定位内嵌视频起始偏移；未找到返回 0。

    检测顺序：
    1) XMP `GCamera:MicroVideoOffset`（老版 Google 语义：值为从文件末尾起的字节数）；
    2) XMP `Camera:MicroVideoOffset`（部分厂商变体）；
    3) 兜底：全文搜索首个合法 `ftyp` box（内嵌视频总是追加在 JPEG 数据之后）。
    所有候选都用 _is_mp4_at 校验，避免误切正常图片。
    """
    head = data[:262144]  # XMP 段在文件头部，无需全文扫
    for pat in (rb'GCamera:MicroVideoOffset="(\d+)"',
                rb'Camera:MicroVideoOffset="(\d+)"',
                rb'Samsung:TrailerSize="(\d+)"'):
        m = re.search(pat, head)
        if m:
            val = int(m.group(1))
            off = len(data) - val
            if _is_mp4_at(data, off):
                return max(0, off)
    # 兜底：首个合法 ftyp（跳过前 64 字节避免与 JPEG 头部混淆）
    pos = data.find(b"ftyp", 8)
    while pos != -1:
        start = pos - 4
        if _is_mp4_at(data, start):
            # 确认它位于 JPEG EOI（FFD9）之后 —— 正常 JPEG 图片本体的结束点
            eoi = data.rfind(b"\xff\xd9", 0, start)
            if eoi != -1:
                return start
        pos = data.find(b"ftyp", pos + 1)
    return 0


# ---------------- 素材分组：按同名 stem 组成「Live 图单元」 ----------------

def plan_units(items: List[Tuple[str, bytes]]) -> Tuple[List[dict], List[str]]:
    """把上传的文件按文件名主干配对成处理单元。

    单元 = {stem, image:(name,bytes)|None, video:(name,bytes)|None}
    - 图片 + 同名视频 → 配对单元（用图片，忽略视频）；
    - 仅图片 → 图片单元（可能是动态照片单文件，处理时再检测）；
    - 仅视频 → 视频单元（提帧）；
    - 同主干多图片时取首个，多余记录 ignored。
    """
    units = {}
    order = []
    ignored = []
    for name, data in items:
        if not is_image_name(name) and not is_video_name(name):
            ignored.append(name)
            continue
        stem = os.path.splitext(os.path.basename(name or ""))[0] or "live"
        key = stem.lower()
        if key not in units:
            units[key] = {"stem": stem, "image": None, "video": None}
            order.append(key)
        u = units[key]
        if is_image_name(name):
            if u["image"] is None:
                u["image"] = (name, data)
            else:
                ignored.append(name)
        else:
            if u["video"] is None:
                u["video"] = (name, data)
            else:
                ignored.append(name)
    return [units[k] for k in order], ignored


# ---------------- 小米 LIVP 解包（ZIP 容器） ----------------

def open_livp_still(data: bytes) -> Tuple[bytes, str]:
    """解包小米 .livp 实况照片，返回 (内部静态帧图片字节, 内部文件名)。

    .livp 本质是一个标准 ZIP 容器，把「静态帧图片(image.jpg / image.heic)
    + 视频(video.mp4)」打包在一起。Live 图提取只需取出其中的静态帧图片即可。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        raise RuntimeError(f"LIVP 解析失败（不是合法的 ZIP 容器）：{e}")
    names = zf.namelist()
    # 优先取名为 image* 的图片成员（小米标准命名）
    member = None
    for n in names:
        b = os.path.basename(n)
        if b.lower().startswith("image") and ext_of(b) in IMG_EXTS:
            member = n
            break
    # 退而求其次：任意图片成员（兼容非标准命名）
    if member is None:
        for n in names:
            if ext_of(n) in IMG_EXTS:
                member = n
                break
    if member is None:
        zf.close()
        raise RuntimeError("LIVP 容器内未找到静态帧图片（image.jpg / image.heic 等）")
    inner = zf.read(member)
    zf.close()
    return inner, member


# ---------------- 静态帧提取 ----------------

def _open_image_bytes(data: bytes, name: str) -> Image.Image:
    ext = ext_of(name)
    if ext in ("heic", "heif") and not HEIC_OK:
        raise RuntimeError(
            "当前环境缺少 HEIC 解码组件（pillow-heif），无法读取 HEIC 静态帧"
            "（iPhone Live 图 / 小米 LIVP 等）；请改传 JPG 静态帧或安装 pillow-heif。")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise RuntimeError(f"图片解析失败（{name}）：{e}")
    return ImageOps.exif_transpose(img).convert("RGB")


def _extract_frame_from_video(data: bytes, name: str) -> Image.Image:
    """用 ffmpeg 提取视频首帧（Apple Live 图 .mov 的首帧 = 其静态照片）。"""
    ff = get_ffmpeg()
    ext = ext_of(name) or "mov"
    with tempfile.TemporaryDirectory(prefix="live_") as td:
        src = os.path.join(td, f"src.{ext}")
        out = os.path.join(td, "frame.png")
        with open(src, "wb") as f:
            f.write(data)
        p = subprocess.run(
            [ff, "-y", "-i", src, "-frames:v", "1", "-q:v", "2", out],
            capture_output=True, timeout=120)
        if p.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(f"视频提帧失败（{name}）")
        return _open_image_bytes(open(out, "rb").read(), "frame.png")


def extract_still(unit: dict) -> Tuple[Image.Image, str]:
    """从 Live 图单元提取静态帧。返回 (PIL图像, 来源标签)。

    来源标签：motion=动态照片单文件（已剥离内嵌视频）、pair=iPhone配对、
    image=图片（静态帧即图片）、frame=视频关键帧。
    """
    img_item = unit.get("image")
    vid_item = unit.get("video")
    if img_item is not None:
        name, data = img_item
        ext = ext_of(name)
        if ext == "livp":
            # 小米实况照片：ZIP 容器，取出内部静态帧图片即可
            still, inner_name = open_livp_still(data)
            return _open_image_bytes(still, inner_name), "livp"
        if is_video_name(name):
            pass  # 不会发生，防御
        # 动态照片单文件：先检测内嵌视频
        off = find_embedded_video_offset(data) if ext in ("jpg", "jpeg") else 0
        if off > 0:
            still = data[:off]
            # 静态帧 = 剥离视频后的 JPEG 本体（以 EOI 收尾，保证可解码）
            eoi = still.rfind(b"\xff\xd9")
            if eoi != -1:
                still = still[:eoi + 2]
            return _open_image_bytes(still, name), "motion"
        if unit.get("video") is not None:
            return _open_image_bytes(data, name), "pair"
        return _open_image_bytes(data, name), "image"
    if vid_item is not None:
        name, data = vid_item
        return _extract_frame_from_video(data, name), "frame"
    raise RuntimeError("空的处理单元")


# ---------------- 输出（格式 + 大小约束） ----------------

def save_still(img: Image.Image, stem: str, cfg: dict, out_dir: str) -> Tuple[str, int, str]:
    """保存静态帧：png/jpg/bmp + 大小上限（默认 3MB）。

    返回 (输出文件名, 字节数, 实际格式)。
    - jpg：降质量→降分辨率；png：降分辨率（复用 image.save_with_constraint）；
    - bmp：无压缩（W*H*3 字节），超上限时按字节预算自动降采样，仍保证 ≤ 上限。
    """
    lc = (cfg or {}).get("live", {}) or {}
    fmt = str(lc.get("format", "jpg") or "jpg").lower()
    if fmt not in ("png", "jpg", "bmp"):
        fmt = "jpg"
    max_mb = float(lc.get("max_size_mb", 3) or 3)
    max_mb = max(0.1, min(200.0, max_mb))
    max_bytes = int(max_mb * 1024 * 1024)
    quality = int(lc.get("quality", 90) or 90)

    suffix = _OUT_SUFFIX[fmt]
    out_name = f"{stem}_live{suffix}"
    path = os.path.join(out_dir, out_name)

    if fmt == "bmp":
        cur = img
        while True:
            buf = io.BytesIO()
            cur.save(buf, format="BMP")
            if buf.tell() <= max_bytes or cur.width <= 64:
                with open(path, "wb") as f:
                    f.write(buf.getvalue())
                return out_name, buf.tell(), fmt
            # BMP 字节数 ≈ W*H*3：按预算比例缩放（留 5% 余量给文件头）
            budget = max_bytes * 0.95
            scale = (budget / max(1, buf.tell())) ** 0.5
            cur = cur.resize((max(1, int(cur.width * scale)),
                              max(1, int(cur.height * scale))), Image.LANCZOS)
    image_svc.save_with_constraint(img, fmt, quality, max_bytes, path)
    return out_name, os.path.getsize(path), fmt


# ---------------- 单元处理入口 ----------------

def process_unit(unit: dict, cfg: dict, out_dir: str) -> dict:
    """处理单个 Live 图单元 → {ok, output, source, bytes, format} 或 {ok:False, error}。"""
    try:
        img, source = extract_still(unit)
        out_name, nbytes, fmt = save_still(img, unit["stem"], cfg, out_dir)
        return {"ok": True, "output": out_name, "source": source,
                "bytes": nbytes, "format": fmt}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def build_live_label(cfg: dict, n_units: int, n_ignored: int) -> str:
    lc = (cfg or {}).get("live", {}) or {}
    fmt = str(lc.get("format", "jpg") or "jpg").upper()
    mb = lc.get("max_size_mb", 3)
    parts = [f"Live提取{fmt}", f"≤{mb}M"]
    if n_units:
        parts.append(f"{n_units}张")
    if n_ignored:
        parts.append(f"忽略{n_ignored}")
    return "·".join(parts)
