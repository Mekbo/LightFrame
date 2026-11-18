# -*- coding: utf-8 -*-
"""独立重启助手：等后端服务退出后，用启动器的 --autostart 重新拉起服务。

由 app_updater._spawn_restart() 以 detached 进程启动，父进程（后端）随后
os._exit(0)。本脚本必须是纯标准库、无 backend 包依赖——它作为独立脚本运行，
不在 FastAPI 进程内，也不 import 任何应用模块。

为什么不用 --restart：--restart 内部会 taskkill /T /F 旧 pid 的整棵进程树，
而本助手正是旧进程的子进程，会被连锅端。改用「后端自己退出 + 助手只负责拉起」，
--autostart 只做 start_server(wait=False)，不杀任何进程，安全。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DETACHED = 0x00000008
NEW_GROUP = 0x00000200
NO_WINDOW = 0x08000000

# ---------------------------------------------------------------------------
# PyInstaller onefile 私有环境变量（重启失败的根因）
#
# 光影工坊.exe 以 --onefile 打包，bootloader 启动时会设置一组 _PYI_* 变量，
# 其中 _PYI_APPLICATION_HOME_DIR 记录了它解压到的 %TEMP%\_MEI_xxx 目录。
# 这些变量会经由「exe → runtime\python.exe(后端) → 本助手」一路继承。
# 若助手启动新 exe 时不清理，新 exe 会误以为自己是「已解压的子进程」，
# 直接复用旧进程退出时早已删除的 _MEI 目录，于是加载 python313.dll 失败，
# 报「找不到指定的模块」——这正是应用内「安装更新 → 重启应用」失败的原因。
#
# 修复：启动新 exe 时传一份剔除私有变量的 env；助手自身也先清一遍做双保险。
# 只删 bootloader 私有键，PATH / SYSTEMROOT / TEMP / COMSPEC 等全部保留。
# ---------------------------------------------------------------------------
RESTART_FIX_TAG = "restartfix-20261118"
_PYI_ENV_PREFIX = "_PYI_"
_PYI_EXTRA_KEYS = frozenset({"_MEIPASS2"})


def _is_pyi_key(key: str) -> bool:
    u = key.upper()
    return u.startswith(_PYI_ENV_PREFIX) or u in _PYI_EXTRA_KEYS


def _clean_child_env(base=None) -> dict:
    """返回一份剔除 PyInstaller 私有变量的环境，供启动新 exe 时使用。"""
    src = os.environ if base is None else base
    return {k: v for k, v in src.items() if not _is_pyi_key(k)}


def _purge_pyi_env() -> int:
    """从当前进程环境剔除私有变量，返回剔除数量（助手自身双保险）。"""
    keys = [k for k in os.environ if _is_pyi_key(k)]
    for k in keys:
        os.environ.pop(k, None)
    return len(keys)


def log(msg: str) -> None:
    try:
        p = Path(__file__).resolve().parent.parent.parent / "logs" / "restart.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", errors="replace") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    """判断目标进程是否仍在运行。

    Windows 坑：OpenProcess 对「已退出但句柄未释放」的僵尸进程仍返回有效句柄，
    只看句柄会永远等不到退出。必须再用 GetExitCodeProcess 判断是否为
    STILL_ACTIVE(259)；259 之外的退出码都代表进程已终止。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            k32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--exe", required=True)
    ap.add_argument("--app", required=True)
    ap.add_argument("--marker", required=True)
    a = ap.parse_args()

    purged = _purge_pyi_env()
    log("助手启动 [%s]，已剔除 %d 个 PyInstaller 私有变量，等待后端 pid=%d 退出"
        % (RESTART_FIX_TAG, purged, a.pid))
    deadline = time.time() + 90
    while time.time() < deadline:
        if not pid_alive(a.pid):
            break
        time.sleep(0.4)
    else:
        log("错误：等待后端退出超时，放弃重启")
        return 2
    # 给端口释放留一点余量
    time.sleep(1.0)

    exe = Path(a.exe)
    if not exe.exists():
        log("错误：找不到启动器 %s" % exe)
        return 3
    try:
        env = _clean_child_env()
        subprocess.Popen([str(exe), "--autostart"],
                         cwd=str(Path(a.app)),
                         env=env,  # 关键：不继承 _PYI_* / _MEIPASS2，否则新 exe 复用旧 _MEI 目录
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=DETACHED | NEW_GROUP | NO_WINDOW if os.name == "nt" else 0,
                         close_fds=True)
        log("已调用 --autostart 拉起服务（已剔除 %d 个私有变量，新 exe 环境干净）"
            % purged)
    except Exception as e:
        log("错误：拉起服务失败 %s" % e)
        return 4

    # 标记重启已完成，供前端轮询判断
    try:
        m = Path(a.marker)
        d = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
        d["restarted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        d["ok"] = True
        m.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    log("重启流程完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
