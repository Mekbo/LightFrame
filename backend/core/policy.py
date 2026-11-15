"""ROLE × ACTION 权限矩阵与统一鉴权判定（阶段 2）。

原则：
- 默认拒绝（deny by default）：除显式白名单外，所有 /api 接口都要求登录。
- 后端强校验是唯一边界，前端隐藏只作体验。
- 资源归属一律用 owner_id 精确相等判断，禁止用路径字符串包含判断。
"""
from __future__ import annotations

ROLES = ("guest", "user", "admin")

# 动作 → 允许的角色（与架构文档第 4 节矩阵一一对应）
POLICY = {
    "page.browse":              {"guest", "user", "admin"},
    "auth.self":                {"guest", "user", "admin"},
    "config.read":              {"user", "admin"},
    "config.write_personal":    {"user", "admin"},
    "config.write_global":      {"admin"},
    "task.create":              {"user", "admin"},
    "task.read_own":            {"user", "admin"},
    "task.control_own":         {"user", "admin"},
    "task.read_all":            {"admin"},
    "task.clear_all":           {"admin"},
    "file.read_own":            {"user", "admin"},
    "file.delete_own":          {"user", "admin"},
    "font.read":                {"guest", "user", "admin"},
    "font.refresh":             {"admin"},
    "template.read":            {"guest", "user", "admin"},
    "template.write":           {"user", "admin"},
    "template.delete":          {"user", "admin"},   # 仅自己的 / 管理员，归属在路由层强校验
    "concurrency.read":         {"user", "admin"},
    "account.self":             {"user", "admin"},
    "admin.users":              {"admin"},
    "ai.use":                   {"user", "admin"},
    "ai.keys.self":             {"user", "admin"},
    "ai.shared":                {"admin"},
}

# 完全公开（不需要登录）
PUBLIC_EXACT = {
    ("GET", "/"),
    ("HEAD", "/"),
    ("GET", "/favicon.ico"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/auth/captcha"),        # 登录保护：4 位数字图形验证码
    ("GET", "/api/auth/login-state"),    # 登录保护：失败次数 / 锁定剩余时间
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/init"),
    ("GET", "/api/public/config"),
    ("GET", "/api/public/developer-contact"),
    ("GET", "/api/public/developer-qr.png"),
}

# 游客可只读浏览的资源（仅 GET，且不能带写操作参数）
GUEST_GET_PREFIXES = (
    "/static/",
    "/api/fonts",
    "/api/text-templates",
    "/api/video-templates",
    "/api/prompt-templates",
    "/api/template-center",
    "/api/system/update-status",   # 「关于应用 → 系统更新」版本状态：游客只读
)

ADMIN_ONLY_PREFIXES = ("/api/admin/",)


def allowed(role: str, action: str) -> bool:
    return role in POLICY.get(action, set())


def owns(task: dict, user: dict) -> bool:
    """任务归属：管理员可见全部；普通用户仅自己的；legacy（无 owner_id）仅管理员可见。"""
    if not isinstance(task, dict):
        return False
    if user.get("role") == "admin":
        return True
    uid = user.get("id") or ""
    return bool(uid) and task.get("owner_id") == uid


def is_guest(user: dict) -> bool:
    return (user or {}).get("role") == "guest"


def evaluate(method: str, path: str, query, role: str):
    """统一鉴权入口。返回 (allowed, code, message)；allowed=True 表示放行。

    在 HTTP 中间件中调用，避免权限判断散落在 30 多个路由里。
    """
    method = (method or "GET").upper()
    if method == "OPTIONS":
        return True, "", ""
    if not path.startswith("/api/"):
        return True, "", ""                      # 静态页 / 静态资源
    if (method, path) in PUBLIC_EXACT:
        return True, "", ""
    if any(path.startswith(p) for p in ADMIN_ONLY_PREFIXES):
        if role != "admin":
            return False, "FORBIDDEN", "仅管理员可执行此操作"
        return True, "", ""

    # 字体刷新仅管理员；模板删除归属（owner/shared）在路由层按记录强校验，
    # 中间件只保证游客不可写，不再一刀切限制为管理员。
    refresh = False
    try:
        refresh = str(query.get("refresh", "")).lower() in ("1", "true", "yes")
    except Exception:
        pass
    if path.startswith("/api/fonts") and refresh and role != "admin":
        return False, "FORBIDDEN", "仅管理员可刷新字体目录"

    if role == "guest":
        if method in ("GET", "HEAD") and any(
                path.startswith(p) for p in GUEST_GET_PREFIXES):
            return True, "", ""
        return False, "FORBIDDEN", "请登录后使用此功能"
    return True, "", ""


# ---------------- 模板归属（与 AI 模型注册表 owner/shared 语义对齐） ----------------

LEGACY_TEMPLATE_OWNER = "system"


def template_is_legacy(rec: dict) -> bool:
    """改造前已存在的模板没有 owner：视为系统预置，所有人只读可用、仅管理员可改。"""
    return not (rec or {}).get("owner")


def template_is_mine(rec: dict, user: dict) -> bool:
    uid = (user or {}).get("id") or ""
    return bool(uid) and (rec or {}).get("owner") == uid


def template_can_edit(rec: dict, user: dict) -> bool:
    """写权限：管理员最高；普通用户仅自己创建的；存量公共模板仅管理员。"""
    if (user or {}).get("role") == "admin":
        return True
    if template_is_legacy(rec):
        return False
    return template_is_mine(rec, user)


def template_visible(rec: dict, user: dict) -> bool:
    """读权限：自己创建的 + 别人显式共享的（只读）+ 管理员全部 + 存量公共模板。"""
    if (user or {}).get("role") == "admin":
        return True
    if template_is_legacy(rec):
        return True
    if template_is_mine(rec, user):
        return True
    return bool((rec or {}).get("shared"))


def template_public(rec: dict, user: dict) -> dict:
    """返回给前端的视图：附权限标记，供 UI 决定编辑/删除按钮是否渲染。"""
    out = dict(rec or {})
    out["owner"] = (rec or {}).get("owner") or LEGACY_TEMPLATE_OWNER
    out["owner_name"] = ((rec or {}).get("owner_name")
                         or ("系统预置" if template_is_legacy(rec) else ""))
    out["shared"] = bool((rec or {}).get("shared")) or template_is_legacy(rec)
    out["is_mine"] = template_is_mine(rec, user)
    out["can_edit"] = template_can_edit(rec, user)
    out["readonly"] = not out["can_edit"]
    return out
