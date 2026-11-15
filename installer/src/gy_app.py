# -*- coding: utf-8 -*-
"""光影工坊 · 主程序入口（一个 exe 承担全部角色）。

  光影工坊.exe                       手动启动：起服务 + 打开浏览器（已运行则只打开）
  光影工坊.exe --autostart           开机自启：静默起服务，不弹浏览器
  光影工坊.exe --manager             打开「启动设置」控制台（浏览器本地界面）
  光影工坊.exe --manager-headless    只启动控制台 HTTP 服务（自动化验证用）
  光影工坊.exe --stop / --restart / --status
  光影工坊.exe --uninstall           弹原生确认框后卸载
  光影工坊.exe --uninstall-silent [--keep-data]   静默卸载（批量部署 / 自动化）
"""
import ctypes
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import common as C
import webui

_CREATE_NO_WINDOW = 0x08000000
MB_ICONERROR = 0x10
MB_ICONQUESTION = 0x20
MB_YESNO = 0x04
IDYES = 6


def pages_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "pages"
    return Path(__file__).resolve().parent / "pages"


def _message(title, msg, flags=MB_ICONERROR):
    """系统原生对话框，不依赖任何 GUI 库。"""
    try:
        return ctypes.windll.user32.MessageBoxW(0, str(msg), str(title), flags)
    except Exception:
        return 0


def _alert(title, msg):
    _message(title, msg, MB_ICONERROR)


