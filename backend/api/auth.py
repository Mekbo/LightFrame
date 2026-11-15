"""认证 / 账户 / 用户管理接口（阶段 1、4）。

统一错误体：{"ok": false, "error": {"code": "...", "message": "..."}}
所有身份均从服务端会话解析，接口不接收任何 uid 参数。
"""
from __future__ import annotations

import copy

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import JSONResponse

from ..core import auth, login_guard, policy
from ..core.config import DEFAULTS

router = APIRouter()


def fail(code: str, message: str, status: int = 403):
    return JSONResponse(
        status_code=status,
        content={"ok": False, "error": {"code": code, "message": message}},
    )


def ok(**kw):
    return {"ok": True, **kw}


def _login_fail(code: str, message: str, status: int, st: dict):
    """登录失败统一响应：除错误体外，附带前端渲染验证码/倒计时所需状态。"""
    return JSONResponse(status_code=status, content={
        "ok": False,
        "error": {"code": code, "message": message},
        "failed": int(st.get("failed", 0) or 0),
        "require_captcha": bool(st.get("require_captcha")),
        "locked": bool(st.get("locked")),
        "retry_after": int(st.get("retry_after", 0) or 0),
        "captcha_at": int(st.get("captcha_at", login_guard.CAPTCHA_AT)),
        "lock_at": int(st.get("lock_at", login_guard.LOCK_AT)),
        "lock_seconds": int(st.get("lock_seconds", login_guard.LOCK_SECONDS)),
    })


def current_user(request: Request) -> dict:
    """优先取中间件注入的身份；异常时回退到 Cookie 解析。"""
    try:
        u = request.state.user
        if isinstance(u, dict):
            return u
    except Exception:
        pass
    return auth.user_from_request(request)


def _client_ip(request: Request) -> str:
    try:
        return request.client.host if request.client else ""
    except Exception:
        return ""


# ------------------------------ 公开 ------------------------------

@router.get("/api/auth/me")
def me(request: Request):
    u = current_user(request)
    return ok(user=u, initialized=auth.is_initialized(),
              guest=policy.is_guest(u))


@router.get("/api/public/config")
def public_config():
    """游客渲染界面用的最小配置：不含路径、密钥、并发等全局信息。"""
    d = copy.deepcopy(DEFAULTS)
    for k in ("output_dir", "video_output_dir", "ffmpeg_path", "port",
              "concurrency", "max_concurrency", "originality_k",
              "add_mode", "progress_channel", "developer_wechat",
              "developer_wechat_link"):
        d.pop(k, None)
    return d


# ------------------------------ 登录 / 初始化 ------------------------------

@router.post("/api/auth/init")
def init_admin(payload: dict = Body(default={}), response: Response = None):
    """首次运行：由管理员自行设置初始密码，并自动登录。

    相比「随机初始密码打印在控制台」，GUI/安装包场景更友好，也避免密码留在日志里。
    """
    if auth.is_initialized():
        return fail("ALREADY_INITIALIZED", "系统已初始化，请直接登录", 409)
    pw = str(payload.get("password") or "")
    pw2 = str(payload.get("password2") or pw)
    if pw != pw2:
        return fail("PASSWORD_MISMATCH", "两次输入的密码不一致", 400)
    try:
        u = auth.init_admin(pw, str(payload.get("username") or "admin"))
    except ValueError as e:
        return fail("INVALID", str(e), 400)
    token = auth.create_session(u["id"])
    auth.set_session_cookie(response, token)
    auth.audit(auth.public_user(u), "auth.init", u["id"])
    return ok(user=auth.public_user(u), first_run=True)


@router.get("/api/auth/captcha")
def login_captcha():
    """公开：生成一次性 4 位数字图形验证码，答案只存服务端内存。"""
    cid, svg = login_guard.new_captcha()
    return ok(captcha_id=cid, svg=svg, ttl=login_guard.CAPTCHA_TTL)


@router.get("/api/auth/login-state")
def login_state(username: str = ""):
    """公开：查询某账户名当前的连续失败次数 / 是否需要验证码 / 锁定剩余秒数。

    只返回计数与状态，不透露账户是否存在、是否管理员。
    """
    return ok(**login_guard.state(username))


@router.post("/api/auth/login")
def login(payload: dict = Body(default={}), request: Request = None,
          response: Response = None):
    if not auth.is_initialized():
        return fail("NOT_INITIALIZED", "系统尚未初始化，请先设置管理员密码", 409)
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    st = login_guard.state(username)

    # ① 锁定期内一律拒绝（即使验证码正确），普通用户才会走到这里
    if st["locked"]:
        mm, ss = divmod(int(st["retry_after"]), 60)
        return _login_fail(
            "ACCOUNT_LOCKED",
            f"尝试次数有点多，账号已临时锁定，请在 {mm:02d}:{ss:02d} 后再试",
            423, st)

    # ② 连续失败达阈值：必须先通过一次性验证码
    if st["require_captcha"]:
        cid = str(payload.get("captcha_id") or "")
        code = str(payload.get("captcha_code") or "")
        if not cid or not code:
            return _login_fail(
                "CAPTCHA_REQUIRED",
                "请先填写右侧 4 位数字验证码再试一次", 401, st)
        if not login_guard.verify_captcha(cid, code):
            return _login_fail("BAD_CAPTCHA", "验证码不对或已过期，换一张再试试", 401, st)

    # ③ 校验账户与密码；管理员只累计失败次数、不锁定
    candidate = auth.find_user(username)
    is_admin = bool(candidate and candidate.get("role") == "admin")
    u = auth.authenticate(username, password, ip=_client_ip(request))
    if not u:
        st = login_guard.note_failure(username, is_admin)
        if st["locked"]:
            return _login_fail(
                "ACCOUNT_LOCKED",
                "账户名或密码不对，账号已临时锁定，请稍后再试",
                423, st)
        if st["require_captcha"]:
            return _login_fail(
                "BAD_CREDENTIALS",
                "账户名或密码不对，请填写右侧 4 位数字验证码后再试",
                401, st)
        return _login_fail("BAD_CREDENTIALS", "账户名或密码错误，或账户已停用", 401, st)

    login_guard.note_success(username)          # 成功即清零，验证码与锁定状态一并解除
    token = auth.create_session(u["id"])
    auth.set_session_cookie(response, token)
    return ok(user=auth.public_user(u))


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE_NAME, "") or ""
    auth.drop_session(token)
    auth.clear_session_cookie(response)
    return ok()


# ------------------------------ 自助账户 ------------------------------

@router.post("/api/auth/password")
def change_password(payload: dict = Body(default={}), request: Request = None):
    u = current_user(request)
    if policy.is_guest(u):
        return fail("FORBIDDEN", "请登录后操作")
    new_pw = str(payload.get("new_password") or "")
    target_id = u["id"]
    if u["role"] == "admin" and payload.get("user_id"):
        target_id = str(payload["user_id"])       # 管理员重置他人密码
    elif not auth.verify_password(str(payload.get("old_password") or ""),
                                  (auth.get_user(u["id"]) or {}).get("password", "")):
        return fail("BAD_CREDENTIALS", "原密码不正确", 401)
    try:
        auth.update_user(target_id, password=new_pw)
    except ValueError as e:
        return fail("INVALID", str(e), 400)
    auth.audit(u, "account.password", target_id)
    return ok()


@router.post("/api/auth/username")
def change_username(payload: dict = Body(default={}), request: Request = None):
    u = current_user(request)
    if policy.is_guest(u):
        return fail("FORBIDDEN", "请登录后操作")
    try:
        updated = auth.update_user(u["id"],
                                   username=str(payload.get("username") or ""))
    except ValueError as e:
        return fail("INVALID", str(e), 400)
    if not updated:
        return fail("NOT_FOUND", "账户不存在", 404)
    auth.audit(u, "account.username", updated["username"])
    return ok(user=auth.public_user(updated))


# ------------------------------ 管理员：用户管理 ------------------------------

@router.get("/api/admin/users")
def admin_list_users(request: Request):
    return ok(users=auth.list_users())


@router.post("/api/admin/users")
def admin_create_user(payload: dict = Body(default={}), request: Request = None):
    u = current_user(request)
    try:
        created = auth.create_user(
            str(payload.get("username") or ""),
            str(payload.get("password") or ""),
            str(payload.get("role") or "user"))
    except ValueError as e:
        return fail("INVALID", str(e), 400)
    auth.audit(u, "admin.user.create", created["id"])
    return ok(user=auth.public_user(created))


@router.post("/api/admin/users/{uid}")
def admin_update_user(uid: str, payload: dict = Body(default={}),
                      request: Request = None):
    u = current_user(request)
    if not auth.get_user(uid):
        return fail("NOT_FOUND", "账户不存在", 404)
    patch = {k: payload[k] for k in
             ("username", "password", "role", "status", "must_change_password")
             if k in payload}
    if patch.get("status") == "inactive" and uid == u["id"]:
        return fail("INVALID", "不能停用当前登录的账户", 400)
    try:
        updated = auth.update_user(uid, **patch)
    except ValueError as e:
        return fail("INVALID", str(e), 400)
    auth.audit(u, "admin.user.update", uid)
    return ok(user=auth.public_user(updated or {}))


@router.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: str, request: Request):
    u = current_user(request)
    if uid == u["id"]:
        return fail("INVALID", "不能删除当前登录的账户", 400)
    target = auth.get_user(uid) or {}
    if target.get("role") == "admin" and auth.count_admins() <= 1:
        return fail("INVALID", "不能删除最后一个管理员", 400)
    if not auth.delete_user(uid):
        return fail("NOT_FOUND", "账户不存在", 404)
    auth.audit(u, "admin.user.delete", uid)
    return ok()
