# -*- coding: utf-8 -*-
"""光影工坊 · 一键安装程序（单文件 EXE）。

双击 → 自动打开浏览器显示中文安装向导 → 选目录 / 勾选项 → 自动安装 → 完成。
界面用内置本地 Web 服务呈现，不依赖 tkinter 或任何第三方 GUI 库。

不区分客户端 / 服务端：装在哪台机器，开启局域网访问后那台机器就是服务端。

静默参数（供自动化验证 / 批量部署，不启动界面）：
  --silent --dir=<路径> [--autostart] [--lan] [--shortcut] [--launch] [--result=<文件>]
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import common as C
import webui

CREATE_NO_WINDOW = 0x08000000
APP = "光影工坊"
DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP
PRESERVE = ("app/config", "app/space", "app/data", "app/output", "logs")


def pages_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "pages"
    return Path(__file__).resolve().parent / "pages"


def payload_zip() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "payload.zip"
    return Path(__file__).resolve().parent.parent / "build" / "payload.zip"


def default_dir() -> Path:
    return DEFAULT_DIR


def free_bytes(path: Path) -> int:
    try:
        p = path
        while not p.exists() and p.parent != p:
            p = p.parent
        return shutil.disk_usage(str(p)).free
    except Exception:
        return 10 ** 12


def _stop_old_service(target: Path) -> None:
    exe = target / C.EXE_NAME
    if exe.exists():
        try:
            subprocess.run([str(exe), "--stop"], timeout=20,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass


def _is_preserved(name: str) -> bool:
    n = name.replace("\\", "/").lstrip("/")
    return any(n == p or n.startswith(p + "/") for p in PRESERVE)


def install_files(target: Path, progress=None) -> tuple:
    """解压 payload 到目标目录；升级时保留用户数据目录与日志。"""
    zp = payload_zip()
    if not zp.exists():
        return False, "安装包数据缺失（payload.zip）"
    target.mkdir(parents=True, exist_ok=True)
    _stop_old_service(target)
    try:
        with zipfile.ZipFile(zp, "r") as z:
            infos = z.infolist()
            for m in infos:
                if m.is_dir():
                    (target / m.filename.replace("\\", "/")).mkdir(parents=True, exist_ok=True)
            members = [m for m in infos if not m.is_dir()]
            total = len(members) or 1
            for i, m in enumerate(members, 1):
                name = m.filename.replace("\\", "/")
                dst = target / name
                if _is_preserved(name) and dst.exists():
                    continue  # 升级：绝不覆盖用户数据
                dst.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 512)
                if progress and (i % 12 == 0 or i == total):
                    progress(i / total)
        for sub in ("runtime/Lib/site-packages", "app/config", "app/config/users",
                    "app/data", "app/space", "app/output", "logs"):
            (target / sub).mkdir(parents=True, exist_ok=True)
        return True, "文件安装完成"
    except Exception as e:
        return False, "安装失败：%s" % e


def _setup_autostart(target: Path, on: bool) -> None:
    """指向已安装的 光影工坊.exe，而不是安装器自身。"""
    import winreg
    exe = target / C.EXE_NAME
    try:
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, C.REG_RUN_KEY)
        try:
            if on:
                winreg.SetValueEx(k, C.REG_RUN_VALUE, 0, winreg.REG_SZ,
                                  '"%s" --autostart' % exe)
            else:
                try:
                    winreg.DeleteValue(k, C.REG_RUN_VALUE)
                except FileNotFoundError:
                    pass
        finally:
            k.Close()
    except Exception:
        pass


def _setup_firewall(port: int = 2026) -> None:
    name = "%s %d" % (C.FW_RULE_NAME, port)
    line = ('netsh advfirewall firewall add rule name="%s" '
            'dir=in action=allow protocol=TCP localport=%d profile=any enable=yes'
            % (name, port))
    try:
        if C.is_admin():
            subprocess.run(line, shell=True, capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        else:
            d = Path(tempfile.mkdtemp(prefix="gy_fw_"))
            bat = d / "fw.bat"
            res = d / "r.txt"
            bat.write_text("@echo off\r\n%s\r\necho %%errorlevel%% > \"%s\"\r\n"
                           % (line, res), encoding="ascii")
            ctypes.windll.shell32.ShellExecuteW(None, "runas", str(bat), "", str(d), 0)
    except Exception:
        pass


def _create_links(target: Path, desktop: bool = True) -> None:
    exe = target / C.EXE_NAME
    ico = target / "app.ico"
    sm = C.startmenu_dir()
    sm.mkdir(parents=True, exist_ok=True)
    pairs = [
        (C.LINK_NAME, "", "启动光影工坊并打开应用"),
        (C.LINK_MANAGER_NAME, "--manager", "启动设置 / 开机自启 / 局域网访问 / 卸载"),
        ("卸载光影工坊.lnk", "--uninstall", "卸载光影工坊"),
    ]
    for name, args, desc in pairs:
        C.create_shortcut(sm / name, exe, args, ico, desc, target)
    if desktop:
        d = C.desktop_dir()
        C.create_shortcut(d / C.LINK_NAME, exe, "", ico, "启动光影工坊并打开应用", target)
        C.create_shortcut(d / C.LINK_MANAGER_NAME, exe, "--manager",
                          ico, "启动设置 / 开机自启 / 局域网访问", target)


def do_install(target: Path, options: dict, progress=None, result_file: Path = None) -> tuple:
    """执行完整安装。返回 (成功, 说明)。"""
    try:
        need = payload_zip().stat().st_size * 3 + 200 * 1024 * 1024
        if free_bytes(target) < need:
            return False, "磁盘空间不足，建议至少预留 1 GB"
        ok, msg = install_files(target, progress)
        if not ok:
            return False, msg
        _setup_autostart(target, bool(options.get("autostart")))
        if options.get("shortcut", True):
            _create_links(target, desktop=True)
        if options.get("lan"):
            _setup_firewall(2026)
        if options.get("launch"):
            exe = target / C.EXE_NAME
            if exe.exists():
                subprocess.Popen(
                    [str(exe)], creationflags=CREATE_NO_WINDOW,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True)
        if result_file:
            result_file.write_text(
                json.dumps({"ok": True, "dir": str(target), "url": "http://127.0.0.1:2026",
                            "options": {k: bool(v) for k, v in options.items()}},
                           ensure_ascii=False), encoding="utf-8")
        return True, "安装完成"
    except Exception as e:
        if result_file:
            try:
                result_file.write_text(json.dumps({"ok": False, "error": str(e)},
                                                 ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return False, "安装失败：%s" % e


# ------------------------------ 浏览器向导 ------------------------------
STATE = {"pct": 0, "msg": "准备中…", "done": False, "ok": False,
         "error": "", "dir": "", "ips": [], "port": 2026}
UI = None


def browse_dir() -> tuple:
    """调用系统原生目录选择对话框（PowerShell + WinForms，系统自带）。

    修复点：旧版对话框无 owner、无前台权限，经常被浏览器窗口挡住（用户看到
    「点浏览没反应」），且失败被静默吞掉。现改为：置顶 owner 窗体承载对话框，
    结果文件区分 成功/取消，进程异常返回明确错误。
    返回 (路径或空, 错误信息)。
    """
    try:
        d = Path(tempfile.gettempdir()) / "gy_browse"
        d.mkdir(parents=True, exist_ok=True)
        res = d / "result.txt"
        if res.exists():
            res.unlink()
        ps = d / "browse.ps1"
        # res 内容：OK<TAB>路径 或 CANCEL
        ps.write_text(
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "$owner = New-Object System.Windows.Forms.Form\n"
            "$owner.TopMost = $true\n"
            "$owner.ShowInTaskbar = $false\n"
            "$owner.WindowState = 'Minimized'\n"
            "$owner.Opacity = 0\n"
            "$owner.Show()\n"
            "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog\n"
            "$dlg.Description = '选择光影工坊安装目录'\n"
            "$dlg.ShowNewFolderButton = $true\n"
            "$r = $dlg.ShowDialog($owner)\n"
            "$owner.Close()\n"
            "if ($r -eq [System.Windows.Forms.DialogResult]::OK) {\n"
            "  [System.IO.File]::WriteAllText('%s', 'OK' + [char]9 + $dlg.SelectedPath, "
            "[System.Text.Encoding]::UTF8)\n"
            "} else {\n"
            "  [System.IO.File]::WriteAllText('%s', 'CANCEL', "
            "[System.Text.Encoding]::UTF8)\n"
            "}\n" % (str(res).replace("\\", "/"), str(res).replace("\\", "/")),
            encoding="utf-8-sig",
        )
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy",
                                "Bypass", "-File", str(ps)],
                               timeout=300, creationflags=CREATE_NO_WINDOW)
        except FileNotFoundError:
            return "", "系统 PowerShell 不可用"
        if r.returncode != 0:
            return "", "目录选择窗口启动失败（可能被安全软件拦截）"
        if not res.exists():
            return "", "目录选择窗口没有返回结果"
        txt = res.read_text(encoding="utf-8").strip()
        if txt == "CANCEL" or not txt:
            return "", ""          # 用户主动取消：不算错误，也不给提示
        if txt.startswith("OK\t"):
            return txt[3:].strip(), ""
        return "", "目录选择窗口返回了无法识别的结果"
    except Exception as e:
        return "", "打开目录选择窗口失败：%s" % e


def _drives() -> list:
    """本机固定 / 可移动磁盘盘符（避开空读卡器卡死）。"""
    out = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                d = "%s:\\" % chr(65 + i)
                if d == "A:\\":            # 软驱占位盘符，列出来反而干扰
                    continue
                t = ctypes.windll.kernel32.GetDriveTypeW(d)
                if t in (3, 4):        # 3=固定盘 4=可移动盘
                    out.append(d)
    except Exception:
        pass
    return out


def list_dir(path: str) -> dict:
    """给向导内置目录浏览器用：列盘符 + 目录一级子目录（仅目录）。

    目标目录可能还没创建（默认安装目录就是这种），此时定位到最近一层已存在的
    上级目录打开，并返回 fallback 标记，避免出现「目录不存在」卡死选择器。
    """
    drives = _drives()
    raw = (path or "").strip().strip('"')
    if not raw:
        base = Path.home()
    else:
        if len(raw) == 2 and raw.endswith(":"):
            raw += "\\"
        base = Path(raw)
    fallback = False
    if not base.is_dir():
        climb = base
        while not climb.is_dir() and climb.parent != climb:
            climb = climb.parent
        if not climb.is_dir():
            return {"ok": False, "error": "目录不存在或无法访问：%s" % raw,
                    "drives": drives}
        base, fallback = climb, True
    dirs, err = [], None
    try:
        entries = sorted(base.iterdir(), key=lambda x: x.name.lower())
        for it in entries:
            try:
                if it.is_symlink():          # 跳过链接目录，防环
                    continue
                if os.path.isjunction(str(it)):   # 跳过 junction（如「Application Data」）
                    continue
                if it.name.startswith("$"):       # 跳过回收站等系统目录
                    continue
                if it.is_dir():
                    dirs.append(it.name)
            except OSError:
                continue
            if len(dirs) >= 500:
                break
    except PermissionError:
        err = "没有权限读取该目录"
    except OSError as e:
        err = "读取目录失败：%s" % e
    if err and not dirs:
        return {"ok": False, "error": err, "drives": drives, "path": str(base)}
    parent = os.path.dirname(str(base))
    return {"ok": True, "path": str(base),
            "parent": parent if parent and parent != str(base) else "",
            "dirs": dirs, "drives": drives,
            "home": str(Path.home()),
            "fallback": fallback,
            "requested": raw,
            "truncated": len(dirs) >= 500}


def mkdir_dir(parent: str, name: str) -> dict:
    """在向导目录浏览器里「新建文件夹」。"""
    p = (parent or "").strip().strip('"')
    n = (name or "").strip()
    if not n:
        return {"ok": False, "error": "请填写文件夹名称"}
    for ch in '\\/:*?"<>|':
        n = n.replace(ch, "")
    n = n.strip().lstrip(".")
    if not n:
        return {"ok": False, "error": "文件夹名称不合法"}
    if not p:
        return {"ok": False, "error": "请先进入要新建文件夹的位置"}
    base = Path(p)
    if not base.is_dir():
        return {"ok": False, "error": "上级目录不存在"}
    target = base / n
    try:
        target.mkdir(parents=False, exist_ok=True)
        return {"ok": True, "dir": str(target)}
    except FileExistsError:
        return {"ok": True, "dir": str(target)}
    except Exception as e:
        return {"ok": False, "error": "新建文件夹失败：%s" % e}


def run_wizard(open_browser=True, port_file=None):
    global UI

    def api(action, data):
        if action == "defaults":
            return {"ok": True, "dir": str(default_dir()),
                    "free_gb": round(free_bytes(default_dir()) / 1024 ** 3, 1),
                    "ips": C.lan_ips(), "port": 2026}
        if action == "checkdir":
            t = Path((data.get("dir") or "").strip())
            if not t:
                return {"ok": False, "error": "请填写安装目录"}
            return {"ok": True, "free_gb": round(free_bytes(t) / 1024 ** 3, 1),
                    "exists": t.exists(),
                    "enough": free_bytes(t) >= 800 * 1024 * 1024}
        if action == "browse":
            if data.get("probe"):
                # 自动化验证用：不弹窗，只检查环境可用性
                import shutil as _sh
                return {"ok": False, "dir": "", "error": "", "probe": True,
                        "powershell": bool(_sh.which("powershell"))}
            p, err = browse_dir()
            return {"ok": bool(p), "dir": p, "error": err}
        if action == "listdir":
            return list_dir(data.get("path") or "")
        if action == "mkdir":
            return mkdir_dir(data.get("parent") or "", data.get("name") or "")
        if action == "progress":
            return dict(STATE)
        if action == "install":
            if STATE.get("started"):
                return {"ok": True, "msg": "已在安装中"}
            STATE.update(started=True, done=False, ok=False, error="", pct=0,
                         msg="正在准备…", dir=data.get("dir") or str(default_dir()))
            opts = {"autostart": bool(data.get("autostart")),
                    "shortcut": bool(data.get("shortcut")),
                    "lan": bool(data.get("lan")),
                    "launch": bool(data.get("launch"))}
            target = Path(STATE["dir"])

            def worker():
                def cb(ratio):
                    STATE["pct"] = min(99, int(ratio * 100))
                    STATE["msg"] = "正在安装… %d%%" % STATE["pct"]

                ok, msg = do_install(target, opts, progress=cb)
                STATE.update(done=True, ok=ok, pct=100 if ok else STATE["pct"],
                             msg="安装完成" if ok else msg,
                             error="" if ok else msg, dir=str(target),
                             ips=C.lan_ips(), port=2026)
                # 关键收尾：安装结束后向导不再有存在意义，
                # 即使用户关掉标签页或不点「完成」，也必须在短时间内自行退出，
                # 不能留在任务管理器里等 15 分钟空闲超时、需要手动结束。
                if UI is not None:
                    UI.idle_timeout = 75 if ok else 300

            threading.Thread(target=worker, daemon=True).start()
            return {"ok": True}
        if action == "quit":
            if UI:
                UI.quit()
            return {"ok": True}
        return {"ok": False, "error": "未知操作：" + action}

    UI = webui.WebUI(pages_dir() / "setup.html", api,
                     idle_timeout=900, port_file=port_file)
    UI.run(open_browser=open_browser)
    return 0


def silent_install(argv) -> int:
    opts = {"autostart": "--autostart" in argv,
            "shortcut": "--shortcut" in argv,
            "lan": "--lan" in argv,
            "launch": "--launch" in argv}
    target = default_dir()
    result = None
    for a in argv:
        if a.startswith("--dir="):
            target = Path(a[6:].strip('"'))
        elif a.startswith("--result="):
            result = Path(a[9:].strip('"'))
    ok, msg = do_install(target, opts, result_file=result)
    return 0 if ok else 1


def main():
    # 只把参数名小写，「--dir=」后面的路径必须保留原始大小写
    argv = []
    for a in sys.argv[1:]:
        if "=" in a and a.startswith("--"):
            k, v = a.split("=", 1)
            argv.append(k.lower() + "=" + v)
        else:
            argv.append(a.lower() if a.startswith("--") else a)
    if "--silent" in argv:
        return silent_install(argv)
    if "--wizard-headless" in argv:
        # 自动化验证用：启动向导 HTTP 服务但不打开浏览器
        return run_wizard(open_browser=False,
                          port_file=Path(tempfile.gettempdir()) / "gy_setup_wizard.port")
    return run_wizard()


if __name__ == "__main__":
    _rc = main()
    # 强制立即退出：onefile 引导进程可能被残留的守护线程 / 句柄拖住几秒，
    # 而安装器理应在「完成」后即刻从任务管理器消失。
    os._exit(_rc if isinstance(_rc, int) else 0)
