"""登录保护：连续失败计数 / 4 位数字图形验证码 / 普通用户锁定。

约定（与产品需求一致）：
- 同一账户名连续输错 >= 2 次：后续登录必须填写 4 位数字验证码。
- 普通用户连续输错 >= 3 次：锁定 3 分钟；锁定期间一律拒绝，解锁后计数清零重新计。
- 管理员：达到阈值同样需要验证码，但**永不锁定**（避免管理员被挡在门外）。
- 一次登录成功立即清零该账户名的失败记录；验证码一次性、5 分钟有效。

状态只放在服务进程内存中：锁定最长 3 分钟，服务重启自然清零，不污染 accounts.json。
计数按「登录时填写的账户名」精确匹配（与 auth.find_user 一致），大小写不同的用户名
互不干扰，避免用错误大小写把真实账户锁定。
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time

# ---- 阈值（前后端提示统一从这里取） ----
CAPTCHA_AT = 2          # 第几次失败后开始要验证码
LOCK_AT = 3             # 普通用户第几次失败后锁定
LOCK_SECONDS = 180      # 锁定时长：3 分钟
CAPTCHA_TTL = 300       # 验证码有效期：5 分钟
CAPTCHA_LEN = 4         # 4 位数字
_MAX_CAPTCHAS = 300     # 内存条目上限，防止公开接口被刷爆

_lock = threading.RLock()
_states: dict = {}      # username -> {"failed": int, "locked_until": float}
_captchas: dict = {}    # captcha_id -> {"code": str, "expires": float}


def _key(username: str) -> str:
    """与 find_user 一致：仅去首尾空白、不做大小写归一。"""
    return (username or "").strip()[:64]


def _gc(now: float | None = None) -> None:
    now = now or time.time()
    for cid in [k for k, v in _captchas.items() if v.get("expires", 0) <= now]:
        _captchas.pop(cid, None)
    if len(_captchas) > _MAX_CAPTCHAS:          # 超额时淘汰最早创建的
        for cid in list(_captchas)[:len(_captchas) - _MAX_CAPTCHAS]:
            _captchas.pop(cid, None)
    for k, v in list(_states.items()):
        until = float(v.get("locked_until", 0) or 0)
        if until and until <= now:              # 锁定到期即整条清零
            _states.pop(k, None)


def state(username: str) -> dict:
    """给前端的公开状态，不含验证码答案或账户是否存在的提示。"""
    with _lock:
        _gc()
        rec = _states.get(_key(username))
        failed = int((rec or {}).get("failed", 0) or 0)
        until = float((rec or {}).get("locked_until", 0) or 0)
        locked = until > time.time()
        return {
            "failed": failed,
            "require_captcha": failed >= CAPTCHA_AT,
            "locked": locked,
            "retry_after": int(max(0, until - time.time())) if locked else 0,
            "captcha_at": CAPTCHA_AT,
            "lock_at": LOCK_AT,
            "lock_seconds": LOCK_SECONDS,
        }


def note_failure(username: str, is_admin: bool = False) -> dict:
    """记录一次密码/账户校验失败，返回更新后的状态。"""
    with _lock:
        now = time.time()
        _gc(now)
        k = _key(username)
        rec = _states.get(k)
        if not rec or (rec.get("locked_until", 0) and rec["locked_until"] <= now):
            rec = {"failed": 0, "locked_until": 0.0}
            _states[k] = rec
        rec["failed"] = int(rec.get("failed", 0)) + 1
        if not is_admin and rec["failed"] >= LOCK_AT:
            rec["locked_until"] = now + LOCK_SECONDS
        return state(username)


def note_success(username: str) -> None:
    with _lock:
        _states.pop(_key(username), None)


def reset(username: str) -> None:
    """管理/测试用：手动解除某账户名的失败状态。"""
    with _lock:
        _states.pop(_key(username), None)


# ------------------------------ 4 位数字图形验证码 ------------------------------

def _svg(code: str) -> str:
    """生成浅色底 + 干扰线的 SVG 数字图（零第三方依赖，前端用 data URI 显示）。"""
    import random

    rng = random.SystemRandom()
    w, h = 128, 42
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">',
        f'<rect width="{w}" height="{h}" rx="6" fill="#f2f7ff"/>',
    ]
    for _ in range(5):
        x1, y1 = rng.randint(0, w), rng.randint(2, h - 2)
        x2, y2 = rng.randint(0, w), rng.randint(2, h - 2)
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="#c9dcf7" stroke-width="1"/>')
    colors = ("#1677FF", "#7C3AED", "#0E9F6E", "#C47F00", "#D1435B")
    for i, ch in enumerate(code):
        x = 13 + i * 30 + rng.randint(-2, 2)
        y = 30 + rng.randint(-3, 3)
        rot = rng.randint(-16, 16)
        color = rng.choice(colors)
        parts.append(
            f'<text x="{x}" y="{y}" font-family="Arial,Helvetica,sans-serif" '
            f'font-size="25" font-weight="700" fill="{color}" '
            f'transform="rotate({rot} {x} {y})">{ch}</text>')
    for _ in range(9):
        parts.append(f'<circle cx="{rng.randint(0, w)}" cy="{rng.randint(0, h)}" '
                     f'r="1" fill="#b9d0f2" opacity="0.8"/>')
    parts.append("</svg>")
    return "".join(parts)


def new_captcha() -> tuple:
    """返回 (captcha_id, svg)；验证码答案只留在服务端内存。"""
    with _lock:
        _gc()
        code = f"{secrets.randbelow(10 ** CAPTCHA_LEN):0{CAPTCHA_LEN}d}"
        cid = secrets.token_urlsafe(18)
        _captchas[cid] = {"code": code, "expires": time.time() + CAPTCHA_TTL}
        return cid, _svg(code)


def verify_captcha(cid: str, code: str) -> bool:
    """一次性校验：无论对错都消费掉，防止重放。"""
    with _lock:
        rec = _captchas.pop(cid or "", None)
    if not rec or rec.get("expires", 0) <= time.time():
        return False
    return hmac.compare_digest((code or "").strip(), str(rec.get("code", "")))
