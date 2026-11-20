"""应用版本、功能更新日志与版本状态检查。

设计要点：
- 版本真值只有一处：APP_VERSION；更新检查周期真值只有一处：全局配置
  update_check_cycle（week / month / off，默认 week），任何登录用户都可改。
- 远端版本来源：GitHub Releases latest → 仓库根目录 VERSION 文件（回退）。
  两者都拿不到时只标记「暂未获取到发布信息」，绝不伪造「有更新」。
- 检查结果缓存于 config/update_status.json，按周期到期才联网，避免每次开页都请求；
  用户点「立即检查更新」时 force=True 可绕过周期手动刷新。
- 全程只用标准库 urllib；联网失败不影响界面，保留上次检查结果。
- 公司/局域网常部署 HTTPS 中间人代理：浏览器装了企业根证书，Python 自带证书库不认，
  会报 CERTIFICATE_VERIFY_FAILED。_request() 按「标准库 → 系统 CA → 兼容模式」逐级回退，
  仅证书类错误才回退；该请求是只读公开版本信息、不发送任何凭据，兼容模式风险可控。
- _urlopen_with_cert_fallback() 抽出公共请求逻辑，供版本信息（文本）与增量补丁包
  （二进制）共用同一套证书兼容策略。
- 家庭宽带访问 GitHub 属跨境链路，存在「间歇性抖动」（连接被重置 / 偶发超时）：
  _request() 在证书分级之外再叠加网络重试，并对最终异常保留原始类型与信息，
  避免只报笼统的「网络不可达」而无法定位。
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from ..core.config import CONFIG_DIR, load_config, save_config

APP_NAME = "光影工坊"
APP_VERSION = "1.0.3"
RELEASE_DATE = "2026-11-20"
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
_TIMEOUT = 8.0          # 单次请求超时；配合重试覆盖跨境链路抖动
_NET_RETRIES = 3         # 网络类错误（超时/连接重置）重试次数，证书错误不走这里
_NET_BACKOFF = 0.6       # 重试间隔基数（秒），逐次递增
_lock = threading.Lock()
_LAST_SSL_MODE = "default"   # 最近一次成功的证书策略，供状态接口展示诊断
# 构建标记：一旦错误信息里出现它，即证明本版本代码已被加载（用于排查"替换后未重启"）
BUILD_TAG = "v1.0.3-videocrossdrive-20261120"

# 本地功能更新日志（随版本发布维护；远端 Release 说明另行展示）
CHANGELOG = [
    {
        "version": "1.0.3",
        "date": RELEASE_DATE,
        "title": "修复视频合成跨磁盘报错（WinError 17）",
        "items": [
            "修复在线合成视频时带配音 / 字幕 / 背景乐的任务在最后一步报「WinError 17 系统无法将文件移到不同的磁盘驱动器」的问题：混音成品从系统临时目录（C 盘）落到作品目录（应用安装盘，如 E 盘）时，改用跨盘安全的移动方式，同盘仍保持高效覆盖",
            "无配音/字幕/背景乐的合成任务不受影响；修复不影响画布、转场、封面、清晰度等既有合成逻辑",
        ],
    },
    {
        "version": "1.0.2",
        "date": RELEASE_DATE,
        "title": "工作区空间选择器增强：上传 / 新建文件夹 / 改名 / 删除",
        "items": [
            "图片处理、文案生产、视频创作等工作区「从空间添加」弹窗新增「上传」按钮，可直接上传图片/视频/音频/文本素材到当前目录，上传后自动刷新并选中，免去先到我的空间上传再返回的往返",
            "同一弹窗新增「新建文件夹」，可在当前目录下按我的空间的同名规则创建文件夹，即时刷新",
            "文件夹卡片新增「进入 / 改名 / 删除」操作，与我的空间管理对齐：普通目录三者可用，输出目录可进入与改名但不可删除，作品根目录仅可进入",
            "改名 / 删除后自动同步当前选中项与路径；删除当前所在目录时自动退回上级，避免停留在已不存在的路径",
            "修正面包屑导航误用导致跳错目录的问题；素材类型判定改为按图片/视频/音频/文本扩展名严格匹配",
        ],
    },
    {
        "version": "1.0.1",
        "date": RELEASE_DATE,
        "title": "去水印框选增强 + 自定义遮盖色",
        "items": [
            "去水印框选：上传图片后可框选任意位置，按相对坐标批量套用到所有图片；水印位置不一致时可逐张框选",
            "选色融合去水印：新增颜色选择器与图片吸色，先高斯模糊柔化水印、再混入所选背景色，带融合强度滑块可调，避免死板纯色块",
            "三种修复算法：高斯模糊覆盖、邻近像素填充、选色融合（模糊+选色，推荐），均带边缘羽化过渡",
            "框选前置：框选坐标基于原图，在缩放/裁切之前处理，同时启用裁切时位置不漂移",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2026-11-17",
        "title": "正式版首发（完整基准包）",
        "items": [
            "字体样式 B/I/U：图片加文字与视频字幕/标题支持加粗、倾斜、下划线三种字体样式，Word 式开关，设置即时预览",
            "倾斜方向校正：斜体统一为标准右倾（/），符合常规排版预期",
            "文案模板默认居中：新建文字模板的默认垂直位置由 86% 调整为 50%（居中），视频字幕仍保持底部",
            "默认字体优化：字体列表只显示真实字体，默认字体置顶标注；历史模板旧字体名继续兼容",
            "配音试听优化：默认选中模型并加载角色，试听文案统一，打开即可试听",
            "系统更新：内置局域网 HTTPS 代理证书兼容（默认 → 系统 CA → 兼容模式三级回退），公司网络也能正常核对版本",
            "增量更新安装：管理员可在「系统更新」一键安装增量补丁包，支持手动 / 空闲自动两种模式；下载经 SHA256 双重校验、安装前自动备份、失败可回滚",
            "空闲自动重启：补丁只覆盖程序文件不停服，任务队列清空后自动重启生效；有任务运行时给出友好提示并持续等待，不打断正在进行的工作",
            "跨版本升级防护：补丁安装增加最低基线校验，跨度过大时拒绝自动安装并引导重装完整包，避免版本错乱",
            "界面优化：功能更新日志默认收起、点击展开，支持一键展开/收起全部，日志变多时页面不再冗长",
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


class CertVerifyError(Exception):
    """本地证书链不受信（公司 HTTPS 代理常见），可安全降级重试。"""


def _is_cert_error(exc: BaseException) -> bool:
    """识别 SSL 证书校验失败；DNS / 超时 / HTTP 错误不误判。"""
    chain = [exc]
    reason = getattr(exc, "reason", None)
    if reason is not None:
        chain.append(reason)
    for item in chain:
        if isinstance(item, ssl.SSLCertVerificationError):
            return True
        if isinstance(item, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(item).upper():
            return True
        text = str(item).lower()
        if "certificate verify failed" in text or "unable to get local issuer certificate" in text:
            return True
    return False


def _ssl_strategies():
    """证书策略按信任强度递减：默认 → 系统 CA → 兼容模式（不校验）。"""
    yield "default", None                       # Python 默认证书库
    try:
        yield "system_ca", ssl.create_default_context()   # Windows 系统存储（含企业根证书）
    except Exception:
        pass
    yield "compat_no_verify", ssl._create_unverified_context()   # 最后兜底


def _make_request(url: str, accept: str = "application/vnd.github+json, text/plain, */*"):
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": "LightFrame/" + APP_VERSION,
            "Accept": accept,
        },
    )


class _RespContext:
    """把 (response, mode) 包装成上下文管理器，供 `with ... as (r, mode)` 解包。

    历史 bug：本函数过去直接 return 一个普通元组，调用方却用 `with 元组 as (r, mode)`，
    导致每次请求都在 __enter__ 阶段抛 `TypeError: tuple 不支持上下文管理器协议`。
    该异常被上层压成「网络不可达」，掩盖了真正原因。现改为可解包的上下文管理器。
    """

    def __init__(self, response, mode):
        self._response = response
        self._mode = mode

    def __enter__(self):
        return self._response, self._mode

    def __exit__(self, exc_type, exc, tb):
        try:
            self._response.close()
        except Exception:
            pass
        return False


def _urlopen_with_cert_fallback(req, timeout: float = _TIMEOUT):
    """按证书策略逐级降级发起请求，返回可 `with ... as (response, mode)` 的上下文管理器。

    仅证书链不受信时降级；超时 / 404 / DNS 等原样抛出，避免无谓重试。
    """
    global _LAST_SSL_MODE
    cert_error = None
    for mode, ctx in _ssl_strategies():
        try:
            r = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            _LAST_SSL_MODE = mode
            return _RespContext(r, mode)
        except Exception as e:
            if _is_cert_error(e):
                cert_error = e
                continue                        # 换下一种证书策略重试
            raise                               # 超时 / 404 / DNS 等不做无谓重试
    raise CertVerifyError(str(cert_error) if cert_error else "证书校验失败")


def _request(url: str, timeout: float = _TIMEOUT) -> str:
    """请求公开版本信息（文本）；证书分级 + 网络抖动重试，并保留原始异常。

    家庭宽带访问 GitHub 是跨境链路，偶发连接重置 / 超时（诊断当时能通、下一刻
    检查却失败，即典型抖动）。这里对网络类错误重试 _NET_RETRIES 次；证书全失败
    与 HTTP 4xx/5xx 不重试（重试无意义）。最终把**原始异常**抛出，让上层能看到
    究竟是 TimeoutError / ConnectionResetError 还是别的，不再只报笼统文案。
    """
    req = _make_request(url)
    last_exc = None
    for attempt in range(_NET_RETRIES):
        try:
            with _urlopen_with_cert_fallback(req, timeout) as (r, _mode):
                return r.read().decode("utf-8", "replace")
        except (CertVerifyError, urllib.error.HTTPError):
            raise                      # 证书全失败 / HTTP 错误：重试无意义
        except Exception as e:         # 超时 / 连接重置 / DNS 等网络抖动
            last_exc = e
            if attempt < _NET_RETRIES - 1:
                time.sleep(_NET_BACKOFF * (attempt + 1))
    raise last_exc


def _request_bytes(url: str, timeout: float = 60.0,
                   max_bytes: int = 32 * 1024 * 1024) -> bytes:
    """下载二进制（增量补丁包）；复用同一套证书分级策略，限制体积防内存打满。"""
    req = _make_request(url, "application/octet-stream")
    with _urlopen_with_cert_fallback(req, timeout) as (r, _mode):
        declared = r.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise RuntimeError("附件体积超过上限（%s 字节）" % declared)
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError("附件体积超过上限（%d 字节）" % max_bytes)
    return data


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
        assets = []
        for a in (data.get("assets") or []):
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "")
            url = str(a.get("browser_download_url") or "")
            if name and url:
                assets.append({"name": name,
                               "url": url,
                               "size": int(a.get("size") or 0)})
        patch = next((a for a in assets
                      if a["name"].startswith("LightFrame_Patch_")
                      and a["name"].lower().endswith(".zip")), None)
        sums = next((a for a in assets if a["name"] == "SHA256SUMS.txt"), None)
        return {
            "latest_version": _clean_version(data.get("tag_name")),
            "release_name": str(data.get("name") or ""),
            "release_notes": str(data.get("body") or "")[:2000],
            "published_at": str(data.get("published_at") or ""),
            "release_url": str(data.get("html_url") or ""),
            "source": "github_release",
            "assets": assets,
            "patch_asset": patch or {},
            "checksum_asset": sums or {},
        }

    # Release API 拿不到有效数据时（限流 / 返回非 Release JSON / 网络抖动），
    # 回退读取根目录 VERSION 纯文本。超时必须与主请求一致：
    # 早前这里写死 2 秒，家里「通但慢」的网络下必然超时，误报"网络不可达"。
    try:
        txt = _request(VERSION_RAW, timeout=_TIMEOUT).strip()
    except urllib.error.HTTPError as e:
        if e.code == 404 or release_404:
            raise NoReleaseError("仓库尚未发布在线版本记录")
        raise RuntimeError("读取 VERSION 文件失败（HTTP %s）" % e.code)
    except Exception as e:
        # 已确认「无 Release」时，辅助版本文件取不到也仍按「暂无在线发布记录」处理，
        # 不把网络抖动误报成检查失败。
        if release_404:
            raise NoReleaseError("仓库尚未发布在线版本记录")
        # 保留原始异常类型与信息：跨境链路失败可能是超时、连接重置、远程断开等，
        # 全部压成一句「网络不可达」会掩盖真因，无法对症处理。
        raise RuntimeError("网络不可达或读取超时（%s: %s）" %
                           (type(e).__name__, str(e)[:140]))
    if not re.fullmatch(r"v?\d+(?:\.\d+){0,3}", txt):
        raise RuntimeError("VERSION 文件内容不是有效版本号")
    return {
        "latest_version": _clean_version(txt),
        "release_name": "",
        "release_notes": "",
        "published_at": "",
        "release_url": "",
        "source": "version_file",
        "assets": [],
        "patch_asset": {},
        "checksum_asset": {},
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
                    st["ssl_mode"] = _LAST_SSL_MODE
                except NoReleaseError:
                    # 仓库可访问但尚未发布任何在线版本：本地正式版即为最新，不算检查失败
                    st["check_error"] = ""
                    st["no_release"] = True
                    st["latest_version"] = ""
                    st["release_notes"] = ""
                    st["source"] = "no_release"
                except Exception as e:
                    st["check_error"] = "[%s] %s" % (BUILD_TAG, str(e)[:180])
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
        "patch_asset": st.get("patch_asset") or {},
        "checksum_asset": st.get("checksum_asset") or {},
        "no_release": no_release,
        "last_checked_at": str(st.get("last_checked_at") or ""),
        "next_check_at": next_check,
        "checked_now": checked_now,
        "check_error": str(st.get("check_error") or ""),
        "check_ssl_mode": str(st.get("ssl_mode") or ""),
        "changelog": CHANGELOG,
    }


def set_cycle(value) -> dict:
    """保存全局更新检查周期（week / month / off）。"""
    v = str(value or "").lower()
    if v not in CYCLES:
        raise ValueError("周期只能是 week / month / off")
    save_config("", {"update_check_cycle": v})
    return {"ok": True, "cycle": v, "cycle_label": CYCLE_LABELS[v], "cycle_days": CYCLES[v]}
