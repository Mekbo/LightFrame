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

    log("助手启动，等待后端 pid=%d 退出" % a.pid)
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
        subprocess.Popen([str(exe), "--autostart"],
                         cwd=str(Path(a.app)),
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=DETACHED | NEW_GROUP | NO_WINDOW if os.name == "nt" else 0,
                         close_fds=True)
        log("已调用 --autostart 拉起服务")
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
