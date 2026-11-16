"""增量补丁包的发现、校验、安装与空闲自动重启（v1.0.0 基准包内置）。

设计要点（与「一个完整首发包 + 后续增量补丁包」的发版策略配套）：
- 安装 ≠ 重启：补丁只含 .py / 静态页，Windows 下 .py 不被进程锁住，
  因此可在服务运行中直接覆盖；覆盖后仅标记「待重启」，不需要停服。
- 重启由独立助手进程完成：后端启动 detached 助手后主动 os._exit(0)，
  助手等父进程消失再调用首发包已存在的 `光影工坊.exe --autostart` 拉起服务。
  这样不需要改 exe、不需要重编译完整包，全部逻辑都能随增量补丁下发。
- 忙闲避让：只有 active==0 且 waiting==0 且连续稳定的空闲才允许重启；
  有任务时保留待重启标记并给出友好提示，等空闲后自动完成。
- 手动/自动两种模式：manual 只提示由管理员点「安装更新」；
  auto 在检测到更新且空闲时自动下载安装。两者的重启都由守护线程统一调度。
- 安全边界：补丁路径白名单 + SHA256 双重校验 + 覆盖前备份 + 失败回滚。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from ..core.config import CONFIG_DIR, load_config, save_config
from . import app_update as A

# backend/services/app_updater.py -> 上级三级 = app 目录（安装形态下为 <安装目录>/app）
APP_ROOT = Path(__file__).resolve().parent.parent.parent
INSTALL_ROOT = APP_ROOT.parent
RUNTIME_PY = INSTALL_ROOT / "runtime" / "python.exe"
LAUNCHER_EXE = INSTALL_ROOT / ("光影工坊.exe")
STATE_PATH = CONFIG_DIR / "update_install.json"
BACKUP_ROOT = CONFIG_DIR / "update_backup"
RESTART_MARK = CONFIG_DIR / "update_restart.json"
HELPER_PY = APP_ROOT / "backend" / "services" / "restart_helper.py"

MODES = {"manual": "手动安装", "auto": "自动安装（空闲时）"}
WINDOWS = {"any": "全天均可", "night": "仅夜间 22:00–06:00"}
DEFAULT_MODE = "manual"
DEFAULT_WINDOW = "any"

# 只允许覆盖程序文件；用户数据（config/space/data/output）、运行时、exe 一律拒绝
ALLOWED_PREFIXES = ("app/backend/", "app/static/")
BANNED_PARTS = ("config/", "space/", "data/", "output/", "runtime/", "logs/")
MAX_PATCH_BYTES = 32 * 1024 * 1024
MAX_SINGLE_FILE = 8 * 1024 * 1024
IDLE_STABLE_ROUNDS = 2          # 连续 2 轮（每轮 30s）空闲才重启
DAEMON_INTERVAL = 30.0

PHASE_LABELS = {
    "idle": "未安装更新",
    "downloading": "正在下载补丁包…",
    "verifying": "正在校验补丁包…",
    "applying": "正在覆盖程序文件…",
    "pending_restart": "补丁已安装，等待空闲后重启",
    "restarting": "正在重启应用…",
    "done": "更新完成",
    "failed": "更新失败",
}

_lock = threading.RLock()
_installing = False
_idle_probe = None               # 由 main.py 注入：返回 {"active": n, "waiting": n}
_idle_streak = 0
_daemon_started = False


# ------------------------------ 状态与配置 ------------------------------

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_state() -> dict:
    return {
        "phase": "idle",
        "phase_label": PHASE_LABELS["idle"],
        "target_version": "",
        "base_version": "",
        "files_count": 0,
        "message": "",
        "error": "",
        "pending_restart": False,
        "restart_requested_at": "",
        "installed_at": "",
        "finished_at": "",
        "rollback_available": False,
        "rollback_version": "",
    }


def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            d = json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
            base = _default_state()
            base.update(d)
            return base
    except Exception:
        pass
    return _default_state()


def _save_state(patch: dict) -> dict:
    with _lock:
        st = _load_state()
        st.update(patch or {})
        st["phase_label"] = PHASE_LABELS.get(st.get("phase", "idle"), "")
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception:
            pass
        return st


def get_mode() -> dict:
    cfg = load_config("")
    mode = str(cfg.get("update_install_mode") or DEFAULT_MODE)
    window = str(cfg.get("update_auto_window") or DEFAULT_WINDOW)
    if mode not in MODES:
        mode = DEFAULT_MODE
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    return {"mode": mode, "mode_label": MODES[mode],
            "window": window, "window_label": WINDOWS[window]}


def set_mode(mode=None, window=None) -> dict:
    patch = {}
    if mode is not None:
        if mode not in MODES:
            raise ValueError("安装方式只能是 manual / auto")
        patch["update_install_mode"] = mode
    if window is not None:
        if window not in WINDOWS:
            raise ValueError("重启时段只能是 any / night")
        patch["update_auto_window"] = window
    if patch:
        save_config("", patch)
    return get_mode()


def set_idle_probe(fn) -> None:
    """注入任务队列探针，避免 services 层反向依赖 main 的 mgr 实例。"""
    global _idle_probe
    _idle_probe = fn


def _queue_state() -> dict:
    try:
        if _idle_probe:
            d = _idle_probe() or {}
            return {"active": int(d.get("active") or 0),
                    "waiting": int(d.get("waiting")
                                   or d.get("waiting_count") or 0)}
    except Exception:
        pass
    return {"active": 0, "waiting": 0}


def is_idle() -> bool:
    q = _queue_state()
    return q["active"] == 0 and q["waiting"] == 0


def _in_window() -> bool:
    if get_mode()["window"] != "night":
        return True
    hour = datetime.now().hour
    return hour >= 22 or hour < 6


def get_status() -> dict:
    """给前端的完整状态（含忙闲、可安装判定、待重启提示）。"""
    st = _load_state()
    cfg = get_mode()
    q = _queue_state()
    ver = A.get_status(force=False)
    busy = q["active"] > 0 or q["waiting"] > 0
    has_patch = bool(ver.get("patch_asset", {}).get("url"))
    can_install = bool(ver.get("has_update") and has_patch
                       and st.get("phase") not in ("downloading", "verifying",
                                                   "applying", "restarting"))
    # 服务重启恢复后，把 restarting 收敛为 done
    if st.get("phase") == "restarting" and not ver.get("has_update"):
        st = _save_state({"phase": "done", "pending_restart": False,
                          "finished_at": _now(),
                          "message": "应用已重启，更新生效"})
    msg = st.get("message") or ""
    hint = ""
    if st.get("pending_restart") and busy:
        hint = "目前有任务在执行，请空闲时间再重启应用；应用会在队列清空后自动完成重启。"
    return {
        "phase": st.get("phase"),
        "phase_label": st.get("phase_label"),
        "message": msg,
        "hint": hint,
        "error": st.get("error") or "",
        "target_version": st.get("target_version") or "",
        "files_count": int(st.get("files_count") or 0),
        "pending_restart": bool(st.get("pending_restart")),
        "installed_at": st.get("installed_at") or "",
        "rollback_available": bool(st.get("rollback_available")),
        "mode": cfg["mode"], "mode_label": cfg["mode_label"],
        "window": cfg["window"], "window_label": cfg["window_label"],
        "mode_options": [{"value": k, "label": v} for k, v in MODES.items()],
        "window_options": [{"value": k, "label": v} for k, v in WINDOWS.items()],
        "busy": busy, "active": q["active"], "waiting": q["waiting"],
        "in_window": _in_window(),
        "has_update": bool(ver.get("has_update")),
        "latest_version": ver.get("latest_version") or "",
        "current_version": ver.get("current_version") or "",
        "has_patch": has_patch,
        "patch_name": ver.get("patch_asset", {}).get("name") or "",
        "can_install": can_install,
        "installed_layout": _installed_layout(),
    }


def _installed_layout() -> bool:
    """是否为标准安装形态（存在 runtime + 启动器）；源码开发态禁用重启。"""
    return RUNTIME_PY.exists() and LAUNCHER_EXE.exists() and HELPER_PY.exists()


# ------------------------------ 补丁安全校验 ------------------------------

class PatchError(RuntimeError):
    """补丁包不合法，安装必须整体失败并回滚。"""


def _safe_rel(name: str) -> str:
    """把 zip 条目规范化为相对安装目录的 posix 路径，拒绝一切越界写法。"""
    raw = str(name or "").replace("\\", "/")
    p = PurePosixPath(raw)
    if raw.startswith("/") or p.is_absolute() or ".." in p.parts:
        raise PatchError("补丁包含非法路径：%s" % raw)
    if ":" in raw.split("/")[0]:
        raise PatchError("补丁包含盘符路径：%s" % raw)
    rel = str(p).lstrip("/")
    if not rel or not any(rel.startswith(x) for x in ALLOWED_PREFIXES):
        raise PatchError("补丁包含白名单外的路径：%s" % rel)
    low = rel.lower()
    if any(b in low for b in BANNED_PARTS):
        raise PatchError("补丁试图写入用户数据目录：%s" % rel)
    if low.endswith((".pyc", ".pyo", ".json")):
        raise PatchError("补丁包含不允许的文件类型：%s" % rel)
    return rel


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_sums(text: str, zip_name: str) -> str:
    """解析 SHA256SUMS.txt，只取与补丁包同名的那一行。"""
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == zip_name:
            h = parts[0].lower()
            if len(h) == 64:
                return h
    raise PatchError("SHA256SUMS.txt 中没有补丁包的校验值")


def _download(url: str, timeout: float = 60.0) -> bytes:
    return A._request_bytes(url, timeout=timeout, max_bytes=MAX_PATCH_BYTES)


def _fetch_patch():
    """返回 (zip_bytes, zip_name, manifest, sums_text)；发现/下载/校验一次完成。"""
    ver = A.get_status(force=True)
    if not ver.get("has_update"):
        raise PatchError("当前已是最新版本，没有可安装的补丁包")
    asset = ver.get("patch_asset") or {}
    url, name = asset.get("url") or "", asset.get("name") or ""
    if not url or not name:
        raise PatchError("该版本未提供增量补丁包（LightFrame_Patch_*.zip），请按发布说明手动升级")
    _save_state({"phase": "downloading", "target_version": ver.get("latest_version"),
                 "error": "", "message": "正在下载 " + name})
    data = _download(url)

    sums_text = ""
    sums_asset = ver.get("checksum_asset") or {}
    if sums_asset.get("url"):
        try:
            sums_text = _download(sums_asset["url"], timeout=20.0).decode("utf-8", "replace")
            expected = _parse_sums(sums_text, name)
            if _sha256(data) != expected:
                raise PatchError("补丁包 SHA256 校验失败，已停止安装")
        except PatchError:
            raise
        except Exception as e:
            raise PatchError("无法读取校验文件 SHA256SUMS.txt：%s" % str(e)[:120])

    _save_state({"phase": "verifying", "message": "正在校验补丁内容"})
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
            names = zf.namelist()
            if "manifest.json" not in names:
                raise PatchError("补丁包缺少 manifest.json")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            files = []
            for item in (manifest.get("files") or []):
                rel = _safe_rel(item.get("path"))
                blob = zf.read(rel)
                if len(blob) > MAX_SINGLE_FILE:
                    raise PatchError("单个文件超过体积上限：%s" % rel)
                declared = str(item.get("sha256") or "").lower()
                if len(declared) != 64:
                    raise PatchError("manifest 中缺少文件 sha256：%s" % rel)
                if _sha256(blob) != declared:
                    raise PatchError("文件 SHA256 校验失败：%s" % rel)
                files.append((rel, blob))
    except PatchError:
        raise
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        raise PatchError("补丁包结构损坏：%s" % str(e)[:120])

    if not files:
        raise PatchError("补丁包没有需要安装的文件")
    version = str(manifest.get("version") or "").lstrip("v")
    if version and version != str(ver.get("latest_version") or "").lstrip("v"):
        raise PatchError("补丁版本与 Release 版本不一致，已停止安装")
    return name, manifest, files


# ------------------------------ 安装与回滚 ------------------------------

def _apply_files(files) -> dict:
    """覆盖前逐文件备份，任一失败则整体回滚。返回备份信息。"""
    version = _load_state().get("target_version") or "unknown"
    backup = BACKUP_ROOT / version
    backup.mkdir(parents=True, exist_ok=True)
    manifest_items = []
    applied = []
    try:
        for rel, blob in files:
            target = INSTALL_ROOT / PurePosixPath(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            rec = {"path": rel, "existed": target.exists()}
            if rec["existed"]:
                bak = backup / (rel.replace("/", "__") + ".bak")
                shutil.copy2(target, bak)
                rec["backup"] = str(bak)
            target.write_bytes(blob)
            applied.append(rec)
            manifest_items.append(rec)
        (backup / "backup_manifest.json").write_text(
            json.dumps(manifest_items, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"backup_dir": str(backup), "items": manifest_items}
    except Exception as e:
        _restore(applied)
        raise PatchError("写入程序文件失败，已回滚：%s" % str(e)[:160])


def _restore(items) -> None:
    for rec in reversed(items or []):
        try:
            target = INSTALL_ROOT / PurePosixPath(rec["path"])
            if rec.get("existed") and rec.get("backup"):
                shutil.copy2(rec["backup"], target)
            elif not rec.get("existed"):
                target.unlink(missing_ok=True)
        except Exception:
            pass


def rollback() -> dict:
    st = _load_state()
    version = st.get("rollback_version") or ""
    if not version:
        raise PatchError("没有可回滚的备份")
    mf = BACKUP_ROOT / version / "backup_manifest.json"
    if not mf.exists():
        raise PatchError("备份清单不存在，无法回滚")
    _restore(json.loads(mf.read_text(encoding="utf-8")))
    return _save_state({"phase": "idle", "pending_restart": False,
                        "rollback_available": False, "error": "",
                        "message": "已回滚到 v%s 的程序文件，请重启应用生效" % version})


def install_async(actor: str = "admin") -> dict:
    """异步执行安装（下载→校验→覆盖）；不阻塞 HTTP 请求，不自动重启。"""
    global _installing
    with _lock:
        if _installing:
            return {"ok": False, "message": "已有安装任务在进行中"}
        st = _load_state()
        if st.get("phase") in ("downloading", "verifying", "applying", "restarting"):
            return {"ok": False, "message": "已有安装任务在进行中"}
        _installing = True

    def _run():
        global _installing
        try:
            name, manifest, files = _fetch_patch()
            _save_state({"phase": "applying", "message": "正在覆盖 %d 个程序文件" % len(files)})
            base_ver = A.APP_VERSION
            bk = _apply_files(files)
            _save_state({
                "phase": "pending_restart",
                "pending_restart": True,
                "base_version": base_ver,
                "files_count": len(files),
                "installed_at": _now(),
                "restart_requested_at": _now(),
                "rollback_available": True,
                "rollback_version": _load_state().get("target_version") or "",
                "error": "",
                "message": "补丁 %s 已安装 %d 个文件，等待队列空闲后自动重启生效"
                           % (name, len(files)),
            })
        except Exception as e:
            _save_state({"phase": "failed", "error": str(e)[:300],
                         "message": "更新失败：" + str(e)[:200],
                         "pending_restart": False})
        finally:
            _installing = False

    threading.Thread(target=_run, name="patch-installer", daemon=True).start()
    return {"ok": True, "message": "已开始安装更新，可在下方查看进度"}


# ------------------------------ 重启链路 ------------------------------

def _spawn_restart() -> None:
    """启动 detached 助手，随后由调用方 os._exit(0) 让出进程。"""
    RESTART_MARK.write_text(json.dumps({
        "target_version": _load_state().get("target_version") or "",
        "requested_at": _now(),
        "pid": os.getpid(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    py = str(RUNTIME_PY)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    subprocess.Popen(
        [py, str(HELPER_PY), "--pid", str(os.getpid()),
         "--exe", str(LAUNCHER_EXE), "--app", str(APP_ROOT),
         "--marker", str(RESTART_MARK)],
        cwd=str(APP_ROOT), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True,
    )


def request_restart(force: bool = False) -> dict:
    """手动立即重启（管理员按钮）。忙时拒绝并给出提示。"""
    st = _load_state()
    if not st.get("pending_restart"):
        return {"ok": False, "message": "当前没有待生效的更新，无需重启"}
    if not force and not is_idle():
        return {"ok": False,
                "message": "目前有任务在执行，请空闲时间再重启应用；队列清空后会自动重启。"}
    if not _installed_layout():
        return {"ok": False, "message": "当前为源码运行形态，请在安装版中使用自动重启"}
    return _do_restart()


def _do_restart() -> dict:
    """写状态 → 延迟启动助手 → 后端退出。延迟保证 HTTP 响应先送达前端。"""
    _save_state({"phase": "restarting", "pending_restart": False,
                 "message": "正在重启应用，请稍候…"})

    def _exit_later():
        time.sleep(1.2)
        try:
            _spawn_restart()
        except Exception as e:
            _save_state({"phase": "failed", "error": "启动重启助手失败：%s" % str(e)[:160],
                         "message": "自动重启失败，请在「启动设置」里手动点一次「重启」"})
            return
        time.sleep(0.3)
        os._exit(0)

    threading.Thread(target=_exit_later, name="restart-trigger", daemon=True).start()
    return {"ok": True, "message": "应用正在重启，页面会自动检测恢复"}


# ------------------------------ 自动守护 ------------------------------

def _daemon() -> None:
    global _idle_streak
    time.sleep(20)                    # 启动后先让服务完全就绪
    while True:
        try:
            time.sleep(DAEMON_INTERVAL)
            st = _load_state()
            idle = is_idle()
            _idle_streak = (_idle_streak + 1) if idle else 0
            stable = _idle_streak >= IDLE_STABLE_ROUNDS

            # 1) 待重启优先：无论手动/自动，队列稳定空闲即完成重启
            if (st.get("pending_restart") and stable and _in_window()
                    and _installed_layout() and not _installing):
                _idle_streak = 0
                _do_restart()
                continue

            # 2) 自动模式：有更新 + 有补丁 + 稳定空闲 + 在时段内 → 自动安装
            if get_mode()["mode"] == "auto" and stable and _in_window():
                ver = A.get_status(force=False)
                if (ver.get("has_update") and ver.get("patch_asset", {}).get("url")
                        and st.get("phase") in ("idle", "done", "failed")
                        and not st.get("pending_restart")):
                    install_async(actor="auto")
        except Exception:
            pass                       # 守护线程永不因单次异常退出


def start_daemon() -> None:
    global _daemon_started
    with _lock:
        if _daemon_started:
            return
        _daemon_started = True
    threading.Thread(target=_daemon, name="update-daemon", daemon=True).start()
