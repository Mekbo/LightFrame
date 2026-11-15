import shutil
from pathlib import Path
from .config import TOOLS_DIR, load_config


def detect_ffmpeg() -> str:
    """优先用设置里的路径，其次项目内置 tools/ffmpeg，最后系统 PATH。"""
    # 旧版引用了不存在的 config 对象（该模块此前未被导入，属潜在 ImportError）
    p = load_config().get("ffmpeg_path")
    if p and Path(p).exists():
        return p
    for pat in ("ffmpeg.exe", "ffmpeg"):
        hits = list(TOOLS_DIR.rglob(pat))
        if hits:
            return str(hits[0])
    # 开发态兜底：imageio-ffmpeg 自带的二进制
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    sys_path = shutil.which("ffmpeg")
    if sys_path:
        return sys_path
    return ""


def ffmpeg_version() -> str:
    exe = detect_ffmpeg()
    if not exe:
        return "未检测到 ffmpeg"
    import subprocess
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10)
        return out.stdout.splitlines()[0] if out.stdout else "未知"
    except Exception as e:
        return f"检测失败: {e}"
