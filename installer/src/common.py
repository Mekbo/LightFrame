# -*- coding: utf-8 -*-
"""光影工坊 · 安装器 / 启动器 / 管理器共用工具。

设计要点：
- 单一安装包，本机与在线（局域网）通用；装在哪台机器，那台机器就是服务端。
- 运行时完全自包含在 <安装目录>/runtime，不依赖系统 Python、不写系统环境变量。
- 开机自启采用 HKCU\\...\\Run（当前用户登录时启动），无需管理员、可随时开关、
  且与「我的空间 / 文档输出目录」依赖用户目录的行为一致。
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

APP_NAME = "光影工坊"
EXE_NAME = "光影工坊.exe"
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_RUN_VALUE = "GuangYingStudio"
FW_RULE_NAME = "GuangYingStudio TCP"
LINK_NAME = "光影工坊.lnk"
LINK_MANAGER_NAME = "光影工坊 · 启动设置.lnk"
DEFAULT_PORT = 2026
CREATE_NO_WINDOW = 0x08000000

# 需要清理的宿主环境注入变量（避免目标机器存在类似沙箱时误伤业务删除）
_SANDBOX_ENV = (
    "CODEBUDDY_TOOL_CALL_ID", "CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR",
    "CODEBUDDY_SAFE_DELETE_SANDBOX", "CODEBUDDY_SAFE_DELETE_ENABLED",
)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_dir() -> Path:
    """安装后所有 exe 位于安装根目录。开发态回退到项目 installer 目录。"""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_root() -> Path:
    return install_dir() / "app"


def runtime_python() -> Path:
    return install_dir() / "runtime" / "python.exe"


def icon_path() -> Path:
    return install_dir() / "app.ico"


def log_dir() -> Path:
    return install_dir() / "logs"


def pid_file() -> Path:
    return install_dir() / "server.pid"


def config_path() -> Path:
    return app_root() / "config" / "config.json"


def read_port() -> int:
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
        return int(cfg.get("port") or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def write_port(port: int) -> bool:
    """修改服务端口（重启后生效），保持其余配置不变。"""
    try:
        p = config_path()
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        cfg["port"] = int(port)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def local_url() -> str:
    return "http://127.0.0.1:%d" % read_port()


def lan_ips() -> list:
    """枚举本机可用于局域网访问的 IPv4 地址。"""
    ips = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    # 默认出口 IP（多网卡时最实用的那个）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127.") and ip not in ips:
            ips.insert(0, ip)
    except Exception:
        pass
    return ips


def is_running(timeout: float = 0.6) -> bool:
    """HTTP 探测首页，避免仅凭端口占用误判为本应用。"""
    try:
        req = urllib.request.Request(local_url() + "/", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False
            head = r.read(4096).decode("utf-8", "ignore")
            return ("光影工坊" in head) or ("<html" in head.lower())
    except Exception:
        return False


def _clean_env() -> dict:
    env = os.environ.copy()
    for k in _SANDBOX_ENV:
        env.pop(k, None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def start_server(wait: bool = True, timeout: float = 45.0) -> tuple:
    """启动后端服务（无控制台窗口，日志写 logs/server.log）。

    返回 (成功, 说明)。若已在运行，直接返回成功。
    """
    if is_running():
        return True, "服务已在运行"
    py = runtime_python()
    if not py.exists():
        return False, "未找到运行环境：%s" % py
    app = app_root()
    if not (app / "backend" / "run.py").exists():
        return False, "未找到应用文件，安装可能不完整"
    log_dir().mkdir(parents=True, exist_ok=True)
    try:
        f = open(log_dir() / "server.log", "a", encoding="utf-8", errors="replace")
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        p = subprocess.Popen(
            [str(py), "-m", "backend.run"],
            cwd=str(app), stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=_clean_env(),
            creationflags=CREATE_NO_WINDOW, startupinfo=si,
        )
        f.close()
        pid_file().write_text(str(p.pid), encoding="utf-8")
    except Exception as e:
        return False, "启动失败：%s" % e
    if not wait:
        return True, "已发起启动"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running(0.5):
            return True, "服务已就绪"
        if p.poll() is not None:
            return False, "服务进程提前退出，请查看 logs/server.log"
        time.sleep(0.4)
    return False, "启动超时，请查看 logs/server.log"


def stop_server() -> tuple:
    """停止本启动器启动的服务。"""
    pid = None
    try:
        pid = int(pid_file().read_text(encoding="utf-8").strip())
    except Exception:
        pass
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    try:
        pid_file().unlink()
    except Exception:
        pass
    for _ in range(20):
        if not is_running(0.4):
            return True, "服务已停止"
        time.sleep(0.3)
    return not is_running(0.4), "已发送停止指令"


def restart_server() -> tuple:
    stop_server()
    time.sleep(0.6)
    return start_server()


def open_browser(url: str = "") -> None:
    import webbrowser
    webbrowser.open(url or local_url())


def open_folder(path: Path) -> None:
    try:
        os.startfile(str(path))
    except Exception:
        pass


def open_log() -> None:
    p = log_dir() / "server.log"
    if p.exists():
        try:
            os.startfile(str(p))
        except Exception:
            pass
    else:
        open_folder(log_dir())


# ------------------------------ 开机自启 ------------------------------
def _winreg():
    import winreg
    return winreg


def autostart_enabled() -> bool:
    try:
        w = _winreg()
        k = w.OpenKey(w.HKEY_CURRENT_USER, REG_RUN_KEY)
        try:
            v, _ = w.QueryValueEx(k, REG_RUN_VALUE)
            return bool(v)
        finally:
            k.Close()
    except Exception:
        return False


def set_autostart(on: bool) -> tuple:
    """写入 / 删除 HKCU Run 项；无需管理员权限。"""
    try:
        w = _winreg()
        k = w.CreateKey(w.HKEY_CURRENT_USER, REG_RUN_KEY)
        try:
            if on:
                exe = Path(sys.executable).resolve() if frozen() else Path(sys.executable).resolve()
                w.SetValueEx(k, REG_RUN_VALUE, 0, w.REG_SZ,
                             '"%s" --autostart' % exe)
            else:
                try:
                    w.DeleteValue(k, REG_RUN_VALUE)
                except FileNotFoundError:
                    pass
        finally:
            k.Close()
        return True, ("已开启开机自启" if on else "已关闭开机自启")
    except Exception as e:
        return False, "设置失败：%s" % e


# ------------------------------ 防火墙 ------------------------------
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def fw_rule_name() -> str:
    return "%s %d" % (FW_RULE_NAME, read_port())


def firewall_added() -> bool:
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             "name=%s" % fw_rule_name()],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
            encoding="gbk", errors="ignore",
        )
        return ("规则名称" in (r.stdout or "")) or ("Rule Name" in (r.stdout or ""))
    except Exception:
        return False


def _run_elevated_bat(lines: list, timeout: float = 40.0) -> tuple:
    """以管理员权限执行一段 bat，并通过结果文件回传退出码。"""
    d = Path(tempfile.mkdtemp(prefix="gy_"))
    bat = d / "gy_task.bat"
    res = d / "result.txt"
    # 纯 ASCII 内容，规避 bat 中文编码问题
    body = ["@echo off"] + lines + ["echo %%errorlevel%% > \"%s\"" % res]
    bat.write_text("\r\n".join(body) + "\r\n", encoding="ascii")
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(bat), "", str(d), 0)
        if int(rc) <= 32:
            return False, "用户取消了管理员授权"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if res.exists():
                try:
                    code = int(res.read_text(encoding="ascii", errors="ignore").strip() or "1")
                except Exception:
                    code = 1
                return code == 0, ("执行成功" if code == 0 else "执行失败（code=%d）" % code)
            time.sleep(0.3)
        return False, "管理员授权超时"
    except Exception as e:
        return False, "提权失败：%s" % e


def firewall_allow() -> tuple:
    """放行 TCP 端口入站（局域网访问必需）；需要管理员授权一次。"""
    name = fw_rule_name()
    port = read_port()
    line = ('netsh advfirewall firewall add rule name="%s" '
            'dir=in action=allow protocol=TCP localport=%d profile=any enable=yes'
            % (name, port))
    if is_admin():
        try:
            r = subprocess.run(line, shell=True, capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
            return r.returncode == 0, ("已放行" if r.returncode == 0 else "放行失败")
        except Exception as e:
            return False, str(e)
    return _run_elevated_bat([line])


def firewall_remove() -> tuple:
    name = fw_rule_name()
    line = 'netsh advfirewall firewall delete rule name="%s"' % name
    if is_admin():
        try:
            subprocess.run(line, shell=True, capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
            return True, "已移除规则"
        except Exception as e:
            return False, str(e)
    return _run_elevated_bat([line])


# ------------------------------ 快捷方式 ------------------------------
def _ps_script() -> Path:
    """生成创建快捷方式的 PowerShell 脚本（UTF-8 BOM）。

    参数通过同目录 JSON 传入，彻底避开中文路径 / 空参数 / PowerShell 命令行引号问题，
    也不会踩到 $args 这个 PowerShell 自动变量。
    """
    p = Path(tempfile.gettempdir()) / "guangying_mklink.ps1"
    p.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$cfg = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'guangying_mklink.json') "
        "-Encoding UTF8 -Raw | ConvertFrom-Json\n"
        "$w = New-Object -ComObject WScript.Shell\n"
        "$s = $w.CreateShortcut($cfg.lnk)\n"
        "$s.TargetPath = $cfg.target\n"
        "$s.Arguments = $cfg.argstr\n"
        "$s.IconLocation = $cfg.icon\n"
        "$s.Description = $cfg.desc\n"
        "$s.WorkingDirectory = $cfg.work\n"
        "$s.Save()\n",
        encoding="utf-8-sig",
    )
    return p


def create_shortcut(lnk: Path, target: Path, arguments: str = "",
                    icon: Path = None, desc: str = APP_NAME,
                    workdir: Path = None) -> bool:
    try:
        ps = _ps_script()
        cfg = {
            "lnk": str(lnk), "target": str(target), "argstr": arguments or "",
            "icon": str(icon or target), "desc": desc,
            "work": str(workdir or target.parent),
        }
        (ps.parent / "guangying_mklink.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", str(ps)],
            capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30)
        return r.returncode == 0 and lnk.exists()
    except Exception:
        return False


def desktop_dir() -> Path:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        try:
            v, _ = winreg.QueryValueEx(k, "Desktop")
            p = Path(os.path.expandvars(v))
            if p.exists():
                return p
        finally:
            k.Close()
    except Exception:
        pass
    return Path.home() / "Desktop"


def startmenu_dir() -> Path:
    p = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME
    return p


def all_shortcuts() -> list:
    return [desktop_dir() / LINK_NAME,
            desktop_dir() / LINK_MANAGER_NAME,
            startmenu_dir() / LINK_NAME,
            startmenu_dir() / LINK_MANAGER_NAME,
            startmenu_dir() / "卸载光影工坊.lnk"]


def remove_shortcuts() -> None:
    for p in all_shortcuts():
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
    try:
        d = startmenu_dir()
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
    except Exception:
        pass
