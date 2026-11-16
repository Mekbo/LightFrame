import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_PATH = CONFIG_DIR / "config.json"
USERS_DIR = CONFIG_DIR / "users"
TOOLS_DIR = BASE_DIR / "tools" / "ffmpeg"


def lightframe_dir() -> Path:
    """光影工坊根目录：自动定位到【当前操作系统登录用户】的文档目录，
    使用 Path.home()（Windows 解析 %USERPROFILE%，类 Unix 解析 $HOME），
    换电脑 / 换账号路径均自动生效，无需硬编码用户名。"""
    return Path.home() / "Documents" / "LightFrame"


# 按产物类型分目录：图片与视频各自独立，便于后续扩展视频加工功能
LIGHTFRAME_DIR = lightframe_dir()
IMAGE_OUTPUT_DIR = LIGHTFRAME_DIR / "OutPut_Image"
VIDEO_OUTPUT_DIR = LIGHTFRAME_DIR / "OutPut_Video"


def output_dir_for(kind: str = "image") -> Path:
    """按任务类型返回对应产物目录（图片 / 视频）。"""
    return VIDEO_OUTPUT_DIR if kind == "video" else IMAGE_OUTPUT_DIR


DEFAULTS = {
    "port": 2026,
    "concurrency": 2,            # 同时执行的任务数（默认 2）
    "max_concurrency": 5,        # 最高并发限制（默认 5），并发数不得超过此值
    "preview_count": 1,          # 任务成品随机预览数量（默认 1，范围 1–6）
    # 系统更新检查周期：week=1 周 / month=1 个月 / off=不检查（全局唯一真值，默认 1 周）
    "update_check_cycle": "week",
    # 增量补丁包安装方式：manual=管理员手动点「安装更新」/ auto=空闲时自动安装（默认手动更安全）
    "update_install_mode": "manual",
    # 自动安装/重启时段：any=全天 / night=仅 22:00–06:00（默认全天）
    "update_auto_window": "any",
    # 开发者微信：公开只读展示，仅管理员可写入 / 删除。
    # developer_wechat = 微信号（展示 / 复制用，纯文本二维码扫码后需手动搜索添加）；
    # developer_wechat_link = 微信「我的二维码」解码出的名片链接（u.wechat.com 等），
    #   系统据此自动生成二维码，微信扫一扫可直接跳到添加好友页。
    # 另可上传 config/developer_wechat_qr.png（微信官方二维码图片）优先级最高。
    "developer_wechat": "",
    "developer_wechat_link": "",
    "ffmpeg_path": "",
    "output_dir": str(IMAGE_OUTPUT_DIR),          # 图片输出目录（前端已改由空间目录名推导，此处仅作兜底）
    "video_output_dir": str(VIDEO_OUTPUT_DIR),    # 视频输出目录（同上）
    # 我的空间：输出目录改为「空间/<uid>/<作品根>/<图片|视频输出名>」，
    # 以下三个名称由每位用户自行维护（相对路径，管理员/普通用户均可修改）。
    "space_root": "全部作品",        # 作品根目录名（系统保留：任何用户不可改名/删除）
    "space_img_out": "OutPut_Image",  # 图片输出子目录名（用户可改）
    "space_vid_out": "OutPut_Video",  # 视频输出子目录名（用户可改）
    # 上传限制（可配置）
    "space_upload_max_files": 10,      # 单次最多文件数
    "space_upload_max_total_mb": 200,  # 单次最大总大小（MB）
    "space_page_size": 10,             # 文件列表每页默认条数（前端提供 10/20/50）
    # 空间配额默认值（全局设置可调；新建账户按此分配，已有账户在用户管理单独调整）
    "space_quota_admin_gb": 50,         # 管理员默认空间（G）
    "space_quota_user_gb": 10,          # 普通用户默认空间（G）
    "image": {
        # 格式转换（含大小约束）
        "convert": {"enabled": False, "format": "png", "quality": 90, "max_size_mb": 3},
        # 裁切
        "crop": {"enabled": False, "mode": "9:16", "free_w": 0, "free_h": 0},
        # 去水印
        "watermark": {"enabled": False, "mode": "cover"},
        # 缩放（放大/缩小）
        "resize": {"enabled": False, "mode": "width", "value": 1080},
        # 模板加文字
        "text": {"enabled": False, "content": "", "position": "bottom-center",
                 "size": 36, "color": "#FFFFFF", "bg": False},
        # 完成通知
        "notify": True,
    },
    "video": {
        "transition": "fade",
        "transitions": [],          # 多选转场效果；为空则回落 transition / 默认 fade
        "transition_duration": 0.5,
        "duration_strategy": "fixed",   # 默认「指定每段时长」
        "target_duration": 30,
        "clip_duration": 5,             # 每段默认 5 秒；含视频素材的组后端保证每段不少于 5 秒
        "aspect": "9:16",
        "resolution": "1080p",
        "blur_sigma": 25,
        "cover": "none",
        "cover_duration": 1.0,          # 片头定格封面停留秒数
        "video_count": 1,
        # 混剪取段：repeat=同一素材可重复取不同片段（默认）；unique=素材均分、每素材一段
        "mix_mode": "repeat",
        # 每次任务是否重新随机组合片段（False=固定种子，同批素材可复现）
        "reshuffle": True,
        # 短视频素材（真实时长 < 每段目标时长）如何补足：
        # freeze=整段直接使用 + 末帧定格补到目标时长（不重复画面，推荐）；loop=循环重播补足
        "short_video_mode": "freeze",
        # 配音 / 字幕（脚本）：scripts 每项 = {subtitle, title, source}，与上传的配音音频按顺序一一对应
        "scripts": [],
        "template_id": "",          # 视频模板 id（字幕 / 顶部标题位置与样式）
        "voice_volume": 1.0,
        "bgm_volume": 0.2,          # 背景乐音量（配音为主，背景乐默认 20%）
        "bgm_fade": 0.6,            # 背景乐 / 配音淡入淡出秒数
        "subtitle_fade": 0.2,       # 字幕逐句切换时的淡入淡出秒数（让切换更柔和）
    },
    # Live 图提取：从动态照片 / iPhone 配对 / 视频文件中提取静态帧
    "live": {
        "format": "jpg",          # 输出格式：png / jpg / bmp
        "max_size_mb": 3,         # 单图大小上限（默认 3MB，可调）
        "quality": 90,            # JPG 保存质量
    },
    "add_mode": "webkitdirectory",
    "progress_channel": "websocket",
    "originality_k": 2,
    # AI 多方模型（阶段 4）：以「模型注册表」为核心，支持 OpenAI / DeepSeek / 混元 / 智谱 /
    # 阿里百炼 / Kimi / MiniMax / TTSmaker（配音）/ 自定义本地模型 等多方接入。
    # 每个模型独立存储 base_url + model + 密钥；普通用户添加「我的模型」，管理员添加可「共享」的模型。
    # 密钥不在此处，统一存放于 config/secrets.json（只在后端注入，永不下发前端）。
    "ai": {
        "timeout": 120,                # 单次 AI 调用超时（秒）——长脚本生成常超 60s，默认放宽
        "features": {
            "image_text": True,        # 图片加文字·AI 生成文案
            "video_script": True,      # 视频脚本生成
            "video_tts": True,         # 脚本配音（TTS）
            "video_title": True,       # 视频标题生成
            "video_cover": True,       # 视频封面生成
        },
        # 模型注册表：[{id,name,provider,kind:[text|image|tts],base_url,model,owner,shared,enabled,
        #              is_default,default_voice_id,voices_cache,extra}]
        "models": [],
    },
    # 局域网 / 公网部署边界（阶段 5）：本版本只落地配置位，公网项保持默认安全值。
    "server": {
        "bind_host": "0.0.0.0",        # 0.0.0.0 = 本机 + 局域网均可访问；可收窄为 127.0.0.1
        "https_enabled": False,        # 预留，本版本不启用
        "cookie_secure": False,        # 由 https_enabled 联动，本版本 false（内网 HTTP）
        "allowed_origins": [],         # 预留，空 = 同源（禁止 *）
        "lan_allow": [],               # 预留网段白名单，空 = 不限制
        "trust_proxy_headers": False,  # 预留反向代理（X-Forwarded-*）
        # 安装形态：True = 安装在服务器/局域网，所有用户须先把素材上传到「我的空间」，
        #   工作区禁止本地临时上传；False = 单机安装，用户直接从本地添加素材。
        "lan_mode": False,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def migrate_image(old: dict) -> dict:
    """把旧扁平 image 配置迁移到新的「每工具独立开关」嵌套结构。"""
    if not isinstance(old, dict):
        old = {}
    img = deep_merge(DEFAULTS["image"], {})
    img["convert"]["format"] = old.get("format", "png")
    img["convert"]["quality"] = int(old.get("quality", 90))
    img["convert"]["max_size_mb"] = float(old.get("max_size_mb", 3))
    img["crop"]["mode"] = old.get("crop", "9:16")
    img["crop"]["free_w"] = int(old.get("crop_free_w", 0) or 0)
    img["crop"]["free_h"] = int(old.get("crop_free_h", 0) or 0)
    img["watermark"]["mode"] = old.get("watermark", "cover")
    img["resize"]["mode"] = old.get("resize_mode", "width")
    img["resize"]["value"] = int(old.get("resize_value", 1080) or 0)
    img["text"]["content"] = old.get("text_content", "")
    img["text"]["position"] = old.get("text_position", "bottom-center")
    img["text"]["size"] = int(old.get("text_size", 36))
    img["text"]["color"] = old.get("text_color", "#FFFFFF")
    img["text"]["bg"] = bool(old.get("text_bg", False))
    for k in ("convert", "crop", "watermark", "resize", "text"):
        img[k]["enabled"] = False
    return img


def _load_global() -> dict:
    """加载全局基准配置（含旧结构迁移）。"""
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            user = {}
        base = deep_merge(DEFAULTS, user)
        img = user.get("image", {})
        if isinstance(img, dict) and ("resize_mode" in img or "format" in img) and "convert" not in img:
            base["image"] = migrate_image(img)
        return base
    return deep_merge(DEFAULTS, {})


def sanitize_uid(uid) -> str:
    if not uid:
        return ""
    return re.sub(r"[^A-Za-z0-9_\-]", "", str(uid))[:40]


def load_config(uid: str = "") -> dict:
    """加载配置：合并 全局基准 + 该用户的独立文件。"""
    base = _load_global()
    uid = sanitize_uid(uid)
    if uid:
        p = USERS_DIR / f"{uid}.json"
        if p.exists():
            try:
                u = json.loads(p.read_text(encoding="utf-8"))
                return deep_merge(base, u)
            except Exception:
                pass
    return base


def save_config(uid: str = "", patch: dict = None) -> dict:
    """保存配置：有 uid 则写入 config/users/{uid}.json，否则写全局。"""
    base = _load_global()
    uid = sanitize_uid(uid)
    if uid:
        USERS_DIR.mkdir(parents=True, exist_ok=True)
        p = USERS_DIR / f"{uid}.json"
        cur = {}
        if p.exists():
            try:
                cur = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                cur = {}
        merged = deep_merge(deep_merge(base, cur), patch or {})
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return merged
    merged = deep_merge(base, patch or {})
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged
