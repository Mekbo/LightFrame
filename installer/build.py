# -*- coding: utf-8 -*-
"""光影工坊 · 阶段 6 一键构建脚本。

产出：output/光影工坊_一键安装包_v1.0.exe（单文件、双击即装、无需 Python / 联网 / 命令行）

构建内容：
  1. 生成红金应用图标
  2. 组装自包含运行时 runtime/（独立 Python 3.13 + 精简依赖）
  3. 组装干净应用 app/（代码 + 字体 + 静态页 + 内置 ffmpeg，零用户数据）
  4. 编译 光影工坊.exe（启动器 / 启动设置 / 卸载 三合一）
  5. 打包 payload.zip
  6. 编译单文件安装向导 EXE
"""
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INS = Path(__file__).resolve().parent
SRC = INS / "src"
BUILD = INS / "build"
PAYLOAD = BUILD / "payload"
EXE_OUT = BUILD / "exe"
WORK = BUILD / "work"
SPEC = BUILD / "spec"
DIST = INS / "dist"
OUT = ROOT / "output"

PY_SRC = Path(sys.prefix).resolve()
DEPS = [
    "fastapi==0.162.2",
    "uvicorn==0.54.0",
    "pillow==13.0.0",
    "pillow-heif==1.8.1",
    "httpx==0.28.1",
    "python-multipart==0.0.33",
]
APP_EXE_NAME = "光影工坊"
SETUP_EXE_NAME = "光影工坊_一键安装包_v1.0"
EMPTY_DIRS = [
    "app/config", "app/config/users", "app/data", "app/space", "app/output",
    "app/tools/ffmpeg", "app/backend/templates", "app/backend/video_templates",
    "app/backend/prompt_templates", "logs",
]


def log(msg):
    print("[构建] %s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def reset():
    if BUILD.exists():
        shutil.rmtree(BUILD, ignore_errors=True)
    for d in (PAYLOAD, EXE_OUT, WORK, SPEC, DIST, OUT):
        d.mkdir(parents=True, exist_ok=True)


def make_icon(path: Path):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(256):
        t = y / 255
        d.line([(0, y), (256, y)],
               fill=(int(158 + 54 * t), int(21 + 48 * t), int(27 + 31 * t), 255))
    mask = Image.new("L", (256, 256), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 255, 255), radius=52, fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    d.ellipse((20, 20, 236, 236), outline=(225, 197, 104, 255), width=7)
    font = None
    for fp in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
               "C:/Windows/Fonts/simhei.ttf"):
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, 148)
                break
            except Exception:
                pass
    if font:
        d.text((128, 126), "光", font=font, fill=(255, 255, 255, 255), anchor="mm")
    else:
        d.ellipse((88, 88, 168, 168), fill=(255, 255, 255, 255))
    img.save(path, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    log("图标生成：%s" % path.name)


def build_runtime():
    rt = PAYLOAD / "runtime"
    rt.mkdir(parents=True, exist_ok=True)
    keep_files = ["python.exe", "pythonw.exe", "python3.exe", "python3.dll",
                  "python313.dll", "vcruntime140.dll", "vcruntime140_1.dll",
                  "LICENSE.txt"]
    for n in keep_files:
        p = PY_SRC / n
        if p.exists():
            shutil.copy2(p, rt / n)
    for sub in ("DLLs", "Lib"):
        src = PY_SRC / sub
        dst = rt / sub
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
            "__pycache__", "site-packages", "*.pyc", "*.pyo"), dirs_exist_ok=True)
    target = rt / "Lib" / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--target", str(target),
           "--only-binary=:all:", "--no-compile", "--disable-pip-version-check",
           "--quiet"] + DEPS
    log("安装运行依赖（首次需联网下载 wheel，约 1-3 分钟）…")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("依赖安装失败：\n%s" % (r.stderr or r.stdout)[-3000:])
    log("运行依赖就绪：%s" % ", ".join(p.name for p in target.iterdir()
                                    if p.is_dir() and not p.name.endswith("dist-info"))[:180])
    # 清理 pip 装入的可执行脚本与缓存，减小体积
    for d in ("bin", "Scripts"):
        shutil.rmtree(target / d, ignore_errors=True)
    return rt


def build_app():
    app = PAYLOAD / "app"
    app.mkdir(parents=True, exist_ok=True)

    def copy_sources(src: Path, dst: Path, exts=None, skip_json_data=False):
        dst.mkdir(parents=True, exist_ok=True)
        for p in src.iterdir():
            if p.name in ("__pycache__",) or p.suffix in (".pyc", ".pyo"):
                continue
            if skip_json_data and p.name.endswith(".json"):
                continue
            if p.is_dir():
                copy_sources(p, dst / p.name, exts, skip_json_data)
            elif exts is None or p.suffix.lower() in exts:
                shutil.copy2(p, dst / p.name)

    # 后端代码：只带 .py 与内置字体，模板数据一律不带（全新安装从空开始）
    backend = app / "backend"
    for sub in ("api", "config", "core", "services", "fonts"):
        s = ROOT / "backend" / sub
        if s.exists():
            copy_sources(s, backend / sub,
                         exts=None if sub == "fonts" else {".py"})
    for f in ("__init__.py", "run.py", "main.py"):
        p = ROOT / "backend" / f
        if p.exists():
            shutil.copy2(p, backend / f)
    # 模板目录留空（应用会按需创建 / 读取）
    for sub in ("templates", "video_templates", "prompt_templates"):
        (backend / sub).mkdir(parents=True, exist_ok=True)

    (app / "static").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "static" / "index.html", app / "static" / "index.html")

    # ffmpeg：直接用已验证的 imageio-ffmpeg 内置二进制，放到应用首选路径
    import imageio_ffmpeg
    ff = Path(imageio_ffmpeg.get_ffmpeg_exe())
    (app / "tools" / "ffmpeg").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ff, app / "tools" / "ffmpeg" / "ffmpeg.exe")
    log("内置 ffmpeg：%.1f MB" % (ff.stat().st_size / 1024 / 1024))

    for d in EMPTY_DIRS:
        (PAYLOAD / d).mkdir(parents=True, exist_ok=True)

    (PAYLOAD / "使用说明.txt").write_text(USAGE_TEXT, encoding="utf-8")
    shutil.copy2(BUILD / "app.ico", PAYLOAD / "app.ico")
    return app


def verify_payload():
    py = PAYLOAD / "runtime" / "python.exe"
    r = subprocess.run([str(py), "-c",
                        "import sys,fastapi,uvicorn,PIL,pillow_heif,httpx,multipart;"
                        "print(sys.version.split()[0]);print('deps-ok')"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("运行时自检失败：\n%s" % (r.stderr or r.stdout)[-2000:])
    log("运行时自检通过：%s" % (r.stdout or "").strip().replace("\n", " / "))

    app = PAYLOAD / "app"
    r = subprocess.run([str(py), "-c",
                        "import backend.main;print('app-import-ok')"],
                       cwd=str(app), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("应用导入自检失败：\n%s" % (r.stderr or r.stdout)[-3000:])
    # 自检过程若产生任何配置 / 数据，一律清除：安装包必须是「全新未初始化」状态，
    # 且不能携带构建机绝对路径（输出目录基于目标机器 Path.home() 动态计算）。
    cleaned = []
    for sub in ("config", "data", "space", "output"):
        d = PAYLOAD / "app" / sub
        if not d.exists():
            continue
        for f in list(d.rglob("*")):
            if f.is_file():
                f.unlink()
                cleaned.append(str(f.relative_to(PAYLOAD)))
    if cleaned:
        log("清理自检残留 %d 个文件：%s" % (len(cleaned), ", ".join(cleaned[:5])))
    log("应用导入自检通过（干净配置）")


def pages_arg():
    """把浏览器界面页打进 EXE（安装向导 / 启动设置各一份）。"""
    return ["--add-data", "%s;pages" % (SRC / "pages")]


def compile_pyi(script: Path, name: str, extra=None):
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--onefile", "--windowed", "--clean",
           "--name", name, "--icon", str(BUILD / "app.ico"),
           "--distpath", str(EXE_OUT), "--workpath", str(WORK),
           "--specpath", str(SPEC), "--paths", str(SRC)]
    if extra:
        cmd += extra
    cmd.append(str(script))
    log("编译 %s.exe …" % name)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(INS))
    exe = EXE_OUT / (name + ".exe")
    if r.returncode != 0 or not exe.exists():
        raise RuntimeError("编译 %s 失败：\n%s" % (name, (r.stderr or r.stdout)[-4000:]))
    log("编译完成：%s.exe  %.1f MB" % (name, exe.stat().st_size / 1024 / 1024))
    return exe


def make_payload_zip() -> Path:
    zp = BUILD / "payload.zip"
    files = [p for p in PAYLOAD.rglob("*") if p.is_file()]
    log("打包 payload.zip（%d 个文件）…" % len(files))
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for d in EMPTY_DIRS:
            z.writestr(d.rstrip("/") + "/", b"")
        for p in files:
            z.write(p, p.relative_to(PAYLOAD).as_posix())
    log("payload.zip：%.1f MB" % (zp.stat().st_size / 1024 / 1024))
    return zp


def main():
    t0 = time.time()
    log("=== 阶段 6 · 构建一键安装包 ===")
    reset()
    make_icon(BUILD / "app.ico")
    build_runtime()
    build_app()
    verify_payload()

    app_exe = compile_pyi(SRC / "gy_app.py", APP_EXE_NAME, extra=pages_arg())
    shutil.copy2(app_exe, PAYLOAD / (APP_EXE_NAME + ".exe"))
    log("主程序已放入 payload")

    zp = make_payload_zip()
    setup = compile_pyi(SRC / "gy_setup.py", SETUP_EXE_NAME,
                        extra=["--add-data", "%s;." % zp] + pages_arg())
    final = OUT / (SETUP_EXE_NAME + ".exe")
    shutil.copy2(setup, final)
    DIST.mkdir(parents=True, exist_ok=True)
    shutil.copy2(zp, DIST / "payload.zip")
    log("最终交付：%s  %.1f MB" % (final, final.stat().st_size / 1024 / 1024))
    log("总耗时 %.1f 秒" % (time.time() - t0))
    return final


USAGE_TEXT = """光影工坊 · 使用说明
==================================================

【这是什么】
单一安装包，不区分客户端 / 服务端。装在哪台电脑，那台电脑就是服务端；
本机自己用 localhost，其他手机 / 电脑通过局域网地址访问同一台机器。

【首次使用】
1. 双击桌面「光影工坊」→ 服务自动启动并打开浏览器。
2. 首次打开请按页面引导设置管理员账号和密码。
3. 之后每次双击即可，无需命令行、无需 Python、无需联网。

【手动启动 / 开机自启】
打开桌面「光影工坊 · 启动设置」：
  · 启动 / 停止 / 重启：即时控制服务
  · 开机自动启动：勾选后，登录 Windows 即在后台静默启动（不弹窗口）
  · 关闭勾选即可取消，随时可切换，无需管理员权限

【局域网 / 在线使用】
1. 在「启动设置」里点一次「放行防火墙」（可能需要一次管理员授权）。
2. 把面板里显示的「局域网地址」发给同一网络下的其他设备即可访问。
3. 若要从外网访问，请自行在路由器上做端口映射（默认 TCP 2026），
   映射哪台机器，哪台机器就是服务端。

【卸载】
「启动设置」→ 卸载，或开始菜单「卸载光影工坊」。
可选择保留「我的空间」与配置（自动备份到安装目录上级的
「光影工坊_数据备份」文件夹），也可完全删除。

【目录说明】
  runtime/   独立 Python 运行环境（不影响系统已装的 Python）
  app/       应用代码与资源；app/space、app/config 为你的数据
  logs/      服务运行日志（排查问题时查看 server.log）

【注意】
  · 请勿手动删除 runtime 目录。
  · 升级安装会保留 app/space、app/config、app/data 与 logs。
  · 端口可在「启动设置」中修改，改完重启服务生效。
"""


def rebuild_exes_only():
    """增量重建：复用已组装好的 runtime / app，仅重编译 EXE 与重打包。"""
    t0 = time.time()
    log("=== 增量重建（仅重编译 EXE 与打包）===")
    if not (BUILD / "app.ico").exists():
        make_icon(BUILD / "app.ico")
    app_exe = compile_pyi(SRC / "gy_app.py", APP_EXE_NAME, extra=pages_arg())
    shutil.copy2(app_exe, PAYLOAD / (APP_EXE_NAME + ".exe"))
    zp = make_payload_zip()
    setup = compile_pyi(SRC / "gy_setup.py", SETUP_EXE_NAME,
                        extra=["--add-data", "%s;." % zp] + pages_arg())
    final = OUT / (SETUP_EXE_NAME + ".exe")
    DIST.mkdir(parents=True, exist_ok=True)
    shutil.copy2(setup, final)
    shutil.copy2(zp, DIST / "payload.zip")
    log("最终交付：%s  %.1f MB" % (final, final.stat().st_size / 1024 / 1024))
    log("总耗时 %.1f 秒" % (time.time() - t0))
    return final


if __name__ == "__main__":
    try:
        f = rebuild_exes_only() if "--exes-only" in sys.argv else main()
        print("\nBUILD_OK=%s" % f)
    except Exception as e:
        print("\nBUILD_FAILED=%s" % e, file=sys.stderr)
        sys.exit(1)
