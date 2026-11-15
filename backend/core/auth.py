"""账户、会话与身份解析（阶段 1）。

设计要点（对应 docs/光影工坊_权限与AI架构设计_v1.md 第 3 节）：
- id 与 username 分离：id 不可变，用于归属 / 目录 / 个人配置；username 可改，仅用于显示与登录。
- 后端只信任服务端会话（HttpOnly Cookie），不信任任何请求参数里的 uid。
- 密码用标准库 pbkdf2_hmac + 每用户随机 salt，校验用 compare_digest；
  不引第三方依赖，便于后续打包到任意电脑。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import BASE_DIR, load_config, sanitize_uid

CONFIG_DIR = BASE_DIR / "config"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
SESSIONS_FILE = CONFIG_DIR / "sessions.json"
AUDIT_FILE = CONFIG_DIR / "audit.log"

COOKIE_NAME = "gy_session"
SESSION_DAYS = 7
PBKDF2_ROUNDS = 200_000
MIN_PASSWORD_LEN = 8

GUEST = {"id": "", "username": "", "role": "guest",
         "must_change_password": False}


# ------------------------------ 基础读写 ------------------------------

def _read_json(path: Path, default):
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, type(default)) else default
    except Exception:
        pass
    return default


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        try:
            import os
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def _now() -> str:
    return datetime.now().isoformat()


# ------------------------------ 密码 ------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_b64, hash_b64 = (stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(hash_b64)
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                  salt, int(rounds))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


# ------------------------------ 账户 ------------------------------

def load_accounts() -> dict:
    data = _read_json(ACCOUNTS_FILE, {})
    if not isinstance(data.get("users"), list):
        data = dict(data, users=[])
    return data


def save_accounts(data: dict) -> None:
    _write_json(ACCOUNTS_FILE, data)


def is_initialized() -> bool:
    return bool(load_accounts()["users"])


def new_id() -> str:
    return "u_" + secrets.token_hex(6)


def find_user(username: str) -> Optional[dict]:
    name = (username or "").strip()
    if not name:
        return None
    for u in load_accounts()["users"]:
        if u.get("username") == name:
            return u
    return None


def get_user(uid: str) -> Optional[dict]:
    for u in load_accounts()["users"]:
        if u.get("id") == uid:
            return u
    return None


def public_user(u: dict) -> dict:
    return {k: u.get(k) for k in
            ("id", "username", "role", "status", "created_at",
             "last_login_at", "must_change_password", "space_quota_bytes")}


def list_users() -> list:
    return [public_user(u) for u in load_accounts()["users"]]


def _create(username: str, password: str, role: str,
            must_change: bool = False) -> dict:
    # 我的空间配额：默认值读全局设置（space_quota_admin_gb / space_quota_user_gb）
    from .config import load_config as _load_cfg
    try:
        _quota = int(float(_load_cfg().get(
            "space_quota_admin_gb" if role == "admin" else "space_quota_user_gb")
            or (50 if role == "admin" else 10))) * (1024 ** 3)
    except Exception:
        _quota = (50 if role == "admin" else 10) * (1024 ** 3)
    u = {
        "id": new_id(),
        "username": (username or "").strip(),
        "password": hash_password(password),
        "role": role,
        "status": "active",
        "tenant_id": "",                 # 公网阶段预留
        "must_change_password": must_change,
        "space_quota_bytes": _quota,
        "created_at": _now(),
        "last_login_at": "",
        "last_login_ip": "",
    }
    data = load_accounts()
    data["users"].append(u)
    data["version"] = 1
    save_accounts(data)
    return u


def init_admin(password: str, username: str = "admin") -> dict:
    """首次运行：由管理员自己设置初始密码（仅在没有账户时可用）。"""
    if is_initialized():
        raise ValueError("系统已初始化")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    name = (username or "admin").strip() or "admin"
    return _create(name, password, "admin", must_change=False)


def create_user(username: str, password: str,
                role: str = "user") -> dict:
    name = (username or "").strip()
    if not name:
        raise ValueError("账户名不能为空")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if role not in ("admin", "user"):
        raise ValueError("角色只能为 admin 或 user")
    if find_user(name):
        raise ValueError("账户名已存在")
    return _create(name, password, role, must_change=True)


def update_user(uid: str, **patch) -> Optional[dict]:
    data = load_accounts()
    for u in data["users"]:
        if u.get("id") != uid:
            continue
        if "username" in patch:
            name = (patch["username"] or "").strip()
            if not name:
                raise ValueError("账户名不能为空")
            if any(x.get("username") == name and x.get("id") != uid
                   for x in data["users"]):
                raise ValueError("账户名已存在")
            u["username"] = name
        if "password" in patch and patch["password"]:
            if len(patch["password"]) < MIN_PASSWORD_LEN:
                raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")
            u["password"] = hash_password(patch["password"])
            u["must_change_password"] = False
        for k in ("role", "status", "must_change_password", "space_quota_bytes",
                  "last_login_at", "last_login_ip"):
            if k in patch and patch[k] is not None:
                u[k] = patch[k]
        save_accounts(data)
        return u
    return None


def delete_user(uid: str) -> bool:
    data = load_accounts()
    before = len(data["users"])
    data["users"] = [u for u in data["users"] if u.get("id") != uid]
    if len(data["users"]) == before:
        return False
    save_accounts(data)
    sessions = _read_json(SESSIONS_FILE, {})
    sessions = {k: v for k, v in sessions.items()
                if v.get("user_id") != uid}
    _write_json(SESSIONS_FILE, sessions)
    return True


def authenticate(username: str, password: str,
                 ip: str = "") -> Optional[dict]:
    u = find_user(username)
    if not u or u.get("status") != "active":
        return None
    if not verify_password(password or "", u.get("password", "")):
        return None
    update_user(u["id"], last_login_at=_now(), last_login_ip=ip or "")
    return get_user(u["id"])


def count_admins() -> int:
    return sum(1 for u in load_accounts()["users"]
               if u.get("role") == "admin" and u.get("status") == "active")


# ------------------------------ 会话 ------------------------------

def _load_sessions() -> dict:
    return _read_json(SESSIONS_FILE, {})


def create_session(uid: str) -> str:
    sessions = _load_sessions()
    token = secrets.token_urlsafe(32)
    sessions[token] = {
        "user_id": uid,
        "created_at": _now(),
        "expires_at": (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(),
    }
    _write_json(SESSIONS_FILE, sessions)
    return token


def resolve_session(token: str) -> Optional[dict]:
    if not token:
        return None
    sessions = _load_sessions()
    rec = sessions.get(token)
    if not rec:
        return None
    try:
        if datetime.fromisoformat(rec["expires_at"]) < datetime.now():
            sessions.pop(token, None)
            _write_json(SESSIONS_FILE, sessions)
            return None
    except Exception:
        return None
    u = get_user(rec.get("user_id", ""))
    if not u or u.get("status") != "active":
        return None
    return u


def drop_session(token: str) -> None:
    sessions = _load_sessions()
    if token in sessions:
        sessions.pop(token)
        _write_json(SESSIONS_FILE, sessions)


def user_from_request(request) -> dict:
    """统一身份入口：Cookie → 服务端会话 → 账户；无会话一律 guest。"""
    token = ""
    try:
        token = request.cookies.get(COOKIE_NAME, "") or ""
    except Exception:
        pass
    u = resolve_session(token)
    if not u:
        return dict(GUEST)
    return {
        "id": u.get("id", ""),
        "username": u.get("username", ""),
        "role": u.get("role", "user"),
        "must_change_password": bool(u.get("must_change_password")),
    }


def cookie_secure() -> bool:
    """Cookie 的 Secure 标记：由 server.https_enabled 或 server.cookie_secure 任一开启即生效。

    阶段 5：内网 HTTP 默认 false；一旦启用 HTTPS，Cookie 自动标记为 Secure，避免凭据在明文通道泄露。
    """
    try:
        s = (load_config().get("server") or {})
        return bool(s.get("https_enabled", False) or s.get("cookie_secure", False))
    except Exception:
        return False


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=cookie_secure(), path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/",
                           httponly=True, samesite="lax",
                           secure=cookie_secure())


# ------------------------------ 审计（只写不展示） ------------------------------

def audit(actor: dict, action: str, target: str = "") -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        who = actor.get("username") or actor.get("role") or "guest"
        line = f"{_now()}\t{who}({actor.get('id', '')})\t{action}\t{target}\n"
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def safe_uid(uid) -> str:
    """只用于内部 id 规范化，绝不接收前端自定义身份。"""
    return sanitize_uid(uid)