# ------------------------------ 卸载 ------------------------------
def do_uninstall(keep_data: bool = True) -> None:
    C.stop_server()
    C.set_autostart(False)
    try:
        if C.firewall_added():
            C.firewall_remove()
    except Exception:
        pass
    C.remove_shortcuts()

    d = C.install_dir()
    if keep_data:
        try:
            backup = d.parent / "光影工坊_数据备份"
            backup.mkdir(parents=True, exist_ok=True)
            for sub in ("config", "space", "data", "output"):
                src = C.app_root() / sub
                if not src.exists():
                    continue
                dst = backup / sub
                if dst.exists():
                    dst = backup / ("%s_%s" % (sub, time.strftime("%Y%m%d_%H%M%S")))
                shutil.move(str(src), str(dst))
        except Exception:
            pass

    # 用 PowerShell 延迟自删除：安装目录可能含中文，PowerShell 处理 Unicode 路径最稳，
    # 且脚本放在系统临时目录，不会被自身删除动作影响。
    try:
        tmp = Path(tempfile.gettempdir()) / ("gy_uninst_%d" % int(time.time()))
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "target.txt").write_text(str(d), encoding="utf-8")
        ps = tmp / "cleanup.ps1"
        ps.write_text(
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "$p = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'target.txt') "
            "-Encoding UTF8 -Raw).Trim()\n"
            "Start-Sleep -Seconds 2\n"
            "for ($i = 0; $i -lt 20; $i++) {\n"
            "  Remove-Item -LiteralPath $p -Recurse -Force\n"
            "  if (-not (Test-Path -LiteralPath $p)) { break }\n"
            "  Start-Sleep -Milliseconds 500\n"
            "}\n"
            "Remove-Item -LiteralPath $PSScriptRoot -Recurse -Force\n",
            encoding="utf-8-sig",
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", str(ps)],
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def show_uninstall():
    r = _message(
        "卸载光影工坊",
        "确定要卸载「光影工坊」吗？\n\n"
        "点「是」：保留我的作品与配置（自动备份到安装目录上级的"
        "「光影工坊_数据备份」文件夹）\n"
        "点「否」：放弃卸载\n\n"
        "如需完全删除数据，请在卸载后手动删除该备份文件夹。",
        MB_ICONQUESTION | MB_YESNO,
    )
    if r != IDYES:
        return
    do_uninstall(keep_data=True)
    _message("卸载完成", "光影工坊已卸载。\n程序文件将在几秒内自动清除。", 0x40)


# ------------------------------ 启动设置控制台 ------------------------------
_FW_CACHE = {"t": 0.0, "v": False}


def firewall_ok() -> bool:
    if time.time() - _FW_CACHE["t"] > 15:
        try:
            _FW_CACHE["v"] = C.firewall_added()
        except Exception:
            _FW_CACHE["v"] = False
        _FW_CACHE["t"] = time.time()
    return _FW_CACHE["v"]


def start_manager_web(open_browser: bool = True):
    import webbrowser
    port_file = C.install_dir() / "manager.port"
    if webui.probe_port(port_file):
        try:
            port = int(port_file.read_text(encoding="utf-8").strip())
            if open_browser:
                webbrowser.open("http://127.0.0.1:%d/" % port)
        except Exception:
            pass
        return

    pending = {"uninstall": False}
    ui_holder = {}

    def status():
        return {
            "ok": True,
            "running": C.is_running(),
            "url": C.local_url(),
            "port": C.read_port(),
            "ips": C.lan_ips(),
            "autostart": C.autostart_enabled(),
            "firewall": firewall_ok(),
            "dir": str(C.install_dir()),
        }

    def action(name, value=None):
        try:
            if name == "start":
                ok, msg = C.start_server()
                return {"ok": ok, "msg": msg}
            if name == "stop":
                ok, msg = C.stop_server()
                return {"ok": ok, "msg": msg}
            if name == "restart":
                ok, msg = C.restart_server()
                return {"ok": ok, "msg": msg}
            if name == "open":
                C.open_browser()
                return {"ok": True, "msg": "已打开应用"}
            if name == "folder":
                C.open_folder(C.install_dir())
                return {"ok": True, "msg": "已打开安装目录"}
            if name == "log":
                C.open_log()
                return {"ok": True, "msg": "已打开日志"}
            if name == "firewall":
                ok, msg = C.firewall_allow()
                _FW_CACHE["t"] = 0.0
                return {"ok": ok, "msg": ("已放行 TCP %d" % C.read_port()) if ok else msg}
            if name == "autostart":
                ok, msg = C.set_autostart(bool(value))
                return {"ok": ok, "msg": msg}
            if name == "port":
                p = int(value)
                if not (1 <= p <= 65535):
                    return {"ok": False, "msg": "端口需在 1-65535 之间"}
                ok = C.write_port(p)
                return {"ok": ok, "msg": "端口已改为 %d，重启服务后生效" % p if ok else "保存失败"}
            if name == "uninstall":
                pending["uninstall"] = True
                if ui_holder.get("ui"):
                    ui_holder["ui"].quit()
                return {"ok": True, "msg": "正在卸载…"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": False, "msg": "未知操作"}

    def api(act, data):
        if act == "status":
            return status()
        if act == "action":
            return action(data.get("name"), data.get("value"))
        if act == "quit":
            if ui_holder.get("ui"):
                ui_holder["ui"].quit()
            return {"ok": True}
        return {"ok": False, "error": "未知操作"}

    ui = webui.WebUI(pages_dir() / "manager.html", api,
                     idle_timeout=900, port_file=port_file)
    ui_holder["ui"] = ui
    ui.run(open_browser=open_browser)
    if pending["uninstall"]:
        do_uninstall(keep_data=True)


# ------------------------------ 入口 ------------------------------
def main():
    a = [x.lower() for x in sys.argv[1:]]
    if "--manager-headless" in a:
        start_manager_web(open_browser=False)
    elif "--manager" in a or "--settings" in a:
        start_manager_web(open_browser=True)
    elif "--uninstall-silent" in a:
        do_uninstall(keep_data="--keep-data" in a)
    elif "--uninstall" in a:
        show_uninstall()
    elif "--stop" in a:
        C.stop_server()
    elif "--restart" in a:
        C.restart_server()
    elif "--autostart" in a:
        C.start_server(wait=False)
    elif "--status" in a:
        try:
            C.log_dir().mkdir(parents=True, exist_ok=True)
            (C.log_dir() / "status.txt").write_text(
                "running=%s\nurl=%s\nport=%d\n" %
                (C.is_running(), C.local_url(), C.read_port()), encoding="utf-8")
        except Exception:
            pass
    else:
        ok, msg = C.start_server(wait=True)
        if ok:
            C.open_browser()
        else:
            _alert("光影工坊启动失败", "%s\n\n可在「光影工坊 · 启动设置」里查看日志。" % msg)


if __name__ == "__main__":
    main()
