"""应用版本、功能更新日志与版本状态检查（正式版 1.0.0）。

设计要点：
- 版本真值只有一处：APP_VERSION；更新检查周期真值只有一处：全局配置
  update_check_cycle（week / month / off，默认 week），任何登录用户都可改。
- 远端版本来源：GitHub Releases latest → 仓库根目录 VERSION 文件（回退）。
  两者都拿不到时只标记「暂未获取到发布信息」，绝不伪造「有更新」。
- 检查结果缓存于 config/update_status.json，按周期到期才联网，避免每次开页都请求；
  用户点「立即检查更新」时 force=True 可绕过周期手动刷新。
- 全程只用标准库 urllib，超时 4 秒；联网失败不影响界面，保留上次检查结果。
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from ..core.config import CONFIG_DIR, load_config, save_config

APP_NAME = "光影工坊"
APP_VERSION = "1.0.0"
RELEASE_DATE = "2026-11-14"
REPO_URL = "https://github.com/Mekbo/LightFrame.git"
REPO_WEB = "https://github.com/Mekbo/LightFrame"
REPO_API = "https://api.github.com/repos/Mekbo/LightFrame/releases/latest"
VERSION_RAW = "https://raw.githubusercontent.com/Mekbo/LightFrame/main/VERSION"

CYCLES = {"week": 7, "month": 30, "off": 0}
CYCLE_LABELS = {
    "week": "每 1 周检查一次",
    "month": "每 1 个月检查一次",
    "off": "不检查",
}
DEFAULT_CYCLE = "week"
STATUS_PATH = CONFIG_DIR / "update_status.json"
_TIMEOUT = 4.0
_lock = threading.Lock()

# 本地功能更新日志（随版本发布维护；远端 Release 说明另行展示）
CHANGELOG = [
    {
        "version": "1.0.0",
        "date": RELEASE_DATE,
        "title": "正式版首个发布",
        "items": [
            "图片处理：格式转换、裁切、缩放、去水印、图片加文字、Live 图提取，支持批量上传与并发队列",
            "文案生产：选题搭建 → 脚本创作 → 配音生成 → 封面制作 → 打包文案，五步完成一套素材",
            "视频创作：九步向导配置素材、封面、配音字幕、视频模板、背景乐、时长、转场与原创度校验",
            "模板中心：图片 / 视频 / AI 提示词三类模板统一管理与共享，所有工作场景自动加载，支持点击预览",
            "AI 大模型：多平台模型接入与密钥托管，模型可共享给其他账户使用",
            "任务管理：环形完成率仪表盘、四态数据卡片、分布进度条，实时反映排队 / 进行中 / 已完成",
            "账户与权限：管理员 / 普通用户 / 游客三级权限，任务与产物按账户隔离",
            "全局设置：输出目录、并发、成品预览数量、原创度校验、系统更新检查周期",
            "关于应用：使用指南、系统更新与版本状态、开发者说",
        ],
    },
]

class NoReleaseError(RuntimeError):
    """远端可访问，但仓库还没有任何在线发布记录（此时本地版本即为最新）。"""


STATUS_TEXT = {
    "up_to_date": ("已是最新版本", "当前安装的就是最新正式版，无需更新。"),
    "update_available": ("发现新版本", "检测到新版本，左侧「系统更新」菜单已显示红色提示；请由应用维护者按发布说明完成升级。"),
    "off": ("未启用自动检查", "当前设置为「不检查」，应用不会自动联网检查；需要时可点「立即检查更新」手动查看。"),
    "unknown": ("暂未获取到发布信息", "暂时无法连接到开源仓库，未能获取在线版本信息；可稍后点「立即检查更新」重试，不影响现有功能。"),
}


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_time(s) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_now().tzinfo)
        return d
    except Exception:
        return None


def _load_status() -> dict:
    try:
        if STATUS_PATH.exists():
            return json.loads(STATUS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_status(st: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _request(url: str, timeout: float = _TIMEOUT) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "LightFrame/" + APP_VERSION,
            "Accept": "application/vnd.github+json, text/plain, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _clean_version(s) -> str:
    return re.sub(r"^v", "", str(s or "").strip(), flags=re.I)


def _ver_tuple(s):
    nums = re.findall(r"\d+", str(s or ""))[:3]
    nums = [int(x) for x in nums]
    return tuple(nums + [0] * (3 - len(nums)))


def is_newer(latest, current: str = APP_VERSION) -> bool:
    return _ver_tuple(_clean_version(latest)) > _ver_tuple(_clean_version(current))


def _fetch_remote() -> dict:
    """返回远端最新版本信息；取不到时抛异常（由调用方降级处理）。"""
    data = None
    release_404 = False
    try:
        data = json.loads(_request(REPO_API))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise RuntimeError("GitHub 返回 %s" % e.code)
        release_404 = True                 # 仓库可访问，只是尚未发布 Release
    except (json.JSONDecodeError, TypeError):
        data = None

    if isinstance(data, dict) and data.get("tag_name"):
        return {
            "latest_version": _clean_version(data.get("tag_name")),
            "release_name": str(data.get("name") or ""),
            "release_notes": str(data.get("body") or "")[:2000],
            "published_at": str(data.get("published_at") or ""),
            "release_url": str(data.get("html_url") or ""),
            "source": "github_release",
        }

    # 仓库还没建 Release 时，回退读取根目录 VERSION 纯文本
    try:
        txt = _request(VERSION_RAW, timeout=2.0).strip()
    except urllib.error.HTTPError as e:
        if e.code == 404 or release_404:
            raise NoReleaseError("仓库尚未发布在线版本记录")
        raise RuntimeError("读取 VERSION 文件失败（HTTP %s）" % e.code)
    except Exception:
        # 已确认「无 Release」时，辅助版本文件取不到也仍按「暂无在线发布记录」处理，
        # 不把网络抖动误报成检查失败。
        if release_404:
            raise NoReleaseError("仓库尚未发布在线版本记录")
        raise RuntimeError("网络不可达或读取超时")
    if not re.fullmatch(r"v?\d+(?:\.\d+){0,3}", txt):
        raise RuntimeError("VERSION 文件内容不是有效版本号")
    return {
        "latest_version": _clean_version(txt),
        "release_name": "",
        "release_notes": "",
        "published_at": "",
        "release_url": "",
        "source": "version_file",
    }


def get_status(force: bool = False) -> dict:
    """按当前检查周期返回版本状态；到期或 force 时才联网检查。"""
    cfg = load_config("")
    cycle = str(cfg.get("update_check_cycle") or DEFAULT_CYCLE).lower()
    if cycle not in CYCLES:
        cycle = DEFAULT_CYCLE
    days = CYCLES[cycle]

    st = _load_status()
    last = _parse_time(st.get("last_checked_at"))
    due = days > 0 and (last is None or _now() - last >= timedelta(days=days))
    checked_now = False

    if force or due:
        with _lock:
            st = _load_status()                       # 双重检查，避免并发重复联网
            last = _parse_time(st.get("last_checked_at"))
            due = days > 0 and (last is None or _now() - last >= timedelta(days=days))
            if force or due:
                try:
                    st.update(_fetch_remote())
                    st["check_error"] = ""
                    st["no_release"] = False
                except NoReleaseError:
                    # 仓库可访问但尚未发布任何在线版本：本地正式版即为最新，不算检查失败
                    st["check_error"] = ""
                    st["no_release"] = True
                    st["latest_version"] = ""
                    st["release_notes"] = ""
                    st["source"] = "no_release"
                except Exception as e:
                    st["check_error"] = str(e)[:180]
                    st["no_release"] = False
                st["last_checked_at"] = _now().isoformat(timespec="seconds")
                st["cycle"] = cycle
                _save_status(st)
                last = _parse_time(st.get("last_checked_at"))
                checked_now = True

    latest = str(st.get("latest_version") or "")
    has_update = bool(latest) and is_newer(latest)
    no_release = bool(st.get("no_release"))
    if has_update:
        status = "update_available"
    elif latest or no_release:
        status = "up_to_date"
    elif cycle == "off":
        status = "off"
    else:
        status = "unknown"
    label, hint = STATUS_TEXT[status]
    if status == "up_to_date" and no_release:
        hint = ("当前安装的 v%s 为最新正式版；开源仓库暂未发布在线版本记录，"
                "发布新版本后会自动在菜单提示。" % APP_VERSION)

    next_check = ""
    if days > 0:
        base = last or _now()
        next_check = (base + timedelta(days=days)).isoformat(timespec="seconds")

    return {
        "app_name": APP_NAME,
        "current_version": APP_VERSION,
        "release_date": RELEASE_DATE,
        "repo_url": REPO_URL,
        "repo_web": REPO_WEB,
        "cycle": cycle,
        "cycle_label": CYCLE_LABELS[cycle],
        "cycle_days": days,
        "cycle_options": [{"value": k, "label": CYCLE_LABELS[k]} for k in ("week", "month", "off")],
        "status": status,
        "status_label": label,
        "status_hint": hint,
        "has_update": has_update,
        "latest_version": latest,
        "release_name": str(st.get("release_name") or ""),
        "release_notes": str(st.get("release_notes") or ""),
        "published_at": str(st.get("published_at") or ""),
        "release_url": str(st.get("release_url") or ""),
        "check_source": str(st.get("source") or ""),
        "no_release": no_release,
        "last_checked_at": str(st.get("last_checked_at") or ""),
        "next_check_at": next_check,
        "checked_now": checked_now,
        "check_error": str(st.get("check_error") or ""),
        "changelog": CHANGELOG,
    }


def set_cycle(value) -> dict:
    """保存全局更新检查周期（week / month / off）。"""
    v = str(value or "").lower()
    if v not in CYCLES:
        raise ValueError("周期只能是 week / month / off")
    save_config("", {"update_check_cycle": v})
    return {"ok": True, "cycle": v, "cycle_label": CYCLE_LABELS[v], "cycle_days": CYCLES[v]}
