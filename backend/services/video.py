"""视频批处理服务：把图片/视频素材合成为指定画幅、分辨率、时长、带 xfade 转场的成品视频。

设计要点（对齐需求表「视频批加工」）：
- 模式：图+图 / 视频+视频 / 图+视频（统一按素材类型逐段归一化后串联）。
- 画幅：9:16 / 16:9；仅当素材比例与画幅不一致（铺不满、会留白）时才用高斯模糊填充
        所缺画面；比例一致能铺满则直接缩放铺满，不做模糊、画面锐利。绝无黑边。
- 分辨率：默认 1080P，4K 上限 / 720P 下限，按画幅映射像素。
- 时长：默认「指定每段时长」5 秒（可手动调）；也可切换为按比例分配总时长。
- 转场：xfade（默认 fade / 0.5s）；支持多选效果，合成时每个衔接点随机取用且相邻不重复。
- 配音/字幕（脚本）：每个脚本条目 = 1 个配音音频 + 1 段字幕文本（+ 可选顶部标题），
  强制一一对应，数量不等或缺失即提示具体原因并停止；一条成品视频分配一套脚本。
  字幕按配音音频真实时长逐句烧录进画面，位置/样式由「视频模板」指定（复用图片加文字模板渲染）。
  脚本与配音统一在「文案生产」（② 脚本创作 / ③ 配音生成）预先生成，再由「一键导入工程」装载。
- 背景乐：支持批量添加，每条成品视频分配一首且互不重复；循环铺满全片、可调音量、淡入淡出，
  与配音 amix 混合。未添加配音/背景乐时成品保持无音轨。
- 原声：凡有视频素材参与，落盘后先剥离原声生成无音轨副本，再进入混剪；
  混剪阶段输出显式 -an，保证绝不带入任何原声（配音与背景乐只来自用户上传）。
- 时长匹配：按视频素材真实时长 L 与每段目标时长 D 自动决策——L≥D 从中段截取 D 秒（不循环），
  L<D 则整段直接使用、末帧定格补到 D（可切换为循环重播），既不会重复画面也严格对齐成品时长。
- 多视频混剪（图+视频 / 视频+视频 等，凡组内 ≥2 条视频素材）：**从每条视频素材各取一段**
  再混合成新视频；为保证「每段至少 5 秒」，含视频的组每段目标时长 D 会被抬到不低于
  MIN_MIX_SEGMENT=5 秒（用户设定的更短值 / 按比例分配算出的更短值均自动抬升，
  成品总时长可能因此略长于设定值），并按组内「视频素材序号」而非素材总序号分散取点，
  使 a/b/c 落在各自可用区间的不同位置。纯图片任务不受影响。
- 准入：不再设置「每组 ≥3 条素材」硬门槛 —— 产出多条视频时素材会被均分到各组，
  若仍按 3 条拦截会直接导致任务失败；现统一为组内 ≥2 条视频即各取一段，
  短于 5 秒的视频用末帧定格补足到 5 秒并在进度/meta 中提示，不阻断合成。
- 重复取不同片段（mix_mode=repeat，默认）：**同一素材可在多条成品视频里被重复取用，
  但每次取的是不同片段**（a1/a2、b1/b2、c1/c2）。一条成品视频 = 全部素材各 1 段，
  段时长由目标总时长反算（n*D-(n-1)*t=target，D≥5 秒）；不同成品视频的组合随机，
  会出现 a1+b1+c1 / a1+b2+c2 / a2+b1+c3 这类搭配。reshuffle=True 每次任务重新随机，
  False 用固定种子便于复现。mix_mode=unique 回退到旧的「素材均分、每素材一段」。
- 原创度：分母按时长；repeat 模式按「片段」判定（指纹=素材指纹+取点，a1≠a2），
  unique 模式按「素材文件」判定；已用片段/素材占比 ≥50%（fresh<50%）拦截。
- 封面：现成封面图作为「片头定格」合入视频内部（默认停留 1 秒、可调），每条视频一张、
        互不重复；不再单独导出图片文件。未提供时可按首/中/末帧抽帧作为片头。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import imageio_ffmpeg
    _HAS_IMGIO = True
except Exception:  # pragma: no cover
    _HAS_IMGIO = False

# 转场效果白名单（ffmpeg 9.0 xfade 支持的全部具名效果，custom 需表达式故不列出）
XFADE_TRANSITIONS = [
    "fade", "fadeblack", "fadewhite", "fadegrays", "fadefast", "fadeslow",
    "dissolve", "distance", "radial", "pixelize", "hblur", "zoomin",
    "wipeleft", "wiperight", "wipeup", "wipedown",
    "wipetl", "wipetr", "wipebl", "wipebr",
    "slideleft", "slideright", "slideup", "slidedown",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circlecrop", "rectcrop", "circleclose", "circleopen",
    "vertclose", "vertopen", "horzclose", "horzopen",
    "diagbl", "diagbr", "diagtl", "diagtr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "hlwind", "hrwind", "vuwind", "vdwind",
    "squeezeh", "squeezev",
    "coverleft", "coverright", "coverup", "coverdown",
    "revealleft", "revealright", "revealup", "revealdown",
]
DEFAULT_TRANSITION = "fade"

# ---------------- 混剪素材准入格式（图 + 视频，两类都收，不限制为单一类型） ----------------
# 与前端导入 / 「我的空间」file_meta 的约定保持一致：
#   图片 png/jpg/jpeg/bmp/livp/heic，视频 mp4/mov/mkv/avi/wmv/flv
# 另保留 webp/gif/tif/tiff/heif/avif/webm/m4v 等常见可用格式，避免误拒。
VIDEO_SRC_EXTS = ("mp4", "mov", "mkv", "avi", "wmv", "flv", "webm", "m4v")
IMAGE_SRC_EXTS = ("jpg", "jpeg", "png", "webp", "bmp", "gif", "tif", "tiff",
                  "heic", "heif", "avif", "livp")
# HEIC / LIVP 等 ffmpeg 无法稳定直接解码的图片：落盘前先取静态帧转成 PNG
STILL_CONVERT_EXTS = ("heic", "heif", "avif", "livp")


def ext_of_name(name: str) -> str:
    return (name.rsplit(".", 1)[-1].lower() if "." in name else "").strip()


def normalize_still_bytes(name: str, data: bytes) -> Tuple[bytes, str]:
    """把 HEIC / LIVP 等图片解成 PNG 字节，供 ffmpeg 按图片素材处理。"""
    import io
    from . import livephoto
    if ext_of_name(name) == "livp":
        still, inner = livephoto.open_livp_still(data)
        img = livephoto._open_image_bytes(still, inner)
    else:
        img = livephoto._open_image_bytes(data, name)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), "png"


def stage_source_items(items: List[Tuple[str, bytes]], ff: str,
                       tmpdir: str) -> Tuple[list, list, int]:
    """混剪素材落盘：图片 + 视频一律接收（不改变准入类型）。

    返回 (paths, source_meta, audio_stripped)；paths 元素为
    (落盘路径, is_video, 原始字节指纹, 源时长)。实况/高效图片先转成 PNG 静态帧；
    解码失败时保留原字节由 ffmpeg 明确报错，绝不静默丢弃素材。
    """
    paths, source_meta = [], []
    audio_stripped = 0
    for name, data in items:
        raw_ext = ext_of_name(name) or "bin"
        is_video = raw_ext in VIDEO_SRC_EXTS
        raw_bytes = data
        ext = raw_ext
        if (not is_video) and raw_ext in STILL_CONVERT_EXTS:
            try:
                data, ext = normalize_still_bytes(name, data)
            except Exception:
                data, ext = raw_bytes, raw_ext
        p = os.path.join(tmpdir, f"src_{len(paths)}.{ext or 'bin'}")
        with open(p, "wb") as f:
            f.write(data)
        # 凡有视频素材参与：先删除原声，后续归一化 / 混剪只基于无音轨副本
        if is_video:
            silent, ok = strip_audio(ff, p, tmpdir, tag=f"src{len(paths)}")
            if ok:
                p = silent
                audio_stripped += 1
        src_dur = probe_duration(ff, p) if is_video else 0.0
        paths.append((p, is_video, fingerprint(raw_bytes), src_dur))
        source_meta.append({"index": len(paths), "name": name,
                            "is_video": is_video,
                            "src_duration": round(src_dur, 2)})
    return paths, source_meta, audio_stripped

# 分辨率档位（短边像素），按画幅映射最终宽高
_RES_SHORT = {"720p": 720, "1080p": 1080, "4k": 2160}


def get_ffmpeg() -> str:
    """返回可用的 ffmpeg 可执行文件路径（优先项目 tools/ffmpeg，其次 imageio_ffmpeg 自带）。"""
    # 1) 项目内置（便于可移植部署）
    bundled = Path(__file__).resolve().parent.parent.parent / "tools" / "ffmpeg"
    for name in ("ffmpeg.exe", "ffmpeg"):
        p = bundled / name
        if p.exists():
            return str(p)
    # 2) imageio_ffmpeg 自带二进制（开发期即用此）
    if _HAS_IMGIO:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    # 3) 回退到 PATH
    return "ffmpeg"


def resolution_to_px(aspect: str, res: str) -> Tuple[int, int]:
    """按画幅与分辨率档位映射像素尺寸。返回 (width, height)。"""
    short = _RES_SHORT.get(res, 1080)
    if aspect == "16:9":
        # 横屏：宽=长边
        long = round(short * 16 / 9)
        return (long, short)
    # 9:16 竖屏：高=长边
    long = round(short * 16 / 9)
    return (short, long)


def fingerprint(data: bytes) -> str:
    """素材指纹：取前 8MB + 总大小做 sha256，兼顾唯一性与速度（大视频不必全量读）。"""
    h = hashlib.sha256()
    h.update(data[:8 * 1024 * 1024])
    h.update(str(len(data)).encode())
    return h.hexdigest()[:32]


# ------------------------- 原创度校验（分母按时长，跨任务复用 K=2） -------------------------

def _usage_path() -> Path:
    from ..core.config import CONFIG_DIR  # 延迟导入，避免循环
    return Path(CONFIG_DIR) / "usage.json"


def load_usage() -> dict:
    p = _usage_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_usage(usage: dict):
    p = _usage_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")


def check_originality(fp_durations: List[Tuple[str, float]], k: int = 2) -> Tuple[bool, str, dict]:
    """按「时长」计算原创度。

    fp_durations: [(素材指纹, 该段时长), ...]
    规则：某素材在「本任务之前」已被使用次数 >= (k-1) 即视为「非原创」（k=2 → 第 2 次使用起算复用）。
    原创度% = 首次使用素材的总时长 / 全部素材总时长 * 100。
    返回 (是否通过, 原因/提示, 明细)
    """
    usage = load_usage()
    total = sum(d for _, d in fp_durations) or 0.0
    fresh = 0.0
    reused_sources = []
    for fp, d in fp_durations:
        prior = usage.get(fp, {}).get("count", 0)
        if prior >= (k - 1):
            reused_sources.append(fp)
        else:
            fresh += d
    pct = (fresh / total * 100) if total > 0 else 100.0
    ok = pct >= 50.0
    detail = {
        "originality": round(pct, 1),
        "total_duration": round(total, 2),
        "fresh_duration": round(fresh, 2),
        "reused_count": len(reused_sources),
        "reused_fps": reused_sources,
    }
    if ok:
        return True, f"原创度 {pct:.1f}%（≥50%，通过）", detail
    reason = (f"原创度仅 {pct:.1f}%，低于 50% 门槛：有 {len(reused_sources)} 个素材段为跨任务复用"
              f"（K={k}，已多次使用）。请更换未使用过或仅使用 1 次的素材后重试。")
    return False, reason, detail


def commit_usage(fp_durations: List[Tuple[str, float]]):
    """任务成功后累加素材使用量（跨任务复用计数）。"""
    usage = load_usage()
    for fp, d in fp_durations:
        rec = usage.setdefault(fp, {"count": 0, "duration": 0.0})
        rec["count"] = rec.get("count", 0) + 1
        rec["duration"] = round(rec.get("duration", 0.0) + d, 3)
    save_usage(usage)


# ------------------------- 单段素材归一化（缺画面时高斯模糊填充，无黑边） -------------------------

def probe_dimensions(ff: str, path: str) -> Tuple[int, int]:
    """用 ffmpeg 探测素材原始显示宽高（图片 / 视频通用），失败返回 (0,0)。"""
    try:
        p = subprocess.run([ff, "-hide_banner", "-i", path],
                           capture_output=True, timeout=30)
        txt = p.stderr.decode("utf-8", "ignore")
    except Exception:
        return (0, 0)
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", txt)
    if not m:
        m = re.search(r"(\d{2,5})x(\d{2,5})", txt)
    if not m:
        return (0, 0)
    w, h = int(m.group(1)), int(m.group(2))
    # 手机竖拍视频常带旋转元数据，显示宽高需互换
    if re.search(r"rotation of -?(?:90|270)", txt, re.I):
        w, h = h, w
    return (w, h) if w > 0 and h > 0 else (0, 0)


def probe_duration(ff: str, path: str) -> float:
    """探测视频真实时长（秒）；图片或探测失败返回 0。"""
    try:
        p = subprocess.run([ff, "-hide_banner", "-i", path],
                           capture_output=True, timeout=30)
        txt = p.stderr.decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
        if not m:
            return 0.0
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        return 0.0


def strip_audio(ff: str, src: str, tmpdir: str, tag: str = "") -> Tuple[str, bool]:
    """先删除原声：生成只有视频流的无声副本，后续混剪一律基于该副本。

    优先流拷贝（不损画质、极快）；容器/编码不兼容时自动回退重编码；
    仍失败则退回原文件（后续每段归一化仍有 -an + 仅映射视频，成品同样无原声）。
    """
    out = os.path.join(tmpdir, f"silent_{os.getpid()}_{abs(hash(src))}_{tag}.mp4")
    attempts = [
        ["-map", "0:v:0", "-c:v", "copy", "-an"],
        ["-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-pix_fmt", "yuv420p", "-an"],
    ]
    for opts in attempts:
        try:
            p = subprocess.run([ff, "-y", "-i", src, *opts,
                                "-movflags", "+faststart", out],
                               capture_output=True, timeout=600)
            if p.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                return out, True
        except Exception:
            continue
    return src, False


def needs_blur(src_wh: Tuple[int, int], w: int, h: int, tol: float = 0.02) -> bool:
    """素材能否铺满目标画幅：宽高比一致 → 能铺满（无需模糊）；不一致 → 需模糊补所缺画面。

    例：16:9 素材生成 9:16 视频，上下必定缺画面 → 用高斯模糊填充。
    探测失败时保守返回 True，保证绝不出现黑边。
    """
    sw, sh = src_wh
    if sw <= 0 or sh <= 0 or w <= 0 or h <= 0:
        return True
    return abs((sw / sh) - (w / h)) > (w / h) * tol


_MOTION_PRESETS = {
    # (panx, pany)：取景窗口在余量内的终点偏移比例（-1~1），决定摇移方向/幅度
    "kenburns": (0.5, 0.3),   # 推近 + 轻微向右下摇移（最自然）
    "zoom_in":  (0.0, 0.0),   # 纯推近（居中放大）
    "zoom_out": (0.0, 0.0),   # 纯拉远（居中缩小）
    "pan_lr":   (0.8, 0.0),   # 横向摇移（左→右）
    "pan_rl":   (-0.8, 0.0),  # 横向摇移（右→左）
}


def _image_motion_filter(w: int, h: int, dur: float, motion: str,
                         strength: float = 0.5) -> Optional[str]:
    """对单张图片生成 Ken Burns 运镜 filter（ffmpeg zoompan）。输入 [0:v]，输出 [mv]。

    通过缓慢改变 zoom（缩放）+ 取景窗口位置（平移）模拟运镜，让静态图「动起来」。
    motion 取值见 _MOTION_PRESETS；无效或 'none' 返回 None。

    关键点：zoompan 的 s=WxH 会把输入直接「拉伸」到目标比例（比例不符则变形），
    故先 scale+固定比例 WZ×HZ（WZ:w=HZ:h，比例已等于画幅）再 crop 对齐，之后
    zoompan 在等比例画布上做推拉摇移，输出严格 w×h 且无变形。
    """
    if not motion or motion == "none":
        return None
    if motion not in _MOTION_PRESETS:
        motion = "kenburns"
    Z = 1.0 + max(0.1, min(0.6, strength)) * 0.5        # 放大余量 1.05~1.30
    zmax = round(Z, 3)
    WZ = int(round(w * Z))
    HZ = int(round(h * Z))
    N = max(1, int(round(float(dur) * 30)))             # 总帧数（30fps）
    zstep = (zmax - 1.0) / N
    # 拉远：zoom 从 zmax 递减到 1.0；其余：从 1.0 递增到 zmax
    z_expr = (f"max({zmax:.4f}-{zstep:.6f}*on,1.0)" if motion == "zoom_out"
              else f"min(1.0+{zstep:.6f}*on,{zmax:.4f})")
    px, py = _MOTION_PRESETS[motion]
    if motion == "zoom_out":
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    else:
        # 取景窗口随 pan 进度在余量内从居中滑向 (panx, pany) 方向（zoompan 自动 clamp 防黑边）
        x_expr = f"iw/2-(iw/zoom/2)+((iw/zoom-{w})/2)*{px:.3f}*(on/{N})"
        y_expr = f"ih/2-(ih/zoom/2)+((ih/zoom-{h})/2)*{py:.3f}*(on/{N})"
    return (f"[0:v]scale={WZ}:{HZ}:force_original_aspect_ratio=increase,"
            f"crop={WZ}:{HZ}[prep];"
            f"[prep]zoompan=z='{z_expr}':d=1:x='{x_expr}':y='{y_expr}'"
            f":s={w}x{h}:fps=30[mv]")


def _build_normalize_filter(w: int, h: int, blur_sigma: float,
                            blur: bool = True, pad_after: float = 0.0,
                            inp: str = "[0:v]", target_dur: float = 0.0) -> str:
    """生成归一化到 w×h 的 filter_complex。

    blur=False（素材比例与画幅一致、能铺满）：直接缩放裁切铺满，画面锐利且无额外开销。
    blur=True（比例不一致、会留白）：背景=模糊放大填充，前景=居中 contain，绝无黑边。
    pad_after>0：短视频素材整段播完后用末帧定格补到目标时长（不循环、不重复画面）。
    target_dur>0：末帧克隆兜底 + 精确裁切，保证输出严格等于该时长。这是防黑屏的关键——
        探测时长偏大时 -ss 会截到内容之外导致片段变短，用末帧填满再裁到精确时长，
        xfade 各输入才能严格等长，尾部不再露黑（参见 2026-11-25 黑屏修复）。
    inp：上游 filter 的输出标签（运镜时上游为 [mv]，否则 [0:v]）。
    """
    # 末帧克隆兜底：追加量取 pad_after / target_dur 的较大者，保证最坏情况（源没帧）也能填满
    pad = max(float(pad_after or 0.0), float(target_dur or 0.0))
    tail_parts = []
    if pad > 0.01:
        # setpts 先归零，兼容 -ss 前置 seek 后 PTS 不从 0 起的情况，避免 trim 取错区间
        tail_parts.append("setpts=PTS-STARTPTS")
        tail_parts.append(f"tpad=stop_mode=clone:stop_duration={pad:.3f}")
    if target_dur > 0.01:
        tail_parts.append(f"trim=duration={target_dur:.3f}")
        tail_parts.append("setpts=PTS-STARTPTS")
    tail = ("," + ",".join(tail_parts)) if tail_parts else ""
    if not blur:
        return (
            f"{inp}scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,format=yuv420p{tail}[out]"
        )
    return (
        f"{inp}scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"gblur={blur_sigma},setsar=1[bg];"
        f"{inp}scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,format=rgba[fg];"
        f"[bg][fg]overlay=format=auto,setsar=1,format=yuv420p{tail}[out]"
    )


def decide_clip_fit(src_duration: float, dur: float,
                    short_mode: str = "freeze", is_video: bool = True,
                    seg_index: int = 0, seg_total: int = 1,
                    start_frac: float = None) -> dict:
    """按「素材真实时长 L」与「成品每段要求时长 D」自动决定混剪取用方式。

    - 图片 / 时长探测失败：定格 D 秒（image_hold / loop 兜底）。
    - L >= D：截取 D 秒（trim），不循环、不拉伸。
      * 传 start_frac（可重复取段模式）：按给定比例随机取点，同一素材多次出现时
        由调用方分配到不同区间，保证每次取到不同画面、提升原创度。
      * 未传（每素材一段模式）：按序号分散取点（第 k/n 条取 (k+1)/(n+1) 处）。
    - L <  D：整段直接使用；freeze=末帧定格补足（推荐，画面不重复），loop=循环重播补足。
    """
    if not is_video or src_duration <= 0:
        return {"mode": "image_hold", "start": 0.0, "pad": 0.0,
                "src_duration": 0.0, "label": "图片定格 / 时长未知"}
    L = float(src_duration)
    if L >= dur - 0.05:
        if start_frac is None:
            frac = ((seg_index + 1) / (seg_total + 1)) if seg_total > 1 else 0.5
            pos = f"第 {seg_index + 1}/{seg_total} 段取点 {frac * 100:.0f}%"
        else:
            frac = min(0.98, max(0.02, float(start_frac)))
            pos = f"随机取点 {frac * 100:.0f}%"
        # 起点安全余量：容器 Duration 可能比真实视频流长（VFR/音轨拉长等），
        # 若按偏大的 L 计算，start 会越过真实内容末尾，截出来短一截、尾部露黑。
        # 把可用起点上界收紧到 (L - dur) 的 90%，宁可少取一点也别越过内容末尾；
        # 即使仍越界，prepare_clip 的末帧克隆兜底也会把片段补到精确 dur 秒。
        usable = max(0.0, L - dur) * 0.90
        start = min(max(0.0, (L - dur) * frac), usable)
        return {"mode": "trim", "start": round(start, 3), "pad": 0.0,
                "src_duration": round(L, 2),
                "label": f"素材 {L:.1f}s ≥ 每段 {dur:.1f}s → {pos}，从 {start:.1f}s 起截取 {dur:.1f}s"}
    gap = max(0.0, dur - L)
    if short_mode == "loop":
        return {"mode": "loop", "start": 0.0, "pad": 0.0,
                "src_duration": round(L, 2),
                "label": f"素材 {L:.1f}s < 每段 {dur:.1f}s → 整段使用 + 循环重播补足 {gap:.1f}s"}
    return {"mode": "freeze", "start": 0.0, "pad": round(gap, 3),
            "src_duration": round(L, 2),
            "label": f"素材 {L:.1f}s < 每段 {dur:.1f}s → 整段直接使用 + 末帧定格 {gap:.1f}s"}


def prepare_clip(src: str, is_video: bool, w: int, h: int, dur: float,
                 blur_sigma: float, ff: str, tmpdir: str,
                 blur: bool = True, tag: str = "", fit: dict = None,
                 motion: str = None, strength: float = 0.5) -> str:
    """把单个素材归一化为 w×h、时长恰好 dur 秒、无音频的临时 mp4。

    fit 由 decide_clip_fit() 给出：长素材精确截取、短素材末帧定格或循环补足。
    传入的是已剥离原声的视频副本；输出再次 -an 且只映射视频流。
    图片素材且 motion 有效时：先叠加 Ken Burns 运镜（zoompan），运镜已铺满全画幅，
    故关闭归一化的模糊填充（避免 pad 留黑边），仅做 scale+crop 归一化。
    """
    fit = fit or decide_clip_fit(0.0, dur, "freeze", is_video)
    out = os.path.join(tmpdir, f"clip_{os.getpid()}_{abs(hash(src))}_{tag}_{dur}.mp4")
    parts = []
    inp = "[0:v]"
    if (not is_video) and motion and motion != "none":
        mf = _image_motion_filter(w, h, dur, motion, strength)
        if mf:
            parts.append(mf)
            inp = "[mv]"          # 运镜输出作为归一化输入
            blur = False          # 运镜已铺满，无需模糊填充
    vf = _build_normalize_filter(w, h, blur_sigma, blur,
                                 pad_after=float(fit.get("pad", 0.0)), inp=inp,
                                 target_dur=dur)
    parts.append(vf)
    vf_full = ";".join(parts)
    cmd = [ff, "-y"]
    if not is_video:
        # 图片：image demuxer loop 1 + 时长（均为输入选项，置于 -i 之前）
        cmd += ["-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}", "-i", src]
    else:
        mode = fit.get("mode")
        if mode == "trim":
            # 长素材：从中段精确截取 D 秒，不做循环
            cmd += ["-ss", f"{float(fit.get('start', 0.0)):.3f}", "-i", src]
        elif mode == "loop":
            # 短素材可选「循环重播」：-stream_loop 必须位于 -i 之前（输入选项）
            cmd += ["-stream_loop", "-1", "-i", src]
        else:
            # 短素材默认：整段直接播放，缺的时长由 filter 里的 tpad 末帧定格补齐
            cmd += ["-i", src]
        cmd += ["-t", f"{dur:.3f}"]  # -t 置于 -i 之后为输出选项，统一裁到 dur 秒
    cmd += [
        "-an", "-r", "30", "-filter_complex", vf_full,
        "-map", "[out]", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        out,
    ]
    _run_ff(cmd, 600, ff, tmpdir, "clip_" + (tag or "seg"))
    return out


def concat_cover_clip(ff: str, cover_clip: str, main_video: str, final: str) -> str:
    """把「片头定格」片段硬切拼接到正片前面（统一重编码，避免流拷贝时间戳错乱）。"""
    subprocess.run([ff, "-y", "-i", cover_clip, "-i", main_video,
                    "-filter_complex",
                    "[0:v]setsar=1[a];[1:v]setsar=1[b];[a][b]concat=n=2:v=1:a=0[vout]",
                    "-map", "[vout]", "-an", "-r", "30", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", final],
                   check=True, capture_output=True, timeout=900)
    return final


# ------------------------- xfade 转场链 -------------------------

def normalize_transitions(raw) -> List[str]:
    """把配置里的转场值规范化为白名单内的效果列表。

    兼容三种输入：None/空 -> [默认fade]；单个字符串 -> [该效果]；列表 -> 过滤后的多选列表。
    """
    if not raw:
        return [DEFAULT_TRANSITION]
    seq = [raw] if isinstance(raw, str) else list(raw)
    valid = [x for x in seq if isinstance(x, str) and x in XFADE_TRANSITIONS]
    return list(dict.fromkeys(valid)) or [DEFAULT_TRANSITION]  # 去重，保证随机池元素唯一


def pick_transitions(pool: List[str], count: int) -> List[str]:
    """为 count 个转场衔接点随机挑效果；效果多于 1 个时保证相邻不重复。"""
    if count <= 0:
        return []
    if len(pool) == 1:
        return pool * count
    out = []
    for _ in range(count):
        choices = [x for x in pool if x != out[-1]] if out else pool
        out.append(random.choice(choices))
    return out


def pick_transitions_balanced(pool: List[str], total: int) -> List[str]:
    """整批任务级均衡随机：每轮把效果池洗牌铺开，保证所选效果都会被用到，且相邻不重复。

    解决「批量合成时每条视频仅 1 个转场点，纯独立随机可能全抽中同一种效果」的体验问题。
    """
    if total <= 0:
        return []
    if len(pool) == 1:
        return pool * total
    seq = []
    while len(seq) < total:
        round_pool = list(pool)
        random.shuffle(round_pool)
        if seq and round_pool[0] == seq[-1]:
            for j in range(1, len(round_pool)):
                if round_pool[j] != seq[-1]:
                    round_pool[0], round_pool[j] = round_pool[j], round_pool[0]
                    break
        seq.extend(round_pool)
    return seq[:total]


def build_xfade_filter(n: int, transitions, t: float, d: float,
                       preset: List[str] = None) -> Tuple[str, List[str], List[str]]:
    """构建 xfade 链路。返回 (filter_complex 字符串, 输入标签列表, 实际使用的转场列表)。

    transitions: 单个效果名或效果名列表；多个时每个衔接点随机取一个（相邻不重复）。
    preset: 上层为整批任务预生成的转场序列，提供时直接采用（保证批次内效果均衡覆盖）。
    n 个等时长(d)片段、转场时长 t：第 k 个衔接点 offset = k*(d - t)。
    """
    pool = normalize_transitions(transitions)
    labels = [f"[{i}:v]" for i in range(n)]
    if n == 1:
        return "[0:v]copy[vout]", labels, []
    used = list(preset[:n - 1]) if preset else pick_transitions(pool, n - 1)
    parts = []
    prev = "[0:v]"
    off = d - t
    for i in range(1, n):
        out_tag = f"[x0{i}]" if i < n - 1 else "[vout]"
        o = i * off
        trans = used[i - 1]
        parts.append(f"{prev}{labels[i]}xfade=transition={trans}:duration={t}:offset={o:.3f}{out_tag}")
        prev = out_tag
    return ";\n".join(parts), labels, used


# ------------------------- 主流程 -------------------------

def _split_items(n_total: int, count: int) -> List[List[int]]:
    """把 n_total 个素材下标尽量均分进 count 组（每组至少 1 个，组大小相差 ≤1）。"""
    count = max(1, min(count, n_total))
    base, rem = divmod(n_total, count)
    groups, idx = [], 0
    for i in range(count):
        size = base + (1 if i < rem else 0)
        groups.append(list(range(idx, idx + size)))
        idx += size
    return groups


def _repeat_plan(target: float, n_segments: int, d_min: float, t_dur: float) -> float:
    """由目标总时长反算「每段时长 d」。

    一条成品视频 = 每个素材各取 1 段（共 n_segments 段），
    n*d - (n-1)*t = target，且每段 d >= d_min（≥5 秒）。
    例：3 条素材 / 目标 30 秒 / 转场 0.5 秒 → 每段 10.33 秒。
    若 target 小于「n 段各 5 秒扣转场」的物理下限，d 取下限，成品会略长于 target
    （如 3 素材设 10 秒 → 实际 14 秒），前端预计时长已同步按此显示。
    """
    t = min(max(float(t_dur), 0.2), 2.0)
    n = max(1, int(n_segments))
    base = max(d_min, float(target))
    d = (base + (n - 1) * t) / n if n > 1 else base
    return max(d_min, round(d, 3))


def _sample_segments(n_total: int, rng: random.Random) -> List[int]:
    """一条成品视频 = 全部素材各 1 段，顺序随机洗牌（a/b/c 各出现一次）。"""
    seq = list(range(n_total))
    rng.shuffle(seq)
    return seq


def _segment_fracs(n_total: int, video_count: int,
                   rng: random.Random) -> dict:
    """为「每条成品视频 × 每个素材」分配取点比例，让同一素材在不同视频里取到不同片段。

    把可用区间按 video_count 等分；每个素材独立打乱这些区间的归属，
    于是自然出现 a1+b1+c1 / a1+b2+c2 / a2+b1+c3 这类组合。
    区间内随机取一点：reshuffle=True 时每次任务不同，False 时由固定种子复现。
    """
    plan = {}
    for i in range(n_total):
        bands = list(range(video_count))
        rng.shuffle(bands)
        for gi in range(video_count):
            lo = bands[gi] / video_count
            plan[(gi, i)] = lo + rng.uniform(0.08, 0.92) / video_count
    return plan


def _fragment_fingerprint(fp: str, src_duration: float, dur: float,
                          frac: float, is_video: bool) -> str:
    """片段指纹 = 素材指纹 + 取点。a1 与 a2 视为不同片段，原创度据此判定。"""
    if not is_video or src_duration <= 0 or frac is None:
        return fp
    L = float(src_duration)
    if L < dur - 0.05:
        return fp                      # 短素材整段使用，取点恒为 0
    f = min(0.98, max(0.02, float(frac)))
    start = max(0.0, (L - dur) * f)
    return f"{fp}@{start:.1f}"


# 多视频混剪：每条视频素材取出的片段最短时长（秒）。
# 凡组内 ≥2 条视频素材（图+视频 / 视频+视频），每段目标时长不低于此值，
# 保证「从多个视频素材中各拿一段，每段至少 5 秒」。
MIN_MIX_SEGMENT = 5.0
# 「多视频混剪」成立条件：组内 ≥2 条视频素材。
# 早期版本要求「纯视频组 ≥3 条素材」并硬拦截，但产出多条成品视频时素材会被均分，
# 例如 4 条素材产出 2 条视频 → 每组仅 2 条，会被误判拦截导致任务直接失败。
# 现统一改为：≥2 条视频即各取一段，短素材由 decide_clip_fit 用末帧定格补足到 5 秒，
# 不再设置条数/时长硬门槛（保证每段实际时长仍 ≥5 秒）。
VIDEO_ONLY_MIN_COUNT = 2
VIDEO_ONLY_MIN_DURATION = 5.0


def check_video_only_rule(groups: List[List[int]], source_meta: List[dict]):
    """识别多视频混剪组并给出提示；不再因条数或时长硬拦截任务。

    - 组内 ≥2 条视频素材（图+视频 / 视频+视频）→ multi_video_groups，
      上层按「从各条视频各取一段，每段 ≥5 秒」合成。
    - 素材短于 5 秒 → 记入 short_notes，由 decide_clip_fit 末帧定格补足，
      不阻断合成（保证每段实际时长仍 ≥5 秒）。
    - 纯图片组 / 单视频组照常处理。
    返回 (True, "", 明细)；保留空 violations 以兼容旧调用方。
    """
    applied_groups, multi_video_groups, short_notes = [], [], []
    for gi, g in enumerate(groups):
        infos = [source_meta[i] for i in g]
        if not infos:
            continue
        video_infos = [x for x in infos if x["is_video"]]
        if len(video_infos) >= VIDEO_ONLY_MIN_COUNT:
            multi_video_groups.append(gi + 1)
        if all(x["is_video"] for x in infos):
            applied_groups.append(gi + 1)
        short = [x for x in video_infos
                 if x["src_duration"] < VIDEO_ONLY_MIN_DURATION - 0.05]
        if short:
            short_notes.append(
                f"第 {gi + 1} 条成品视频有 {len(short)} 条素材短于 "
                f"{VIDEO_ONLY_MIN_DURATION:.0f} 秒，将用末帧定格补足")
    info = {"applied": bool(applied_groups),
            "video_only_groups": applied_groups,
            "multi_video_groups": multi_video_groups,
            "min_count": VIDEO_ONLY_MIN_COUNT,
            "min_duration": VIDEO_ONLY_MIN_DURATION,
            "min_segment": MIN_MIX_SEGMENT,
            "short_notes": short_notes,
            "violations": []}
    return True, "", info


def _assign_covers(n_videos: int, cover_paths: List[str]) -> Tuple[List[str], bool]:
    """为每条视频分配一张封面，返回 (分配列表, 是否发生复用)。

    封面充足时随机打乱后一一分配，严格保证每条视频封面不重复；
    封面不足时轮转补齐（相邻尽量不同）并标记 reused=True，由上层给出提示。
    """
    if not cover_paths:
        return [], False
    pool = list(cover_paths)
    random.shuffle(pool)
    if len(pool) >= n_videos:
        return pool[:n_videos], False
    assigned = []
    while len(assigned) < n_videos:
        round_pool = list(pool)
        random.shuffle(round_pool)
        if assigned and len(round_pool) > 1 and round_pool[0] == assigned[-1]:
            round_pool[0], round_pool[1] = round_pool[1], round_pool[0]
        for c in round_pool:
            if len(assigned) >= n_videos:
                break
            assigned.append(c)
    return assigned, True


# ------------------------- 配音 / 字幕（脚本） / 背景乐 -------------------------

AUDIO_EXTS = ("mp3", "wav", "m4a", "aac", "flac", "ogg", "wma", "aif", "aiff")
_BS = chr(92)   # ffmpeg filtergraph 逗号转义符；用 chr() 避免源码里多重反斜杠转义


def check_script_pairs(scripts, voices):
    """配音与字幕必须一一对应（无论哪种任务模式 / 批量与否）。

    规则：数量相等；每条脚本都有字幕文本；每个配音音频都有字节。
    没有任何脚本时视为「未启用配音字幕」，保持原有无声行为。
    返回 (是否通过, 提示, 明细)。
    """
    s = list(scripts or [])
    a = list(voices or [])
    info = {"enabled": bool(s or a), "script_count": len(s),
            "voice_count": len(a), "paired": False}
    if not s and not a:
        return True, "", info
    if not a:
        # 有字幕无配音：允许（纯字幕 / AI 脚本不配音模式，无声但保留字幕）
        info["paired"] = True
        return True, "", info
    if len(s) != len(a):
        lack = "字幕脚本" if len(s) < len(a) else "配音音频"
        return False, (f"配音与字幕未一一对应：已添加 {len(a)} 个配音音频、{len(s)} 条字幕脚本，"
                       f"请补足{lack}（第 N 个配音 ↔ 第 N 条字幕）后再提交。"), info
    for i, x in enumerate(s):
        if not str((x or {}).get("subtitle") or "").strip():
            return False, f"第 {i + 1} 条脚本缺少字幕文本，无法与第 {i + 1} 个配音音频一一对应。", info
        if len(a[i]) < 2 or not a[i][1]:
            return False, f"第 {i + 1} 个配音音频为空，无法与第 {i + 1} 条字幕对应。", info
    info["paired"] = True
    return True, "", info


def _assign_scripts(n_videos: int, scripts: List[dict], voice_paths: List[str]):
    """一条成品视频一套脚本：第 N 条脚本 → 第 N 条视频；不足时轮转复用并提示。"""
    n = len(scripts or [])
    if not n:
        return [], False
    out = [(scripts[i % n], voice_paths[i % n]) for i in range(n_videos)]
    return out, n < n_videos


# 字幕切分边界：
#  - 句末强断点（。！？…；;及换行）：作为字幕块边界，句末长停顿计入前块末尾；
#  - 逗号轻断点（，）：同样作为字幕块边界（轻切分），让一句里的逗号也单独成行、各自顺序
#    显示，配合字幕淡入淡出观感更柔和；逗号停顿权重较小（见 _PUNCT_PAUSE）。
# 顿号等其他句中停顿标点仍保留在块内，仅表示停留时长、不切分字幕。
_SPLIT_CHARS = set("。！？!?…；;\n，,")
# 字幕块末尾要剥离的标点（句末标点 + 逗号等），避免字幕末尾带标点
_TAIL_PUNCT = re.compile(r"[。！？!?…；;，,、．.]+$")
# 标点停顿权重（相对时间单位，模拟人声在标点处的停留；数值越大停顿越久）
_PUNCT_PAUSE = {
    "。": 0.50, "．": 0.50, ".": 0.50,
    "！": 0.50, "!": 0.50,
    "？": 0.50, "?": 0.50,
    "…": 0.60,
    "；": 0.35, ";": 0.35,
    "，": 0.22, ",": 0.22,
    "、": 0.18,
    "\n": 0.30,
}
_CHAR_UNIT = 0.26  # 每个普通字符的基础朗读时长（相对时间单位）


def _segment_and_time(text: str) -> Tuple[List[str], List[Tuple[float, float]], float]:
    """把字幕文案切成「逐句/逐轻句」字幕块，并估算每块的相对时间轴（字符数 + 标点停顿）。

    切分规则：
      - 句末强断点（。！？…；;）与换行作为字幕块硬边界；
      - 逗号作**轻切分**边界：一句里的逗号也单独成块、各自顺序显示（如「你好，我是XX」→
        「你好」「我是XX」），配合字幕淡入淡出让观感更柔和；
      - 顿号等其余句中停顿标点**保留在块内**，不切分字幕，仅让该块显示更久（贴近人声停顿）；
      - 每块末尾剥离句末/逗号等标点，字幕不出现难看的尾标点。
    时间轴：逐字符累加「字符基础时长 + 标点停顿」，强/轻断点的停顿计入前一块末尾，
    使后一块恰在配音说完上一句 + 句号/逗号停顿后才出现 —— 与配音人声对齐。
    返回 (块文本列表, [(start,end)...] 相对时间, 估算总时长)。空文本返回 ([],[],0.0)。
    """
    raw = str(text or "")
    blocks: List[str] = []
    spans: List[Tuple[float, float]] = []
    t = 0.0
    buf = ""
    buf_start = 0.0

    def _flush():
        nonlocal buf, buf_start
        if not buf:
            return
        b = _TAIL_PUNCT.sub("", buf).strip()
        if b:
            blocks.append(b)
            spans.append((buf_start, t))
        buf = ""

    for ch in raw:
        if ch == "\n":
            _flush()
            t += _PUNCT_PAUSE["\n"]
            buf_start = t
        elif ch in _SPLIT_CHARS:
            if buf:
                buf += ch
                t += _PUNCT_PAUSE.get(ch, 0.40)
                _flush()
            else:
                # 连续强断点：累加停顿，不开新块
                t += _PUNCT_PAUSE.get(ch, 0.40)
            buf_start = t
        else:
            buf += ch
            t += _PUNCT_PAUSE.get(ch, _CHAR_UNIT)
    _flush()
    return blocks, spans, t


def _detect_voice_pauses(ff: str, voice_path: str, max_dur: float) -> List[float]:
    """检测配音音频中的真实句间停顿点（静音段中点，单位秒）。

    TTS 配音在句/逗号处通常有可测量的停顿；把它作为字幕切块的锚点，可消除
    「按字数估算 + 全局线性缩放」造成的中间漂移（首尾因缩放锚定所以看着准，
    中间逐句累积误差 → 字幕早于人声）。

    失败（无 ffmpeg / 音频损坏 / 无静音段）时返回空列表，调用方回退估算逻辑。
    """
    try:
        cmd = [ff, "-hide_banner", "-nostats", "-i", voice_path,
               "-af", "silencedetect=noise=-35dB:d=0.15", "-f", "null", "-"]
        p = subprocess.run(cmd, capture_output=True, timeout=180)
        err = (p.stderr or b"").decode("utf-8", "replace")
        starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", err)]
        ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", err)]
        mids = []
        for i, st in enumerate(starts):
            if i >= len(ends):
                break
            en = ends[i]
            mid = (st + en) / 2.0
            # 过滤片头/片尾静音与过短停顿（字间气口不算句间边界）
            if 0.25 <= mid <= max_dur - 0.35 and (en - st) >= 0.15:
                mids.append(round(mid, 3))
        return mids
    except Exception:
        return []


def _snap_spans_to_pauses(spans: List[Tuple[float, float]], pauses: List[float],
                          show_dur: float) -> List[Tuple[float, float]]:
    """把字幕块内部边界吸附到最近的真实停顿点。

    - 首尾锚定：第一块从 0 开始、最后一块到 show_dur（配音真实时长）结束；
    - 单调约束：吸附后的边界严格递增，且相邻块至少 min_gap 秒，杜绝重叠/倒挂；
    - 容差约束：仅在估算边界附近 tol 秒内吸附，避免把边界拉到很远反而更糟；
    - 部分吸附：静音点数与块数不必相等，能吸附几个就吸附几个，其余保留估算值。
    """
    n = len(spans)
    if n < 2 or not pauses:
        return spans
    avg = show_dur / n
    tol = max(0.5, min(2.5, avg * 0.75))
    min_gap = min(0.35, avg * 0.3)
    bounds = [0.0]          # 边界列表：b[0]=0，b[i]=第 i 块的 start，b[-1]=show_dur
    used = set()
    prev = 0.0
    for i in range(n - 1):
        est = spans[i][1]   # 该块估算的结束点 = 下一块的起始边界
        best, best_d = None, tol + 1.0
        for j, p in enumerate(pauses):
            if j in used or p <= prev + min_gap or p > show_dur - min_gap:
                continue
            d = abs(p - est)
            if d < best_d:
                best_d, best = d, j
        if best is not None and best_d <= tol:
            used.add(best)
            nb = pauses[best]
        else:
            nb = est        # 容差内无可用停顿点 → 保留估算边界
        nb = max(nb, prev + min_gap)
        bounds.append(round(nb, 3))
        prev = nb
    bounds.append(show_dur)
    out = []
    for i in range(n):
        st, en = bounds[i], bounds[i + 1]
        if en <= st:        # 极端兜底（正常已被单调约束排除）
            en = min(show_dur, st + min_gap)
        out.append((round(st, 3), round(en, 3)))
    return out


def _segment_subtitle(text: str) -> List[str]:
    """[_segment_and_time 的薄包装] 仅返回字幕块纯文本（末尾不含标点）。"""
    return _segment_and_time(text)[0]


def render_text_png(style: dict, content: str, w: int, h: int, path: str) -> str:
    """用「图片加文字」同一套样式渲染透明文字层 PNG（视频硬字幕 / 顶部标题）。"""
    from . import image as image_svc
    image_svc.render_text_layer(w, h, style, content).save(path, "PNG")
    return path


def apply_script_and_bgm(ff: str, video_in: str, out_path: str, w: int, h: int,
                         total_dur: float, tpl: dict, script: dict,
                         voice_path: str, bgm_path: str, vcfg: dict,
                         tmpdir: str, tag: str = "") -> dict:
    """烧录字幕/顶部标题，并把配音 + 背景乐混音到成品视频。

    字幕先按「强断点（。！？…；; 及换行）+ 逗号轻断点」切成字幕块（顿号等其余句中
    标点保留在块内，仅表示说话停顿、不切分字幕），再按「字符数 + 标点停顿权重」估算的相对
    时间轴逐块顺序显示、彼此不重叠，并归一化到配音真实时长 —— 这就是字幕与配音人声对齐的核心；
    每条字幕末尾剥离标点。相邻字幕通过 alpha 淡入淡出交叉过渡，切换更柔和。
    顶部标题贯穿全片（不淡入淡出）。位置/样式全部来自视频模板，
    与「图片加文字」模板共用同一套渲染函数。
    """
    script = script or {}
    tpl = tpl or {}
    sub_style = tpl.get("subtitle") or {}
    title_style = tpl.get("title") or {}
    sub_on = bool(tpl.get("subtitle_enabled", True))
    title_on = bool(tpl.get("title_enabled", True))
    blocks, spans_rel, est_total = (
        _segment_and_time(script.get("subtitle", "")) if sub_on else ([], [], 0.0)
    )
    lines = blocks
    title_text = (str(script.get("title") or "").strip()) if title_on else ""

    voice_dur = probe_duration(ff, voice_path) if voice_path and os.path.exists(voice_path) else 0.0
    show_dur = min(total_dur, voice_dur) if voice_dur > 0.05 else total_dur
    show_dur = max(0.2, min(show_dur, total_dur))

    bgm_vol = max(0.0, min(1.0, float(vcfg.get("bgm_volume", 0.2))))
    voice_vol = max(0.0, min(1.0, float(vcfg.get("voice_volume", 1.0))))
    fade = max(0.05, min(float(vcfg.get("bgm_fade", 0.6)), total_dur / 3.0))

    # 1) 透明文字层：标题 1 张 + 字幕逐块多张（按配音时长逐块顺序显示）
    layers = []   # (png, start, end, kind)
    if title_text:
        p = os.path.join(tmpdir, f"txt_{tag}_0.png")
        render_text_png(title_style, title_text, w, h, p)
        layers.append((p, 0.0, total_dur, "title"))

    n = len(blocks)
    timeline = []
    if n >= 1:
        if est_total > 1e-6:
            # 按「字符数 + 标点停顿」估算的相对时间轴，归一化到配音真实时长
            scale = show_dur / est_total
            spans = [(st * scale, en * scale) for st, en in spans_rel]
        elif n == 1:
            # 退化：整段配音时长内持续显示
            spans = [(0.0, show_dur)]
        else:
            # 估算异常兜底：均分
            per = show_dur / n
            spans = [(i * per, (i + 1) * per) for i in range(n)]
        # 真实停顿点吸附：配音在句/逗号处有可测停顿时，用它们校正字幕切块边界，
        # 消除「按字数估算 + 全局缩放」造成的中间逐句漂移（首尾仍锚定）。
        # 只有 >=2 块、且有配音文件才尝试；检测失败/点数不足时保留上面的估算 spans。
        align = "estimate"
        if n >= 2 and voice_path and os.path.exists(voice_path) and voice_dur > 0.05:
            pauses = _detect_voice_pauses(ff, voice_path, show_dur)
            # 至少要有 1 个可用停顿点才吸附，避免无意义调用
            if pauses:
                snapped = _snap_spans_to_pauses(spans, pauses, show_dur)
                # 仅当吸附产生了实际变化才采用（静音点过少/过远时不打扰原逻辑）
                if snapped != spans:
                    spans = snapped
                    align = "pause_snap"
        for line, (st, en) in zip(blocks, spans):
            p = os.path.join(tmpdir, f"txt_{tag}_{len(layers)}.png")
            render_text_png(sub_style, line, w, h, p)
            layers.append((p, st, en, "subtitle"))
            timeline.append({"start": round(st, 2), "end": round(en, 2), "text": line[:60]})

    # 2) 输入顺序：0=正片，1..k=文字层，k+1=配音，k+2=背景乐
    cmd = [ff, "-y", "-i", video_in]
    for p, _, _, _ in layers:
        cmd += ["-loop", "1", "-t", f"{total_dur:.3f}", "-i", p]
    next_idx = 1 + len(layers)
    voice_idx = bgm_idx = None
    if voice_path and os.path.exists(voice_path):
        voice_idx = next_idx
        cmd += ["-i", voice_path]
        next_idx += 1
    if bgm_path and os.path.exists(bgm_path):
        bgm_idx = next_idx
        cmd += ["-stream_loop", "-1", "-i", bgm_path]
        next_idx += 1

    # 3) 视频链：逐层 overlay，按时间轴 enable 显示；字幕叠加 alpha 淡入淡出让切换更柔和
    fade_sec = max(0.05, float(vcfg.get("subtitle_fade", 0.2)))
    parts = []
    prev = "[0:v]"
    for i, (_p, st, en, kind) in enumerate(layers):
        out_tag = "[vout]" if i == len(layers) - 1 else f"[vx{i}]"
        if kind == "title":
            # 顶部标题贯穿全片，常显、不淡入淡出
            enable = f"gte(t{_BS},0)"
            parts.append(f"{prev}[{i + 1}:v]overlay=0:0:format=auto:enable='{enable}'{out_tag}")
        else:
            # 字幕：对文字层 alpha 做淡入（窗起点）与淡出（窗终点），相邻字幕首尾交叉过渡；
            # 之后用常显 + alpha 控制可见性，窗外 alpha=0 自然隐藏，无需 between 硬开关。
            fin = min(fade_sec, max(0.05, (en - st) * 0.4))
            fout = fin
            ftag = f"fs{i}"
            parts.append(
                f"[{i + 1}:v]fade=t=in:st={st:.3f}:d={fin:.3f}:alpha=1,"
                f"fade=t=out:st={max(st, en - fout):.3f}:d={fout:.3f}:alpha=1[{ftag}]")
            parts.append(
                f"{prev}[{ftag}]overlay=0:0:format=auto:enable='gte(t{_BS},0)'{out_tag}")
        prev = out_tag
    if not layers:
        parts.append("[0:v]null[vout]")

    # 4) 音频链：配音 + 背景乐（循环、降音量、淡入淡出）→ amix
    has_audio = voice_idx is not None or bgm_idx is not None
    trim_T = f"{total_dur:.3f}"
    fade_out_st = f"{max(0.0, total_dur - fade):.2f}"
    if voice_idx is not None and bgm_idx is not None:
        parts.append(f"[{voice_idx}:a]atrim=0:{trim_T},apad,"
                     f"afade=t=in:st=0:d={fade:.2f},volume={voice_vol}[va]")
        parts.append(f"[{bgm_idx}:a]atrim=0:{trim_T},apad,"
                     f"afade=t=in:st=0:d={fade:.2f},afade=t=out:st={fade_out_st}:d={fade:.2f},"
                     f"volume={bgm_vol}[ba]")
        parts.append("[va][ba]amix=inputs=2:duration=first:normalize=0[aout]")
    elif voice_idx is not None:
        parts.append(f"[{voice_idx}:a]atrim=0:{trim_T},apad,"
                     f"afade=t=in:st=0:d={fade:.2f},volume={voice_vol}[aout]")
    elif bgm_idx is not None:
        parts.append(f"[{bgm_idx}:a]atrim=0:{trim_T},apad,"
                     f"afade=t=in:st=0:d={fade:.2f},afade=t=out:st={fade_out_st}:d={fade:.2f},"
                     f"volume={bgm_vol}[aout]")

    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += ["-t", trim_T, "-r", "30", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", out_path]
    subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    return {"source": script.get("source", "manual"),
            "template": tpl.get("name") or "内置默认模板",
            "subtitle_lines": len(lines),
            "subtitle_align": align,
            "subtitle_text": str(script.get("subtitle") or "")[:120],
            "title": title_text[:60],
            "voice_duration": round(voice_dur, 2),
            "subtitle_duration": round(show_dur, 2),
            "subtitle_timeline": timeline,
            "bgm_volume": bgm_vol, "voice_volume": voice_vol}


def _run_ff(cmd: list, timeout: int, ff: str, tmpdir: str, tag: str) -> None:
    """执行 ffmpeg 命令；失败时把 stderr 写入临时目录日志并抛出带摘要的错误。

    历史问题：所有 ffmpeg 调用都用 capture_output=True 吞掉日志，一旦失败只能看到
    "CalledProcessError"，无法判断是源素材损坏、时间戳异常还是滤镜参数问题。
    这里在失败时保留最后一段 stderr（写入 tmpdir/ffmpeg_err_<tag>.log，任务失败会
    清理 tmpdir，故同时把摘要放进异常消息，便于前端/日志定位）。
    """
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "replace")
        try:
            log = os.path.join(tmpdir, f"ffmpeg_err_{tag}.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write("COMMAND:\n" + " ".join(str(x) for x in cmd) + "\n\nSTDERR:\n" + err)
        except Exception:
            pass
        # 取最后若干行里含 error/invalid/cannot 等关键字的摘要
        lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
        key = [ln for ln in lines if any(k in ln.lower()
               for k in ("error", "invalid", "cannot", "no such", "failed", "unable"))]
        brief = (key[-3:] if key else lines[-6:])
        raise RuntimeError("ffmpeg 处理失败 [%s]: %s" % (tag, " | ".join(brief)[:500]))


def _move_overwrite(src: str, dst: str) -> None:
    """跨盘安全地把 src 移动并覆盖 dst。

    os.replace/os.rename 在 Windows 上不能跨磁盘卷（WinError 17 / errno.EXDEV）：
    临时目录常在 C 盘（tempfile.mkdtemp），而作品目录在应用安装盘（如 E 盘），
    混音后的成品从临时目录落到作品目录时必然跨盘。shutil.move 会先尝试 rename，
    失败后自动退化为「复制 + 删除」，可可靠跨盘；同盘时仍是高效 rename。
    """
    try:
        os.replace(src, dst)
    except OSError as e:
        if getattr(e, "winerror", None) == 17 or e.errno == errno.EXDEV:
            shutil.move(src, dst)
        else:
            raise


def process_video(items: List[Tuple[str, bytes]], cfg: dict, out_dir: str,
                  progress_cb=None, covers=None, scripts=None,
                  voices=None, bgm=None, user=None) -> dict:
    # user 参数保留：脚本/配音的 AI 生成已迁到「文案生产」，此处不再使用，仅为调用方兼容。
    """合成视频。支持多条视频、多转场随机、封面片头、配音字幕脚本、背景乐。

    items: [(文件名, 原始字节), ...]（图片或视频任意混合）
    cfg: 含 video 配置块（见 config.DEFAULTS['video']）
    out_dir: 产物目录
    covers: [(文件名, 原始字节), ...] 现成封面图；会作为片头定格合入视频
    scripts: [{"subtitle": 文本, "title": 顶部标题, "source": "manual"|"ai"}, ...]
    voices:  [(文件名, 原始字节), ...] 配音音频，与 scripts 按顺序一一对应
    bgm:     [(文件名, 原始字节), ...] 背景乐池，每条成品视频分配一首且互不重复
    返回 {"ok": bool, "outputs": [...], "originality": 明细, "video_meta": {...}, "error": 可选}
    """
    v = (cfg or {}).get("video", {}) or {}
    if progress_cb:
        # 进入处理：先给一个很小的进度，便于前端尽快开始估算剩余时间
        progress_cb(stage="准备素材与校验…", percent=2)
    aspect = v.get("aspect", "9:16")
    res = v.get("resolution", "1080p")
    # 多选转场优先；未选择（空）回落到单值 transition，再回落到默认 fade
    transitions = normalize_transitions(
        v.get("transitions") if v.get("transitions") else v.get("transition", DEFAULT_TRANSITION)
    )
    t_dur = float(v.get("transition_duration", 0.5))
    strategy = v.get("duration_strategy", "fixed")
    target = float(v.get("target_duration", 30))
    clip_dur = float(v.get("clip_duration", 5))
    blur_sigma = float(v.get("blur_sigma", 25))
    cover = v.get("cover", "none")
    cover_duration = min(10.0, max(0.2, float(v.get("cover_duration", 1.0))))
    k = int(v.get("originality_k", 2))
    orig_enabled = bool(v.get("originality_enabled", True))
    video_count = max(1, int(v.get("video_count", 1)))
    # 取段模式：repeat=从全部素材反复随机取段拼满目标总时长（素材可重复出现，默认）；
    #           unique=素材均分到各条视频、每个素材只取一段（旧逻辑，保留可回退）。
    # reshuffle=True 每次任务重新随机组合；False 用固定种子，同批素材可复现。
    mix_mode = str(v.get("mix_mode", "repeat") or "repeat").lower()
    if mix_mode not in ("repeat", "unique"):
        mix_mode = "repeat"
    reshuffle = bool(v.get("reshuffle", True))
    repeat_mode = (mix_mode == "repeat")
    short_mode = str(v.get("short_video_mode", "freeze") or "freeze")
    if short_mode not in ("freeze", "loop"):
        short_mode = "freeze"
    # 图片运镜：凡是图片参与合成都加 Ken Burns 运镜（用户明确要求）
    #   - random_many：每张图片各自随机不同风格（默认，节奏更灵动）
    #   - random：整批随机选一个统一风格
    #   - 具体风格：kenburns / zoom_in / zoom_out / pan_lr / pan_rl
    #   - none：关闭运镜（静态定格）
    motion_cfg = v.get("motion")
    if not motion_cfg:
        motion_cfg = "random_many"
    MOTION_CHOICES = list(_MOTION_PRESETS.keys())   # 5 种风格
    if motion_cfg == "random":
        motion = random.choice(MOTION_CHOICES)        # 整批统一一个
    elif motion_cfg == "random_many":
        motion = "random_many"                        # 标记：调用处逐张随机
    elif motion_cfg not in MOTION_CHOICES and motion_cfg != "none":
        motion = "kenburns"                          # 非法值兜底
    else:
        motion = motion_cfg
    motion_strength = float(v.get("motion_strength", 0.5))

    # 脚本（配音 + 字幕）必须一一对应；脚本与配音由「文案生产」预先生成后导入
    scripts = list(scripts or [])
    voices = list(voices or [])
    bgm = list(bgm or [])
    pair_ok, pair_msg, pair_info = check_script_pairs(scripts, voices)
    if not pair_ok:
        return {"ok": False, "error": pair_msg,
                "script_pairs": pair_info, "outputs": []}
    from .video_templates import resolve as resolve_vtpl
    vtpl = resolve_vtpl(v)

    w, h = resolution_to_px(aspect, res)
    n_total = len(items)
    if n_total == 0:
        return {"ok": False, "error": "没有素材"}

    ff = get_ffmpeg()
    tmpdir = tempfile.mkdtemp(prefix="lf_video_")
    # 先落盘素材并计算指纹：混剪准入仍是「图片 + 视频」两类，不做单一类型限制
    paths, source_meta, audio_stripped = stage_source_items(items, ff, tmpdir)

    # 落盘现成封面图（仅接受图片扩展名；HEIC / LIVP 先转 PNG 静态帧）
    cover_paths = []
    for name, data in (covers or []):
        ext = ext_of_name(name)
        if ext not in IMAGE_SRC_EXTS:
            continue
        if ext in STILL_CONVERT_EXTS:
            try:
                data, ext = normalize_still_bytes(name, data)
            except Exception:
                continue   # 无法解码的封面跳过，与其他非图片扩展名一致
        p = os.path.join(tmpdir, f"cover_src_{len(cover_paths)}.{ext}")
        with open(p, "wb") as f:
            f.write(data)
        cover_paths.append(p)

    # 落盘配音音频 / 背景乐（可选；配音与 scripts 已在开头校验一一对应）
    def _dump_audio(pairs, prefix):
        out_paths = []
        for i, (name, data) in enumerate(pairs):
            ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "")
            if ext not in AUDIO_EXTS:
                ext = "mp3"
            p = os.path.join(tmpdir, f"{prefix}_{i}.{ext}")
            with open(p, "wb") as f:
                f.write(data)
            out_paths.append(p)
        return out_paths

    voice_paths = _dump_audio(voices, "voice")
    bgm_paths = _dump_audio(bgm, "bgm")

    # 随机源：reshuffle=True 每次任务不同；False 用「素材指纹 + 配置」做种子，便于复现
    if reshuffle:
        rng = random.Random()
    else:
        seed_src = "|".join(str(x[2]) for x in paths) + f"|{video_count}|{target}|{clip_dur}|{mix_mode}"
        rng = random.Random(int(hashlib.sha256(seed_src.encode()).hexdigest()[:16], 16))

    # 分组 = 每条成品视频的段序列。repeat 模式下每条视频都用上全部素材各 1 段，
    # 但不同成品视频从同一素材取「不同片段」（a1/a2/a3）。
    if repeat_mode:
        d_min = max(MIN_MIX_SEGMENT, clip_dur)
        seg_count = n_total
        d_repeat = _repeat_plan(target, seg_count, d_min, t_dur)
        groups = [_sample_segments(n_total, rng) for _ in range(video_count)]
        frac_plan = _segment_fracs(n_total, video_count, rng)
    else:
        seg_count, d_repeat, frac_plan = 0, 0.0, {}
        groups = _split_items(n_total, video_count)
    # 多视频混剪识别：组内 ≥2 条视频即「各取一段、每段 ≥5 秒」；不再硬拦截
    rule_ok, rule_msg, rule_info = check_video_only_rule(groups, source_meta)
    if not rule_ok:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"ok": False, "error": rule_msg,
                "video_only_rule": rule_info, "outputs": []}
    assigned_covers, cover_reused = _assign_covers(len(groups), cover_paths)
    # 一条成品视频一套脚本；背景乐每条不同（不足时轮转复用并提示）
    assigned_scripts, scripts_reused = _assign_scripts(len(groups), scripts, voice_paths)
    assigned_bgm, bgm_reused = _assign_covers(len(groups), bgm_paths)

    # 整批任务级转场序列：先按每条视频的衔接点数合计，一次性均衡随机，再按组切分
    transition_counts = [max(0, len(g) - 1) for g in groups]
    transition_seq = pick_transitions_balanced(transitions, sum(transition_counts))

    def clip_dur_for(m: int) -> float:
        """单组 m 个素材时每段时长：指定每段时长（默认）直接用 clip_dur；
        按比例分配总时长时先扣减转场再均分。"""
        if strategy == "fixed":
            return max(0.5, clip_dur)
        t_eff = min(max(t_dur, 0.2), 2.0)
        d = (target + (m - 1) * t_eff) / m if m > 1 else target
        return max(0.5, d)

    # 多视频混剪：凡组内含视频素材（图+视频 / 视频+视频），每段目标时长抬到 ≥5 秒，
    # 保证从每条视频各取的一段都不短于 5 秒；纯图片组仍严格沿用用户设定值。
    # 用户设定的每段时长 / 按比例分配算出的值若小于 5 秒会被自动抬升，
    # 因此含视频时成品总时长可能略长于设定的目标总时长。
    if repeat_mode:
        # 每条成品视频段数相同，段时长由目标总时长反算；保证每条恰好 target 秒
        group_ds = [d_repeat] * len(groups)
    else:
        group_ds = [
            max(MIN_MIX_SEGMENT, clip_dur_for(len(g)))
            if any(paths[j][1] for j in g) else clip_dur_for(len(g))
            for g in groups
        ]

    # 原创度校验：repeat 模式按「片段」判定（a1 与 a2 算不同片段），
    # 已用片段占比 <50%（fresh ≥50%）即合格；unique 模式沿用旧的素材指纹口径。
    # 开关关闭时跳过（便于反复用同批素材测试）。
    if repeat_mode:
        fps = [
            (_fragment_fingerprint(paths[i][2], paths[i][3], group_ds[gi],
                                   frac_plan.get((gi, i)), paths[i][1]),
             group_ds[gi])
            for gi, g in enumerate(groups) for i in g
        ]
    else:
        fps = [(paths[i][2], group_ds[gi]) for gi, g in enumerate(groups) for i in g]
    if orig_enabled:
        ok, msg, detail = check_originality(fps, k=k)
        if not ok:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return {"ok": False, "error": msg, "originality": detail, "outputs": []}
    else:
        detail = {"originality": 100.0, "total_duration": 0.0,
                  "fresh_duration": 0.0, "reused_count": 0,
                  "reused_fps": [], "skipped": True}

    if progress_cb:
        stage = "原创度校验通过，开始合成"
        multi_g = rule_info.get("multi_video_groups") or []
        if repeat_mode:
            stage += (f"（重复取不同片段：每条 {seg_count} 段 × {d_repeat:.2f}s ≈ {target:.0f}s，"
                      f"共 {len(groups)} 条成品视频；各素材取不同片段组合如 a1+b2+c2 / a2+b1+c3"
                      f"{'，每次任务重新随机' if reshuffle else '，固定组合可复现'}；"
                      f"原创度按片段判定）")
        elif multi_g:
            stage += (f"（多视频混剪：第 {'/'.join(str(x) for x in multi_g)} 条成品视频"
                      f"从各条视频素材各取一段，每段 ≥{MIN_MIX_SEGMENT:.0f} 秒）")
        if rule_info.get("applied") and not repeat_mode:
            stage += "（纯视频混剪：各条素材各取一段）"
        if rule_info.get("short_notes"):
            stage += (f"（{len(rule_info['short_notes'])} 条成品视频含短素材，"
                      f"已自动补足到 {MIN_MIX_SEGMENT:.0f} 秒）")
        # 含视频的组被抬升到 5 秒时给出明确提示，避免用户对成品变长感到疑惑
        if not repeat_mode and any(group_ds[gi] > clip_dur_for(len(g)) + 0.01
                                   for gi, g in enumerate(groups) if any(paths[j][1] for j in g)):
            stage += f"（含视频素材：每段时长已自动抬升到不少于 {MIN_MIX_SEGMENT:.0f} 秒）"
        if audio_stripped:
            stage += f"（{audio_stripped} 条视频素材已剥离原声）"
        if cover_reused:
            stage += f"（封面仅 {len(cover_paths)} 张 / {len(groups)} 条视频，将循环复用）"
        if pair_info.get("enabled"):
            stage += f"（{len(scripts)} 套脚本：配音与字幕一一对应）"
        if scripts_reused:
            stage += f"（脚本仅 {len(scripts)} 套 / {len(groups)} 条视频，将循环复用）"
        if bgm_paths:
            stage += f"（{len(bgm_paths)} 首背景乐）"
        if bgm_reused:
            stage += "（背景乐不足，将循环复用）"
        progress_cb(stage=stage, percent=8)

    # 逐组合成独立视频
    outputs = []
    videos_meta = []
    blur_used = 0
    blur_total = 0
    cover_applied = 0
    all_fits = []
    for gi, g in enumerate(groups):
        m = len(g)
        d = group_ds[gi]
        if progress_cb:
            # 进度百分比（供前端倒计时）：setup 占 8%，每条视频占 80%/G，收尾 12%
            _pct_c = int(8 + (gi / len(groups)) * 80 + (80 / len(groups)) * 0.30)
            progress_cb(stage=f"合成第 {gi + 1}/{len(groups)} 条视频（{m} 个素材）", percent=_pct_c)
        clips = []
        group_fits = []
        # 多视频混剪：分散取点只按「组内视频素材」编号，图片不占用视频的取点位置，
        # 保证图+视频混合时 a/b/c 各条视频仍落在各自可用区间的不同位置。
        video_total = sum(1 for j in g if paths[j][1])
        video_pos = 0
        for seg_pos, i in enumerate(g):
            p, is_video, fp, src_dur = paths[i]
            # 仅当素材宽高比与目标画幅不一致（铺不满、会留白）时才启用高斯模糊
            src_wh = probe_dimensions(ff, p)
            blur = needs_blur(src_wh, w, h)
            blur_total += 1
            blur_used += 1 if blur else 0
            # 按素材真实时长与每段目标时长自动决定：截取中段 / 整段直接使用。
            # repeat 模式用规划好的随机取点（同一素材在不同成品视频取不同片段）
            if repeat_mode:
                frac = frac_plan.get((gi, i))
                fit = decide_clip_fit(src_dur, d, short_mode, is_video,
                                      start_frac=frac)
                frag = _fragment_fingerprint(fp, src_dur, d, frac, is_video)
            else:
                seg_i, seg_n = (video_pos, video_total) if is_video else (0, 1)
                fit = decide_clip_fit(src_dur, d, short_mode, is_video,
                                      seg_index=seg_i, seg_total=seg_n)
                frag = fp
            if is_video and not repeat_mode:
                video_pos += 1
            group_fits.append({"index": i + 1,
                               "video": gi + 1, "segment": seg_pos + 1,
                               "fragment": frag,
                               "type": "video" if is_video else "image",
                               "mode": fit["mode"],
                               "src_duration": fit["src_duration"],
                               "start": fit["start"],
                               "pad": fit["pad"],
                               "label": fit["label"]})
            clip_motion = (random.choice(MOTION_CHOICES)
                           if motion == "random_many" else motion)
            clips.append(prepare_clip(p, is_video, w, h, d, blur_sigma, ff, tmpdir,
                                     blur=blur, tag=f"g{gi}s{seg_pos}", fit=fit,
                                     motion=None if is_video else clip_motion,
                                     strength=motion_strength))
        all_fits.extend(group_fits)
        suffix = f"_{gi + 1}" if len(groups) > 1 else ""
        final = os.path.join(out_dir, f"合成视频{suffix}.mp4")
        main_path = os.path.join(tmpdir, f"main_{gi}.mp4")
        used_transitions = []
        t_eff = min(max(t_dur, 0.2), d * 0.9)  # 防止转场≥片段时长导致非法
        if m == 1:
            # 单段直接复制为成品（统一封装）
            subprocess.run([ff, "-y", "-i", clips[0], "-an", "-c:v", "libx264",
                            "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                            "-movflags", "+faststart", main_path],
                           check=True, capture_output=True, timeout=600)
        else:
            start = sum(transition_counts[:gi])
            preset = transition_seq[start:start + transition_counts[gi]]
            fc, _, used_transitions = build_xfade_filter(m, transitions, t_eff, d, preset=preset)
            cmd = [ff, "-y"]
            for c in clips:
                cmd += ["-i", c]
            cmd += ["-filter_complex", fc, "-map", "[vout]", "-an", "-r", "30",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", main_path]
            _run_ff(cmd, 900, ff, tmpdir, f"xfade_g{gi + 1}")

        # 片头定格封面：现成封面图优先（每条一张、互不重复）；否则按关键帧抽帧。
        # 两种来源都「合入视频」，不再单独导出 jpg。
        main_dur = m * d - (m - 1) * (t_eff if m > 1 else 0)
        cover_img = None
        cover_src_kind = "none"
        if assigned_covers:
            cover_img = assigned_covers[gi]
            cover_src_kind = "custom"
        elif cover in ("first", "middle", "last"):
            ss = (0.0 if cover == "first" else
                  max(0.0, main_dur - 0.1) if cover == "last" else
                  max(0.0, main_dur / 2))
            cover_img = os.path.join(tmpdir, f"frame_{gi}.jpg")
            subprocess.run([ff, "-y", "-ss", f"{ss:.3f}", "-i", main_path,
                            "-frames:v", "1", "-q:v", "2", cover_img],
                           check=True, capture_output=True, timeout=120)
            cover_src_kind = "frame"

        if cover_img and os.path.exists(cover_img):
            cw, ch = probe_dimensions(ff, cover_img)
            cblur = needs_blur((cw, ch), w, h)
            cover_motion = (random.choice(MOTION_CHOICES)
                            if motion == "random_many" else motion)
            cover_clip = prepare_clip(cover_img, False, w, h, cover_duration,
                                      blur_sigma, ff, tmpdir, blur=cblur, tag=f"cover{gi}",
                                      motion=cover_motion, strength=motion_strength)
            concat_cover_clip(ff, cover_clip, main_path, final)
            cover_applied += 1
            total_dur = main_dur + cover_duration
        else:
            shutil.move(main_path, final)
            total_dur = main_dur

        # 配音 + 字幕烧录 + 背景乐混音：未添加则完全跳过，保持原有无音轨成品
        mix_meta = None
        bgm_name = ""
        if assigned_scripts or assigned_bgm:
            script, voice_path = assigned_scripts[gi] if assigned_scripts else ({}, "")
            bgm_path = assigned_bgm[gi] if assigned_bgm else ""
            bgm_name = os.path.basename(bgm_path) if bgm_path else ""
            real_dur = probe_duration(ff, final) or total_dur
            if progress_cb:
                _pct_s = int(8 + (gi / len(groups)) * 80 + (80 / len(groups)) * 0.70)
                progress_cb(stage=f"第 {gi + 1} 条：烧录字幕 / 混入配音与背景乐", percent=_pct_s)
            mixed = os.path.join(tmpdir, f"mixed_{gi}.mp4")
            mix_meta = apply_script_and_bgm(
                ff, final, mixed, w, h, real_dur, vtpl, script,
                voice_path, bgm_path, v, tmpdir, tag=f"g{gi}")
            _move_overwrite(mixed, final)   # 临时目录(C盘)→作品目录(可能E盘)，需跨盘安全覆盖
            total_dur = real_dur

        outputs.append(os.path.basename(final))
        videos_meta.append({"index": gi + 1, "clips": m,
                            "duration": round(total_dur, 2),
                            "cover_source": cover_src_kind,
                            "transitions": used_transitions,
                            "script": mix_meta, "bgm": bgm_name,
                            "sources": group_fits})

    # 收尾处理（写入元数据、清理临时目录前）：进度接近完成，留给前端倒计时收口
    if progress_cb:
        progress_cb(stage="收尾处理中…", percent=95)

    # 成功后累加原创度使用量（跨任务复用）
    commit_usage(fps)
    fit_counts = {}
    for x in all_fits:
        fit_counts[x["mode"]] = fit_counts.get(x["mode"], 0) + 1

    # 清理临时目录
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass

    return {"ok": True, "outputs": outputs, "originality": detail,
            "video_meta": {"width": w, "height": h, "video_count": len(groups),
                           "total_clips": n_total,
                           "transitions_pool": transitions,
                           "cover_mode": "opening",
                           "cover_duration": cover_duration if cover_applied else 0,
                           "cover_applied": cover_applied,
                           "cover_source": "custom" if assigned_covers else ("frame" if cover in ("first", "middle", "last") else "none"),
                           "cover_count": len(cover_paths),
                           "cover_reused": cover_reused,
                           "blur_applied": blur_used, "blur_total": blur_total,
                           "audio_stripped": audio_stripped,
                           "short_video_mode": short_mode,
                           "min_mix_segment": MIN_MIX_SEGMENT,
                           "mix_mode": mix_mode,
                           "reshuffle": reshuffle,
                           "segment_count": seg_count if repeat_mode else None,
                           "segment_duration": d_repeat if repeat_mode else None,
                           "target_duration": target if repeat_mode else None,
                           "segment_sequences": [[i + 1 for i in g] for g in groups] if repeat_mode else None,
                           "video_only_rule": rule_info,
                           "clip_fit": fit_counts,
                           "clip_fit_detail": all_fits,
                           "script_pairs": pair_info,
                           "scripts": {"count": len(scripts),
                                       "reused": scripts_reused,
                                       "sources": list(dict.fromkeys(
                                           [str(x.get("source") or "manual") for x in scripts])),
                                       "subtitle_lines": sum(len(_segment_subtitle(x.get("subtitle", ""))) for x in scripts),
                                       "has_title": any(str(x.get("title") or "").strip() for x in scripts)},
                           "template": {"name": vtpl.get("name") or "内置默认模板",
                                        "id": vtpl.get("id", ""),
                                        "subtitle_enabled": vtpl.get("subtitle_enabled", True),
                                        "title_enabled": vtpl.get("title_enabled", True)},
                           "bgm": {"count": len(bgm_paths), "reused": bgm_reused,
                                   "volume": v.get("bgm_volume", 0.2),
                                   "fade": v.get("bgm_fade", 0.6)},
                           "videos": videos_meta}}
