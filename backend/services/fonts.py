"""字体自动加载服务（应用字体优先，系统字体仅作空目录兜底）。

- 优先只扫描 backend/fonts/：把 .ttf/.otf/.ttc 放进该目录即自动出现在前端字体下拉；
- 应用目录中有字体时，系统字体一个都不加载（列表干净、渲染结果可控）；
- 应用目录为空时，才从系统字体中挑选最常用的 10 个作为兜底，避免中文渲染失败；
- 目录内容变化（新增 / 删除 / 替换）通过文件签名自动重新扫描，无需重启服务；
- 兼容别名「思源黑体」保留：指向当前默认字体，
  保证历史模板 / 默认配置里保存的该名称仍可正常渲染。
"""
import os
from pathlib import Path

from PIL import ImageFont

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
FONT_EXTS = (".ttf", ".otf", ".ttc")
DEFAULT_ALIAS = "思源黑体"

# 仅当应用字体目录为空时启用的系统兜底
SYSTEM_FONT_DIRS = [
    Path("C:/Windows/Fonts"),
    Path.home() / "Library" / "Fonts",
    Path("/usr/share/fonts"),
]
FALLBACK_COUNT = 10
# 兜底挑选顺序：常用中文字体优先，其次常用西文字体；不足 10 个再按字母序补齐
FALLBACK_PREFER = (
    "msyh", "simhei", "simsun", "deng", "simkai", "simfang", "msyhbd",
    "arial", "times", "calibri", "verdana", "georgia", "tahoma",
    "segoeui", "impact", "cour", "trebuc", "cambria", "consola", "comic",
)

_REGISTRY = None
_SIGNATURE = None


def _signature():
    """应用字体目录签名：文件名 + 大小 + 修改时间。

    registry() 每次调用都会比对，新增或替换字体后无需重启服务即可生效。
    """
    try:
        if not FONT_DIR.exists():
            return ()
        return tuple(sorted(
            (p.name, p.stat().st_size, int(p.stat().st_mtime))
            for p in FONT_DIR.iterdir()
            if p.suffix.lower() in FONT_EXTS
        ))
    except OSError:
        return ()


def _system_fonts(limit: int = FALLBACK_COUNT) -> dict:
    """应用字体目录为空时的兜底：从系统字体里挑最常用的 limit 个。

    先按 FALLBACK_PREFER 顺序取，不足再按字母序补齐；返回 {字体名: 路径}。
    """
    pool = {}
    for d in SYSTEM_FONT_DIRS:
        if not d.exists():
            continue
        try:
            for p in d.rglob("*"):
                if p.is_file() and p.suffix.lower() in FONT_EXTS:
                    pool.setdefault(p.stem.lower(), (p.stem, str(p)))
        except OSError:
            continue
    picked = {}
    for key in FALLBACK_PREFER:
        if len(picked) >= limit:
            break
        if key in pool:
            name, path = pool[key]
            picked[name] = path
    if len(picked) < limit:
        used = {n.lower() for n in picked}
        for key in sorted(pool):
            if len(picked) >= limit:
                break
            if key not in used:
                name, path = pool[key]
                picked[name] = path
    return picked


def _is_app_font(path: str) -> bool:
    try:
        return Path(path).parent.resolve() == FONT_DIR.resolve()
    except OSError:
        return False


def _scan() -> dict:
    """构建字体注册表：应用目录有字体就只用它；为空才回退 10 个系统字体。"""
    found = {}
    if FONT_DIR.exists():
        for p in sorted(FONT_DIR.iterdir()):
            if p.suffix.lower() in FONT_EXTS:
                found.setdefault(p.stem, str(p))
    if found:
        return found
    return _system_fonts()


def registry(force: bool = False) -> dict:
    """返回注册表；目录签名变化或 force=True 时重新扫描。"""
    global _REGISTRY, _SIGNATURE
    sig = _signature()
    if force or _REGISTRY is None or sig != _SIGNATURE:
        _REGISTRY = _scan()
        _SIGNATURE = sig
    return _REGISTRY


def refresh() -> dict:
    """强制立即重扫（前端「刷新字体」按钮调用）。"""
    return registry(force=True)


def suggest_default() -> str:
    """默认字体：优先思源 / Noto，否则取应用字体目录中的第一个。"""
    reg = registry()
    for key in reg:
        if "sourcehan" in key.lower() or "noto" in key.lower() or "思源" in key:
            return key
    return next(iter(reg), "")


def list_fonts() -> list:
    """前端下拉列表：应用字体优先，系统字体仅在应用目录为空时兜底出现。

    首项为兼容别名「思源黑体」（指向默认字体），其后列出全部真实字体名；
    历史模板里保存的系统字体名由 resolve_path 回退到默认字体，不会报错。
    """
    reg = registry()
    default = suggest_default()
    out = []
    if default:
        out.append({"name": DEFAULT_ALIAS, "path": reg[default],
                    "is_default": True, "source": "app",
                    "label": f"{DEFAULT_ALIAS}（默认 · {default}）"})
    for name, path in sorted(reg.items()):
        is_app = _is_app_font(path)
        label = name + ("（默认字体）" if name == default else "")
        if not is_app:
            label += "（系统兜底）"
        out.append({"name": name, "path": path, "is_default": False,
                    "source": "app" if is_app else "system", "label": label})
    return out


def resolve_path(name: str):
    """字体名 → 路径。未知名称（含历史系统字体名）统一回退到应用默认字体。"""
    reg = registry()
    if name and name in reg:
        return reg[name]
    d = suggest_default()
    return reg.get(d) if d else None


def get_font(name: str, size: int):
    p = resolve_path(name)
    if p and os.path.exists(p):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    # 应用字体目录为空 / 全部损坏时的最后兜底（不进入字体列表）
    return ImageFont.load_default()
