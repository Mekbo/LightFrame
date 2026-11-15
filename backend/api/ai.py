"""AI 多方模型网关（阶段 4）。

对应 docs/光影工坊_权限与AI架构设计_v1.md：
- 以「模型注册表」为核心：admin 与普通用户均可添加多种大模型
  （OpenAI / DeepSeek / 腾讯混元 / 智谱 GLM / 阿里百炼 / Kimi / MiniMax / 自定义本地模型）。
- 每个模型独立保存 base_url + model + 密钥；普通用户添加「我的模型」，管理员添加的模型可任意
  勾选「共享」供普通用户使用（可同时共享多个）。
- 配音专走 TTS 通道：内置 TTSMaker 后端（免费测试 token `ttsmaker_demo_token` 可用，
  支持角色列表查询与试听），也可接 OpenAI 兼容的 /audio/speech。
- 应用各 AI 使用点（图片文案 / 视频脚本·标题·配音·封面）均可让用户自行选择模型。
- 密钥只在后端注入（config/secrets.json），任何响应都不返回明文；游客 FORBIDDEN、无可用密钥 NO_CREDENTIAL。
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

from .auth import current_user, fail
from ..core import config as cfg_mod
from ..core.config import BASE_DIR, load_config, save_config

CONFIG_DIR = BASE_DIR / "config"
SECRETS_FILE = CONFIG_DIR / "secrets.json"
USAGE_FILE = CONFIG_DIR / "ai_usage.json"

router = APIRouter()


# ------------------------------ 模型预设（provider） ------------------------------

# caps: 该 provider 通过 OpenAI 兼容协议能提供的能⼒；tts=True 表示走专用 TTS 后端。
PROVIDERS = {
    "openai":    {"label": "OpenAI",            "base_url": "https://api.openai.com/v1",                       "caps": ["text", "image", "tts"]},
    "deepseek":  {"label": "DeepSeek",          "base_url": "https://api.deepseek.com/v1",                     "caps": ["text"]},
    "hunyuan":   {"label": "腾讯混元",           "base_url": "https://api.hunyuan.cloud.tencent.com/v1",       "caps": ["text", "image"]},
    "zhipu":     {"label": "智谱 GLM",          "base_url": "https://open.bigmodel.cn/api/paas/v4",            "caps": ["text", "image"]},
    # 百炼自 2026-09-30 起旧 DashScope 域名不再支持新特性，改用「业务空间专属域名」
    # https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
    # WorkspaceId 因账号/业务空间而异，不能硬编码，故此处不预填，由用户粘贴控制台的 API Host。
    # 百炼的语音合成（qwen-audio-3.1-tts-*）走原生 HTTP 接口，不是 OpenAI 的 /audio/speech：
    #   POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer
    # 因此同一 provider 同时承载文本/图片（compatible-mode）与配音（api/v1），后端会自动归一域名。
    "dashscope": {"label": "阿里百炼",           "base_url": "", "caps": ["text", "image", "tts"],
                  "require_base_url": True,
                  "hint": "请粘贴百炼控制台的 API Host：https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1（旧域名 dashscope.aliyuncs.com 自 2026-09-30 起不再支持新特性）。做配音时 model 填 qwen-audio-3.1-tts-plus 或 qwen-audio-3.1-tts-flash，地址填同一个 API Host 即可，无需改路径。"},
    "kimi":      {"label": "Kimi（月之暗面）",   "base_url": "https://api.moonshot.cn/v1",                     "caps": ["text"]},
    "minimax":   {"label": "MiniMax",           "base_url": "https://api.minimax.chat/v1",                     "caps": ["text"]},
    "doubao":    {"label": "豆包（火山方舟）",    "base_url": "https://ark.cn-beijing.volces.com/api/v3",        "caps": ["text"]},
    "ttsmaker":  {"label": "TTSMaker（配音）",   "base_url": "https://api.ttsmaker.cn/v1",                     "caps": ["tts"], "tts": True},
    "custom":    {"label": "自定义 / 本地模型",   "base_url": "",                                               "caps": ["text", "image", "tts"]},
}

CAP_LABELS = {"text": "文本", "image": "图片", "tts": "配音"}


# ------------------------------ 基础读写 ------------------------------

def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            import os
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def _mask_key(k: str) -> str:
    k = (k or "").strip()
    if not k:
        return ""
    if len(k) <= 6:
        return "***"
    return k[:4] + "*" * max(4, len(k) - 8) + k[-4:]


def _gen_id(prefix: str = "m") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ------------------------------ 异常与结果 ------------------------------

class AIError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def ai_result(ok: bool, data=None, usage=None, error=None) -> dict:
    return {"ok": ok, "data": data or {}, "usage": usage or {}, "error": error or {}}


# ------------------------------ 密钥管理 ------------------------------

def load_secrets() -> dict:
    data = _read_json(SECRETS_FILE, {})
    if not isinstance(data.get("shared"), dict):
        data["shared"] = {}
    if not isinstance(data.get("personal"), dict):
        data["personal"] = {}
    return data


def save_secrets(data: dict) -> None:
    _write_json(SECRETS_FILE, data)


def ai_cfg() -> dict:
    return load_config().get("ai", {}) or {}


def record_usage(entry: dict) -> None:
    """仅记录用量与错误码，不记密钥、不记完整文案。"""
    try:
        rows = _read_json(USAGE_FILE, [])
        if not isinstance(rows, list):
            rows = []
        rows.append(entry)
        if len(rows) > 2000:
            rows = rows[-2000:]
        _write_json(USAGE_FILE, rows)
    except Exception:
        pass


# ------------------------------ 模型注册表读写 ------------------------------

def _models() -> list:
    return list((ai_cfg().get("models") or []))


def _save_models(models: list) -> None:
    save_config("", {"ai": {"models": models}})


def _find_model(model_id: str) -> Optional[dict]:
    for m in _models():
        if m.get("id") == model_id:
            return m
    return None


# ------------------------------ 后端实现 ------------------------------

class OpenAIBackend:
    """OpenAI 兼容协议：/chat/completions、/audio/speech、/images/generations。"""

    def __init__(self, base_url: str, model: str, timeout: int = 60):
        self.base = (base_url or "").rstrip("/")
        self.model = model or ""
        self.timeout = max(5, int(timeout))

    def _headers(self, api_key: str) -> dict:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def generate_text(self, prompt: str, system: Optional[str], api_key: str) -> dict:
        if not self.base:
            raise AIError("NO_CREDENTIAL", "未配置 base_url（请在「我的模型」中填写）")
        if not self.model:
            raise AIError("NO_CREDENTIAL", "未配置 model（请在「我的模型」中填写）")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        # 超时 / 连接抖动自动重试一次（长脚本生成常超过一次读超时；换条连接往往就通了）
        last_exc: Exception = None
        for attempt in range(2):
            try:
                r = httpx.post(self.base + "/chat/completions",
                               headers=self._headers(api_key),
                               json={"model": self.model, "messages": messages,
                                     "temperature": 0.8, "max_tokens": 2000},
                               timeout=self.timeout)
                break
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt == 0:
                    time.sleep(1.2)
                    continue
                hint = "（已自动重试 1 次仍失败）"
                if isinstance(e, httpx.TimeoutException):
                    hint += "可稍后重试，或在「AI 大模型 → 功能设置」把超时调大"
                raise AIError("PROVIDER_ERROR",
                              f"上游模型服务超时/网络异常：{e}{hint}")
        r.raise_for_status()
        d = r.json()
        text = d["choices"][0]["message"]["content"]
        usage = d.get("usage", {}) or {}
        return ai_result(True, {"text": text.strip()},
                        usage={"prompt_tokens": usage.get("prompt_tokens"),
                               "completion_tokens": usage.get("completion_tokens"),
                               "chars": len(text)})

    def generate_tts(self, text: str, api_key: str, voice: str = "alloy") -> bytes:
        if not self.base:
            raise AIError("NO_CREDENTIAL", "未配置 base_url")
        if not self.model:
            raise AIError("NO_CREDENTIAL", "未配置 model")
        r = httpx.post(self.base + "/audio/speech",
                       headers=self._headers(api_key),
                       json={"model": self.model, "input": text,
                             "voice": voice, "response_format": "mp3"},
                       timeout=self.timeout)
        r.raise_for_status()
        return r.content

    def generate_image(self, prompt: str, api_key: str) -> bytes:
        if not self.base:
            raise AIError("NO_CREDENTIAL", "未配置 base_url")
        if not self.model:
            raise AIError("NO_CREDENTIAL", "未配置 model")
        r = httpx.post(self.base + "/images/generations",
                       headers=self._headers(api_key),
                       json={"model": self.model, "prompt": prompt, "n": 1,
                             "size": "1024x1024", "response_format": "b64_json"},
                       timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        item = (d.get("data") or [{}])[0]
        b64 = item.get("b64_json") or _url_to_b64(item.get("url"), api_key)
        if not b64:
            raise AIError("PROVIDER_ERROR", "图片生成返回为空")
        return base64.b64decode(b64)

    def selfcheck(self, api_key: str) -> dict:
        if not self.base or not self.model:
            return ai_result(False, error={"code": "NO_CREDENTIAL",
                                          "message": "未配置 base_url 或 model"})
        t0 = time.time()
        try:
            httpx.post(self.base + "/chat/completions",
                       headers=self._headers(api_key),
                       json={"model": self.model, "messages": [{"role": "user", "content": "ping"}],
                             "max_tokens": 1},
                       timeout=min(self.timeout, 15))
            return ai_result(True, {"connected": True, "model": self.model,
                                   "latency_ms": int((time.time() - t0) * 1000)})
        except Exception as e:
            return ai_result(False, {"connected": False, "model": self.model,
                                    "latency_ms": int((time.time() - t0) * 1000)},
                              error={"code": "PROVIDER_ERROR", "message": str(e)[:200]})


def _url_to_b64(url: Optional[str], api_key: str) -> Optional[str]:
    if not url:
        return None
    try:
        r = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
        r.raise_for_status()
        return base64.b64encode(r.content).decode("ascii")
    except Exception:
        return None


def normalize_voice(v: dict) -> dict:
    """TTSMaker 官方字段为 id/name/language/audio_sample_file_url，统一成前端稳定契约。"""
    v = v or {}
    return {
        "id": v.get("id"),
        "name": v.get("name") or f"角色 {v.get('id')}",
        "language": v.get("language") or "",
        "gender": v.get("gender"),
        "sample_url": v.get("audio_sample_file_url") or v.get("sample_mp3_url") or "",
        "text_characters_limit": v.get("text_characters_limit"),
        "is_need_queue": v.get("is_need_queue"),
    }


class TTSMakerBackend:
    """TTSMaker 配音：create-tts-order 返回临时音频 URL（2h），再下载为字节。"""

    def __init__(self, base_url: str = "https://api.ttsmaker.cn/v1", timeout: int = 60):
        self.base = (base_url or "https://api.ttsmaker.cn/v1").rstrip("/")
        self.timeout = max(5, int(timeout))

    def _post(self, path: str, params: dict) -> dict:
        r = httpx.post(self.base + path,
                       headers={"Content-Type": "application/json; charset=utf-8"},
                       json=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_voices(self, token: str, language: Optional[str] = None) -> list:
        params = {"token": token}
        if language:
            params["language"] = language
        r = httpx.get(self.base + "/get-voice-list", params=params, timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "success":
            raise AIError("PROVIDER_ERROR", d.get("error_details") or "获取角色列表失败")
        voices = [normalize_voice(v) for v in (d.get("voices_detailed_list") or [])]
        if language:
            lang = str(language).lower()
            voices = [v for v in voices if lang in str(v.get("language") or "").lower()]
        # 中文角色优先，其余按语言/名称排序；前端下拉直接可用
        voices.sort(key=lambda v: (
            0 if str(v.get("language") or "").lower().startswith("zh") else 1,
            str(v.get("language") or ""), str(v.get("name") or "")))
        return voices

    def generate_tts(self, text: str, token: str, voice_id: int, audio_format: str = "mp3") -> bytes:
        if not voice_id:
            raise AIError("NO_CREDENTIAL", "未选择配音角色（voice_id）")
        d = self._post("/create-tts-order", {
            "token": token, "text": text, "voice_id": int(voice_id),
            "audio_format": audio_format, "audio_speed": 1.0,
        })
        if d.get("status") != "success" or not d.get("audio_file_url"):
            raise AIError("PROVIDER_ERROR", d.get("error_details") or "TTSMaker 合成失败")
        url = d["audio_file_url"]
        r = httpx.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.content

    def selfcheck(self, token: str) -> dict:
        if not token:
            return ai_result(False, error={"code": "NO_CREDENTIAL", "message": "未配置 TTSMaker token"})
        t0 = time.time()
        try:
            r = httpx.get(self.base + "/get-token-status", params={"token": token}, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
            ok = d.get("error_code") in (None, "0", "", "success")
            return ai_result(ok, {"connected": ok, "model": "ttsmaker",
                                  "latency_ms": int((time.time() - t0) * 1000),
                                  "token_status": d.get("token_status")},
                             error=None if ok else {"code": "PROVIDER_ERROR",
                                                   "message": d.get("error_details") or "token 无效"})
        except Exception as e:
            return ai_result(False, {"connected": False, "model": "ttsmaker",
                                    "latency_ms": int((time.time() - t0) * 1000)},
                             error={"code": "PROVIDER_ERROR", "message": str(e)[:200]})


# ------------------------------ 阿里百炼 Qwen-Audio-TTS ------------------------------
# 官方音色列表：https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list
# 音色必须属于当前模型，跨模型混用会返回 InvalidParameter（TTS speak operation failed）。
_QWEN_TTS_PLUS_VOICES = [
    ("longanhuan_v3.1", "龙安欢", "女", "zh"),
    ("longanlingxin_v3.1", "龙安灵心", "女", "zh"),
    ("longanfengyue_v3.1", "龙安风悦", "女", "zh"),
    ("xunanchuan_v3.1", "许南川", "男", "zh"),
]
_QWEN_TTS_FLASH_EXTRA = [
    ("yuxiaoyun_v3.1", "于小云", "女", "zh"), ("qiaoxiaojiao_v3.1", "乔小娇", "女", "zh"),
    ("xiaxiaochen_v3.1", "夏小晨", "女", "zh"), ("anmingyuan_v3.1", "安明远", "男", "zh"),
    ("wenhuaiqing_v3.1", "温怀清", "女", "zh"), ("anxiaolan_v3.1", "安小岚", "女", "zh"),
    ("xieshurou_v3.1", "谢舒柔", "女", "zh"), ("baiqinglan_v3.1", "白清岚", "女", "zh"),
    ("xuyuyuan_v3.1", "许玉远", "女", "zh"), ("anruorou_v3.1", "安若柔", "女", "zh"),
    ("wenhuaizhi_v3.1", "闻怀之", "女", "zh"), ("xiaoxingzhi_v3.1", "萧行之", "女", "zh"),
    ("guyunshu_v3.1", "顾云舒", "女", "zh"), ("huozhuoshi_v3.1", "霍拙石", "男", "zh"),
    ("yeqinghe_v3.1", "叶清禾", "女", "zh"), ("yunhuanhuan_v3.1", "云欢欢", "女", "zh"),
    ("xuxiaoqiao_v3.1", "徐小俏", "女", "zh"), ("baianran_v3.1", "白安然", "女", "zh"),
    ("xuyanchu_v3.1", "许言初", "女", "zh"), ("yezhiqing_v3.1", "叶知晴", "女", "zh"),
    ("andi_v3.1", "安迪", "男", "zh"), ("anyuqing_v3.1", "安语晴", "女", "zh"),
    ("linxiaoling_v3.1", "林小灵", "女", "zh"), ("longfeifei_v3.1", "龙菲菲", "女", "zh"),
    ("longjing_v3.1", "龙婧", "女", "zh"), ("longqiang_v3.1", "龙嫱", "女", "zh"),
    ("longyan_v3.1", "龙颜", "女", "zh"), ("longxiaobai_v3.1", "龙小白", "女", "zh"),
    ("longyue_v3.1", "龙悦", "女", "zh"), ("longcheng_v3.1", "龙橙", "男", "zh"),
    ("longlaotie_v3.1", "龙老铁", "男", "zh"), ("longshao_v3.1", "龙邵", "男", "zh"),
    ("longshu_v3.1", "龙书", "男", "zh"), ("longze_v3.1", "龙泽", "男", "zh"),
    ("longhao_v3.1", "龙浩", "男", "zh"), ("longjin_v3.1", "龙津", "男", "zh"),
    ("longnan_v3.1", "龙楠", "男", "zh"), ("longshuo_v3.1", "龙硕", "男", "zh"),
    ("longtian_v3.1", "龙天", "男", "zh"), ("longxiaocheng_v3.1", "龙小诚", "男", "zh"),
    ("longke_v3.1", "龙可", "儿童", "zh"), ("loongbella_v3.1", "Bella", "女", "zh"),
]
_QWEN_TTS_EN_VOICES = [
    ("Emily_v3.1", "Emily", "女", "en"), ("Luna_v3.1", "Luna", "女", "en"),
    ("Eric_v3.1", "Eric", "男", "en"), ("Luca_v3.1", "Luca", "男", "en"),
    ("Abby_v3.1", "Abby", "女", "en"), ("Annie_v3.1", "Annie", "女", "en"),
    ("Ava_v3.1", "Ava", "女", "en"), ("Beth_v3.1", "Beth", "女", "en"),
    ("Betty_v3.1", "Betty", "女", "en"), ("Cally_v3.1", "Cally", "女", "en"),
    ("Cindy_v3.1", "Cindy", "女", "en"), ("Donna_v3.1", "Donna", "女", "en"),
    ("Andy_v3.1", "Andy", "男", "en"), ("Brian_v3.1", "Brian", "男", "en"),
    ("David_v3.1", "David", "男", "en"),
]
DASHSCOPE_TTS_VOICES = {
    "qwen-audio-3.1-tts-plus": _QWEN_TTS_PLUS_VOICES,
    "qwen-audio-3.1-tts-flash": _QWEN_TTS_PLUS_VOICES + _QWEN_TTS_FLASH_EXTRA + _QWEN_TTS_EN_VOICES,
}
DASHSCOPE_TTS_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"


def dashscope_tts_voices(model: str) -> list:
    """系统音色是静态清单（官方无「拉取音色」接口），按模型名返回对应音色。"""
    key = (model or "").strip().lower()
    if key in DASHSCOPE_TTS_VOICES:
        bucket = DASHSCOPE_TTS_VOICES[key]
    elif "flash" in key:
        bucket = DASHSCOPE_TTS_VOICES["qwen-audio-3.1-tts-flash"]
    elif "plus" in key or "tts" in key:
        bucket = DASHSCOPE_TTS_VOICES["qwen-audio-3.1-tts-plus"]
    else:
        # 未收录的模型（如 cosyvoice 系）音色 id 完全不同，不猜，交给用户填默认音色
        bucket = []
    out = []
    for vid, name, gender, lang in bucket:
        out.append({"id": vid, "name": f"{name}（{gender}）", "language": lang,
                    "gender": gender, "sample_url": ""})
    return out


def dashscope_native_base(base_url: str) -> str:
    """把控制台 API Host 归一成业务空间根域名。

    用户通常粘贴 .../compatible-mode/v1（文本/图片用），而配音接口在 .../api/v1/...，
    这里去掉兼容模式后缀，两种填法都能用。
    """
    b = (base_url or "").strip().rstrip("/")
    for suffix in ("/compatible-mode/v1", "/compatible-mode", "/api-ws/v1/inference",
                   "/api/v1/services/audio/tts/SpeechSynthesizer", "/api/v1"):
        if b.endswith(suffix):
            b = b[: -len(suffix)].rstrip("/")
            break
    if b.startswith("wss://"):
        b = "https://" + b[len("wss://"):]
    elif b.startswith("ws://"):
        b = "http://" + b[len("ws://"):]
    elif b and not b.startswith("http"):
        b = "https://" + b
    return b


def _dashscope_error(r) -> str:
    try:
        d = r.json()
        msg = d.get("message") or d.get("msg") or ""
        code = d.get("code") or ""
        if msg:
            return f"{code}：{msg}" if code else str(msg)
        return str(d)[:200]
    except Exception:
        return (r.text or "")[:200] or f"HTTP {r.status_code}"


_TTS_AUDIO_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_TTS_AUDIO_CACHE_MAX = 12


def _oss_download(url: str, timeout: int, rounds: int = 3) -> bytes:
    """下载百炼返回的 OSS 预签名音频。

    刚合成完时对象可能还没在 OSS 落地，此时预签名 GET 返回的是 403 AccessDenied
    （而不是 404），属于秒级抖动。这里做几轮退避重试，并降级尝试 https +
    X-DashScope-OssResourceResolve 头，避免一次抖动就让试听失败。
    """
    combos = [(url, {}), (url, {"X-DashScope-OssResourceResolve": "enable"})]
    if url.startswith("http://"):
        https_url = "https://" + url[len("http://"):]
        combos += [(https_url, {}), (https_url, {"X-DashScope-OssResourceResolve": "enable"})]
    last = ""
    for rnd in range(max(1, rounds)):
        for u, hdr in combos:
            try:
                rr = httpx.get(u, headers=hdr, timeout=timeout, follow_redirects=True)
            except Exception as e:                     # 网络抖动
                last = f"{type(e).__name__}: {e}"[:100]
                continue
            if rr.status_code == 200 and rr.content:
                return rr.content
            last = f"HTTP {rr.status_code}"
        if rnd < rounds - 1:
            time.sleep(0.6 * (rnd + 1))
    raise AIError("PROVIDER_ERROR",
                  f"配音已合成，但音频下载失败（{last}）。音频地址有时效性，请再点一次试听重试")


class DashScopeTTSBackend:
    """百炼语音合成（qwen-audio-3.1-tts-*）：非流式 HTTP，返回 24h 有效的音频 URL。

    注意：这不是 OpenAI 协议的 /audio/speech，官方明确该模型不支持 /v1/audio/speech。
    """

    def __init__(self, base_url: str, model: str, timeout: int = 60):
        self.raw_base = (base_url or "").strip()
        self.base = dashscope_native_base(base_url)
        self.model = (model or "").strip()
        self.timeout = max(10, int(timeout))

    def _endpoint(self) -> str:
        if not self.base:
            raise AIError("NO_CREDENTIAL", "未配置百炼业务空间地址（base_url）")
        return self.base + DASHSCOPE_TTS_PATH

    def get_voices(self, token: Optional[str] = None, language: Optional[str] = None) -> list:
        return dashscope_tts_voices(self.model)

    def generate_tts(self, text: str, api_key: str, voice: Optional[str] = None,
                     audio_format: str = "mp3") -> bytes:
        if not self.model:
            raise AIError("NO_CREDENTIAL", "未配置 model（如 qwen-audio-3.1-tts-plus）")
        voices = self.get_voices()
        ids = [v["id"] for v in voices]
        v = (voice or "").strip() or (ids[0] if ids else "longanhuan_v3.1")
        if ids and v not in ids:
            # 音色与模型不匹配时官方直接报错，这里回落到该模型的默认音色
            v = ids[0]
        ck = (self.base, self.model, v, audio_format, text)
        hit = _TTS_AUDIO_CACHE.get(ck)
        if hit:
            _TTS_AUDIO_CACHE.move_to_end(ck)
            return hit
        payload = {"model": self.model,
                   "input": {"text": text, "voice": v, "format": audio_format,
                             "sample_rate": 24000}}
        r = httpx.post(self._endpoint(),
                       headers={"Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json"},
                       json=payload, timeout=self.timeout)
        if r.status_code >= 400:
            raise AIError("PROVIDER_ERROR", "百炼配音失败：" + _dashscope_error(r))
        d = r.json()
        audio = ((d.get("output") or {}).get("audio") or {})
        url = (audio.get("url") or "").strip()
        if url:
            data = _oss_download(url, self.timeout)
        elif audio.get("data"):
            data = base64.b64decode(audio["data"])
        else:
            raise AIError("PROVIDER_ERROR", "百炼配音返回为空：" + str(d)[:160])
        _TTS_AUDIO_CACHE[ck] = data
        while len(_TTS_AUDIO_CACHE) > _TTS_AUDIO_CACHE_MAX:
            _TTS_AUDIO_CACHE.popitem(last=False)
        return data

    def selfcheck(self, api_key: str) -> dict:
        t0 = time.time()
        try:
            data = self.generate_tts("你好，这是配音通道自检。", api_key)
            return ai_result(True, {"connected": True, "model": self.model,
                                   "voice": self.get_voices()[0]["id"] if self.get_voices() else "",
                                   "bytes": len(data),
                                   "latency_ms": int((time.time() - t0) * 1000)})
        except AIError as e:
            return ai_result(False, {"connected": False, "model": self.model,
                                    "latency_ms": int((time.time() - t0) * 1000)},
                             error={"code": e.code, "message": e.message})
        except Exception as e:
            return ai_result(False, {"connected": False, "model": self.model,
                                    "latency_ms": int((time.time() - t0) * 1000)},
                             error={"code": "PROVIDER_ERROR", "message": str(e)[:200]})


# ------------------------------ 服务编排 ------------------------------

class AIService:
    def _backend_for(self, m: dict, capability: Optional[str] = None):
        cap_timeout = int(ai_cfg().get("timeout", 60) or 60)
        provider = m.get("provider")
        if PROVIDERS.get(provider, {}).get("tts"):
            return TTSMakerBackend(m.get("base_url") or PROVIDERS["ttsmaker"]["base_url"], cap_timeout)
        # 百炼配音走原生 HTTP（/api/v1/services/audio/tts/SpeechSynthesizer），
        # 同一模型的文本/图片仍走 OpenAI 兼容协议，按调用能力分流。
        if provider == "dashscope" and capability == "tts":
            return DashScopeTTSBackend(m.get("base_url") or "", m.get("model", ""), cap_timeout)
        return OpenAIBackend(m.get("base_url") or "", m.get("model", ""), cap_timeout)

    # ---- 可见性 / 密钥解析 ----
    @staticmethod
    def owns(user: dict, m: dict) -> bool:
        """「自己创建的」模型：个人模型 owner=自己的 uid；管理员创建的公共模型
        owner 固定为字符串 "admin"（历史数据约定），也归管理员自己，否则管理员
        反而看不到自己建的公司公共模型池。"""
        owner = m.get("owner")
        if owner == (user or {}).get("id", ""):
            return True
        return owner == "admin" and (user or {}).get("role") == "admin"

    def visible_to(self, user: dict, m: dict) -> bool:
        """统一加载规则（管理员 / 普通用户完全一致）：
        ① 自己创建的模型；② 别人显式开启共享且已启用的模型。
        不加载其他人的私人模型 —— 管理员不再有「看到全部模型」的特权。
        使用点下拉另由 list_usable() 过滤掉未配密钥的模型。
        """
        if self.owns(user, m):
            return True
        uid = (user or {}).get("id", "")
        owner = m.get("owner")
        if not owner or owner == uid:
            return False
        return bool(m.get("shared")) and bool(m.get("enabled", True))

    @staticmethod
    def shared_key_for(m: dict) -> str:
        """共享模型的密钥：优先取服务端共享密钥池（管理员公共模型）；若模型是由
        普通用户共享出来的，其密钥仍在原所有者的 personal 区，回退读取。
        调用方必须先通过 visible_to()，确保只读取「已显式共享」模型的密钥。"""
        secrets = load_secrets()
        k = (secrets.get("shared", {}) or {}).get(m.get("id"), "") or ""
        if k:
            return k
        owner = m.get("owner")
        return ((secrets.get("personal", {}) or {}).get(owner, {}) or {}).get(m.get("id"), "") or ""

    def key_for(self, user: dict, m: dict) -> str:
        secrets = load_secrets()
        uid = (user or {}).get("id", "")
        if m.get("owner") == uid:
            return (secrets.get("personal", {}).get(uid, {}) or {}).get(m["id"], "") or ""
        return self.shared_key_for(m)

    def resolve(self, user: dict, model_id: str, capability: str):
        """返回 (model_def, api_key, backend)；不可用抛 AIError。"""
        role = (user or {}).get("role")
        if role in (None, "", "guest"):
            raise AIError("FORBIDDEN", "游客不能使用 AI 功能，请先登录", status=403)
        m = _find_model(model_id)
        if not m:
            raise AIError("NO_CREDENTIAL", "模型不存在或已被删除（请重新选择模型）")
        if capability not in (m.get("kind") or []):
            raise AIError("BAD_REQUEST", f"该模型不支持「{CAP_LABELS.get(capability, capability)}」能力")
        if not self.visible_to(user, m):
            raise AIError("FORBIDDEN", "无权使用该模型", status=403)
        key = self.key_for(user, m).strip()
        if not key:
            raise AIError("NO_CREDENTIAL",
                          f"模型「{m.get('name','')}」未配置密钥：请在「AI 大模型」中为该模型填写密钥",
                          status=400)
        return m, key, self._backend_for(m, capability)

    # ---- 列表（供下拉与面板） ----
    def list_for_user(self, user: dict, capability: Optional[str] = None) -> list:
        out = []
        for m in _models():
            if capability and capability not in (m.get("kind") or []):
                continue
            if not self.visible_to(user, m):
                continue
            out.append(self._pub(m, user))
        return out

    def list_usable(self, user: dict, capability: Optional[str] = None) -> list:
        """仅返回「对当前用户可见且已配置密钥」的模型（使用点下拉与回落只认这些）。"""
        return [m for m in self.list_for_user(user, capability) if m.get("has_key")]

    def manage_view(self, user: dict) -> dict:
        role = (user or {}).get("role")
        uid = (user or {}).get("id", "")
        personal, admin_all, shared_avail = [], [], []
        for m in _models():
            if m.get("owner") == uid:
                # 自己的个人模型
                personal.append(self._pub(m, user, is_mine=True))
            elif role == "admin" and m.get("owner") == "admin":
                # 管理员自己创建的公共模型：单独分区，便于开关共享
                admin_all.append(self._pub(m, user))
            elif self.visible_to(user, m):
                # 别人显式共享出来的模型（管理员与普通用户同一套规则）
                shared_avail.append(self._pub(m, user))
        return {
            "is_admin": role == "admin",
            "features": ai_cfg().get("features", {}),
            "timeout": int(ai_cfg().get("timeout", 120) or 120),
            "providers": {k: {"label": v["label"], "base_url": v["base_url"],
                              "caps": v["caps"], "hint": v.get("hint", ""),
                              "require_base_url": bool(v.get("require_base_url"))}
                          for k, v in PROVIDERS.items()},
            "personal": personal,
            "admin_all": admin_all,
            "shared_available": shared_avail,
        }

    def _pub(self, m: dict, user: dict, *, is_mine: bool = False) -> dict:
        secrets = load_secrets()
        uid = (user or {}).get("id", "")
        if m.get("owner") == uid:
            has_key = bool((secrets.get("personal", {}).get(uid, {}) or {}).get(m["id"]))
        else:
            has_key = bool(self.shared_key_for(m))
        return {
            "id": m["id"], "name": m.get("name", ""), "provider": m.get("provider", ""),
            "provider_label": PROVIDERS.get(m.get("provider"), {}).get("label", m.get("provider", "")),
            "kind": m.get("kind", []), "shared": bool(m.get("shared")),
            "owner": m.get("owner"), "enabled": bool(m.get("enabled", True)),
            "is_default": bool(m.get("is_default")),
            "default_voice_id": m.get("default_voice_id"),
            "base_url": m.get("base_url", ""), "model": m.get("model", ""),
            "has_key": has_key, "is_mine": is_mine,
            "voice_count": len(m.get("voices_cache") or []),
        }

    # ---- 增删改 ----
    def _normalize(self, payload: dict, *, owner: str, shared: bool) -> dict:
        provider = (payload.get("provider") or "custom").strip()
        if provider not in PROVIDERS:
            raise AIError("BAD_REQUEST", f"不支持的 provider：{provider}")
        preset = PROVIDERS[provider]
        kind = [c for c in (payload.get("kind") or []) if c in preset["caps"]]
        if not kind:
            kind = list(preset["caps"])
        base_url = (payload.get("base_url") or "").strip()
        if not base_url and provider != "custom":
            base_url = preset["base_url"]
        if not base_url and (provider == "custom" or preset.get("require_base_url")):
            raise AIError("BAD_REQUEST", preset.get("hint") or "该模型必须填写 base_url")
        model = (payload.get("model") or "").strip()
        if "tts" not in kind and not model:
            raise AIError("BAD_REQUEST", "文本/图片模型必须填写 model")
        if provider == "dashscope" and "tts" in kind and not model:
            raise AIError("BAD_REQUEST",
                          "百炼配音必须填写 model：qwen-audio-3.1-tts-plus（4 个旗舰音色，质量最好）"
                          "或 qwen-audio-3.1-tts-flash（音色更多、更便宜）")
        return {
            "id": payload.get("id") or _gen_id(),
            "name": (payload.get("name") or "").strip() or preset["label"],
            "provider": provider, "kind": kind, "base_url": base_url, "model": model,
            "owner": owner, "shared": bool(shared),
            "enabled": True,
            "is_default": bool(payload.get("is_default")),
            "default_voice_id": payload.get("default_voice_id") or None,
            "voices_cache": payload.get("voices_cache") or [],
            "extra": payload.get("extra") or {},
        }

    def _prepare_update(self, models: list, payload: dict, owner: str):
        """编辑既有模型：校验归属、保留角色缓存；返回 (待写列表, existing, key)。

        api_key 留空在「编辑」时表示保持原密钥，在「新建」时不允许（只加载已配密钥模型）。
        """
        mid = payload.get("id")
        existing = _find_model(mid) if mid else None
        if mid and not existing:
            raise AIError("BAD_REQUEST", "模型不存在或已被删除，请重新添加")
        if existing and existing.get("owner") != owner:
            raise AIError("FORBIDDEN", "无权编辑该模型（仅创建者或管理员可修改）", status=403)
        if existing:
            models = [m for m in models if m.get("id") != mid]
        key = (payload.get("api_key") or "").strip()
        if not key and not existing:
            raise AIError("BAD_REQUEST", "新建模型必须填写 API Key / Token")
        return models, existing, key

    @staticmethod
    def _keep_voices(m: dict, existing: dict, payload: dict):
        """编辑时若表单未重新拉取角色列表，保留已有角色缓存。"""
        if existing and not payload.get("voices_cache"):
            m["voices_cache"] = existing.get("voices_cache") or []

    def add_personal(self, user: dict, payload: dict, api_key: str) -> dict:
        uid = (user or {}).get("id", "")
        if not uid:
            raise AIError("FORBIDDEN", "请先登录", status=403)
        models, existing, key = self._prepare_update(_models(), {**payload, "api_key": api_key}, uid)
        # 共享状态：表单显式传入则以表单为准；编辑时未带该字段则保持原状，
        # 避免「改个名称」把已共享的模型意外取消共享。
        if "shared" in (payload or {}):
            shared = bool(payload.get("shared"))
        else:
            shared = bool((existing or {}).get("shared"))
        m = self._normalize(payload, owner=uid, shared=shared)
        self._keep_voices(m, existing, payload)
        models.append(m)
        _save_models(models)
        # 编辑留空 = 保持原密钥；仅在没有原密钥时才回退写入
        if key or not existing:
            self._store_key(user, m["id"], key, personal_uid=uid)
        return {"ok": True, "model": self._pub(m, user, is_mine=True),
                "edited": bool(existing)}

    def add_admin(self, user: dict, payload: dict, api_key: str, shared: bool) -> dict:
        if (user or {}).get("role") != "admin":
            raise AIError("FORBIDDEN", "仅管理员可添加共享模型", status=403)
        models, existing, key = self._prepare_update(
            _models(), {**payload, "api_key": api_key}, "admin")
        m = self._normalize(payload, owner="admin", shared=shared)
        self._keep_voices(m, existing, payload)
        models.append(m)
        _save_models(models)
        if key or not existing:
            self._store_key(user, m["id"], key, shared=True)
        return {"ok": True, "model": self._pub(m, user), "edited": bool(existing)}

    def delete_model(self, user: dict, model_id: str) -> dict:
        m = _find_model(model_id)
        if not m:
            raise AIError("BAD_REQUEST", "模型不存在")
        uid = (user or {}).get("id", "")
        role = (user or {}).get("role")
        if m.get("owner") != uid and role != "admin":
            raise AIError("FORBIDDEN", "无权删除该模型", status=403)
        models = [x for x in _models() if x.get("id") != model_id]
        _save_models(models)
        secrets = load_secrets()
        personal = secrets.get("personal") or {}
        # 按模型真实归属清理密钥：管理员代删他人共享模型时，
        # 原所有者的 personal 区也要一并清掉，避免残留孤立密钥。
        for who in {m.get("owner") or uid, uid}:
            bucket = personal.get(who)
            if isinstance(bucket, dict):
                bucket.pop(model_id, None)
        (secrets.get("shared") or {}).pop(model_id, None)
        save_secrets(secrets)
        return {"ok": True}

    def set_shared(self, user: dict, model_id: str, shared: bool) -> dict:
        """共享开关（管理员 / 普通用户同一套规则）：
        只能对自己创建的模型开关共享；管理员另可管理自己创建的公共模型
        （owner="admin" 的历史约定）。不能改别人的模型 —— 管理员也不再
        拥有对他人私人模型的运维特权，与 visible_to() 的收窄保持一致。
        """
        uid = (user or {}).get("id", "")
        role = (user or {}).get("role")
        if role in (None, "", "guest") or not uid:
            raise AIError("FORBIDDEN", "请先登录", status=403)
        # 必须在同一份列表上修改并保存：_models() 每次都重新读盘，
        # 改动临时 dict 后再调 _models() 会把修改丢掉。
        models = _models()
        m = next((x for x in models if x.get("id") == model_id), None)
        if not m:
            raise AIError("BAD_REQUEST", "模型不存在")
        owner = m.get("owner")
        if owner != uid and not (role == "admin" and owner == "admin"):
            raise AIError("FORBIDDEN", "仅模型创建者可设置共享", status=403)
        m["shared"] = bool(shared)
        _save_models(models)
        return {"ok": True, "shared": bool(shared)}

    def _store_key(self, user, model_id, api_key, *, personal_uid=None, shared=False):
        secrets = load_secrets()
        key = (api_key or "").strip()
        if shared:
            if key:
                secrets["shared"][model_id] = key
            else:
                secrets["shared"].pop(model_id, None)
        else:
            uid = personal_uid or (user or {}).get("id", "")
            secrets.setdefault("personal", {}).setdefault(uid, {})
            if key:
                secrets["personal"][uid][model_id] = key
            else:
                secrets["personal"][uid].pop(model_id, None)
        save_secrets(secrets)

    def set_default(self, user: dict, model_id: str) -> dict:
        models = _models()
        m = next((x for x in models if x.get("id") == model_id), None)
        if not m:
            raise AIError("BAD_REQUEST", "模型不存在")
        uid = (user or {}).get("id", "")
        role = (user or {}).get("role")
        if m.get("owner") != uid and role != "admin":
            raise AIError("FORBIDDEN", "无权操作该模型", status=403)
        # 只重置「同一归属 + 同能力」的模型，避免误伤其他用户/管理员的模型
        caps = set(m.get("kind") or [])
        for x in models:
            if x.get("owner") == m.get("owner") and caps.intersection(x.get("kind") or []):
                x["is_default"] = (x.get("id") == model_id)
        _save_models(models)
        return {"ok": True, "is_default": True}

    def cache_voices(self, model_id: str, voices: list) -> None:
        models = _models()
        m = next((x for x in models if x.get("id") == model_id), None)
        if m:
            m["voices_cache"] = voices
            _save_models(models)

    # ---- 生成 ----
    def _record(self, user, feature, provider, model, ok, code, latency_ms, extra):
        record_usage({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "user_id": (user or {}).get("id", ""),
            "role": (user or {}).get("role", ""),
            "feature": feature, "provider": provider, "model": model,
            "ok": ok, "code": code, "latency_ms": latency_ms, **(extra or {}),
        })

    def generate_text(self, user, prompt, model_id, system=None, feature="text") -> dict:
        t0 = time.time()
        try:
            m, key, be = self.resolve(user, model_id, "text")
            res = be.generate_text(prompt, system, key)
            self._record(user, feature, m.get("provider"), m.get("model"), True, "",
                         int((time.time() - t0) * 1000), res.get("usage", {}))
            res.setdefault("data", {})["model_id"] = m["id"]
            res.setdefault("data", {})["model_name"] = m.get("name", "")
            return res
        except AIError as e:
            self._record(user, feature, "", model_id, False, e.code,
                         int((time.time() - t0) * 1000), {})
            return ai_result(False, error={"code": e.code, "message": e.message})
        except Exception as e:
            self._record(user, feature, "", model_id, False, "PROVIDER_ERROR",
                         int((time.time() - t0) * 1000), {})
            return ai_result(False, error={"code": "PROVIDER_ERROR",
                                           "message": "上游模型服务异常：" + str(e)[:200]})

    def generate_image(self, user, prompt, model_id, feature="image") -> dict:
        t0 = time.time()
        try:
            m, key, be = self.resolve(user, model_id, "image")
            data = be.generate_image(prompt, key)
            self._record(user, feature, m.get("provider"), m.get("model"), True, "",
                         int((time.time() - t0) * 1000), {"bytes": len(data)})
            b64 = base64.b64encode(data).decode("ascii")
            return ai_result(True, {"image_b64": b64, "content_type": "image/png",
                                    "model_id": m["id"], "model_name": m.get("name", "")})
        except AIError as e:
            self._record(user, feature, "", model_id, False, e.code,
                         int((time.time() - t0) * 1000), {})
            return ai_result(False, error={"code": e.code, "message": e.message})
        except Exception as e:
            self._record(user, feature, "", model_id, False, "PROVIDER_ERROR",
                         int((time.time() - t0) * 1000), {})
            return ai_result(False, error={"code": "PROVIDER_ERROR",
                                           "message": "上游模型服务异常：" + str(e)[:200]})

    def generate_tts(self, user, text, model_id, voice_id=None, feature="tts") -> Optional[bytes]:
        t0 = time.time()
        try:
            m, key, be = self.resolve(user, model_id, "tts")
            if isinstance(be, TTSMakerBackend):
                vid = voice_id or m.get("default_voice_id")
                data = be.generate_tts(text, key, vid)
            elif isinstance(be, DashScopeTTSBackend):
                voice = (voice_id or m.get("default_voice_id")
                         or (m.get("extra") or {}).get("voice") or "")
                data = be.generate_tts(text, key, voice)
            else:
                voice = (m.get("extra") or {}).get("voice") or "alloy"
                data = be.generate_tts(text, key, voice)
            self._record(user, feature, m.get("provider"), m.get("model"), True, "",
                         int((time.time() - t0) * 1000), {"bytes": len(data)})
            return data
        except AIError as e:
            self._record(user, feature, "", model_id, False, e.code,
                         int((time.time() - t0) * 1000), {})
            raise
        except Exception as e:
            self._record(user, feature, "", model_id, False, "PROVIDER_ERROR",
                         int((time.time() - t0) * 1000), {})
            raise AIError("PROVIDER_ERROR", "上游配音服务异常：" + str(e)[:200])

    def tts_voices(self, user, model_id) -> dict:
        try:
            m, key, be = self.resolve(user, model_id, "tts")
            if not isinstance(be, (TTSMakerBackend, DashScopeTTSBackend)):
                return ai_result(False, error={"code": "BAD_REQUEST",
                                              "message": "该配音模型不支持角色列表（仅 TTSMaker / 百炼 Qwen-Audio-TTS 支持）"})
            voices = be.get_voices(key)
            self.cache_voices(model_id, voices)
            if not voices:
                return ai_result(True, {"voices": [], "message":
                                        "该模型的音色清单未内置，请直接填写默认音色 ID（如 longanhuan_v3.1）"})
            return ai_result(True, {"voices": voices})
        except AIError as e:
            return ai_result(False, error={"code": e.code, "message": e.message})
        except Exception as e:
            return ai_result(False, error={"code": "PROVIDER_ERROR",
                                           "message": "获取角色列表失败：" + str(e)[:200]})

    def selfcheck_model(self, user, model_id) -> dict:
        t0 = time.time()
        try:
            m = _find_model(model_id)
            if not m:
                return ai_result(False, error={"code": "BAD_REQUEST", "message": "模型不存在"})
            cap = "text" if "text" in (m.get("kind") or []) else (m["kind"][0] if m.get("kind") else "text")
            mm, key, be = self.resolve(user, model_id, cap)
            res = be.selfcheck(key)
            res.setdefault("data", {}).update({
                "model_id": mm["id"], "model_name": mm.get("name", ""),
                "provider": mm.get("provider"), "capability": cap,
            })
            return res
        except AIError as e:
            return ai_result(False,
                             data={"model_id": model_id},
                             error={"code": e.code, "message": e.message})


ai_service = AIService()


def fallback_model_id(user: dict, model_id: str, capability: str) -> tuple:
    """下拉「— 默认/首个可用 —」= 空值时，自动回落到首个已配密钥的可用模型。

    返回 (model_id, error_result)；无可用模型时返回结构化 NO_CREDENTIAL，绝不静默挑无密钥模型。
    """
    if model_id:
        return model_id, None
    usable = ai_service.list_usable(user, capability)
    if not usable:
        return "", ai_result(False, error={
            "code": "NO_CREDENTIAL",
            "message": "尚无可用模型：请在「AI 大模型」中添加模型并填写密钥（只加载已配密钥的模型）"})
    default = next((m for m in usable if m.get("is_default")), None)
    return (default or usable[0])["id"], None


# ------------------------------ 路由 ------------------------------

def _user(request: Request) -> dict:
    return current_user(request)


def _forbid_guest(user) -> Optional[dict]:
    if (user or {}).get("role") in (None, "", "guest"):
        return {"ok": False, "error": {"code": "FORBIDDEN", "message": "请登录后使用 AI 功能"}}
    return None


@router.get("/api/ai/models")
async def ai_models_list(request: Request, capability: str = "", usable: bool = True):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    models = ai_service.list_usable(user, capability or None) if usable else ai_service.list_for_user(user, capability or None)
    return ai_result(True, {"models": models})


@router.get("/api/ai/manage")
async def ai_manage(request: Request):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    return ai_result(True, ai_service.manage_view(user))


@router.post("/api/ai/models")
async def ai_model_add(request: Request, body: dict = None):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    b = body or {}
    try:
        return ai_service.add_personal(user, b, b.get("api_key", ""))
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})


@router.delete("/api/ai/models/{model_id}")
async def ai_model_del(request: Request, model_id: str):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    try:
        return ai_service.delete_model(user, model_id)
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})


@router.post("/api/ai/models/{model_id}/shared")
async def ai_model_shared(request: Request, model_id: str):
    """通用共享开关：创建者（含普通用户）可共享 / 取消共享自己的模型。

    刻意放在 /api/ai 前缀下 —— /api/admin/ 会被中间件的
    ADMIN_ONLY_PREFIXES 拦成「仅管理员」，普通用户走不到。
    """
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    try:
        data = await request.json()
    except Exception:
        data = {}
    shared = bool((data or {}).get("shared", True))
    try:
        return ai_service.set_shared(user, model_id, shared)
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})


@router.post("/api/ai/models/{model_id}/voices")
async def ai_model_voices(request: Request, model_id: str):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    return ai_service.tts_voices(user, model_id)


@router.post("/api/admin/models")
async def ai_admin_model_add(request: Request, body: dict = None):
    user = _user(request)
    if (user or {}).get("role") != "admin":
        return fail("FORBIDDEN", "仅管理员可添加共享模型")
    b = body or {}
    try:
        return ai_service.add_admin(user, b, b.get("api_key", ""), bool(b.get("shared")))
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})


@router.delete("/api/admin/models/{model_id}")
async def ai_admin_model_del(request: Request, model_id: str):
    user = _user(request)
    if (user or {}).get("role") != "admin":
        return fail("FORBIDDEN", "仅管理员可管理模型")
    try:
        return ai_service.delete_model(user, model_id)
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})


@router.post("/api/admin/models/{model_id}/shared")
async def ai_admin_model_shared(request: Request, model_id: str):
    user = _user(request)
    if (user or {}).get("role") != "admin":
        return fail("FORBIDDEN", "仅管理员可设置共享")
    try:
        data = await request.json()
    except Exception:
        data = {}
    shared = bool((data or {}).get("shared", True))
    try:
        return ai_service.set_shared(user, model_id, shared)
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})


@router.get("/api/admin/ai")
async def admin_ai_get(request: Request):
    user = _user(request)
    if (user or {}).get("role") != "admin":
        return fail("FORBIDDEN", "仅管理员可查看")
    return ai_result(True, {"features": ai_cfg().get("features", {}),
                            "timeout": int(ai_cfg().get("timeout", 120) or 120)})


@router.post("/api/admin/ai")
async def admin_ai_set(request: Request, body: dict = None):
    user = _user(request)
    if (user or {}).get("role") != "admin":
        return fail("FORBIDDEN", "仅管理员可配置")
    features = (body or {}).get("features")
    if isinstance(features, dict):
        patch = {"ai": {"features": features}}
        save_config("", patch)
    timeout = (body or {}).get("timeout")
    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            timeout = None
        if timeout and 15 <= timeout <= 600:
            save_config("", {"ai": {"timeout": timeout}})
    return ai_result(True, {"features": ai_cfg().get("features", {}),
                            "timeout": int(ai_cfg().get("timeout", 120) or 120)})


@router.post("/api/ai/text")
async def ai_text(request: Request, body: dict = None):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    b = body or {}
    prompt = (b.get("prompt") or "").strip()
    if not prompt:
        return ai_result(False, error={"code": "BAD_REQUEST", "message": "prompt 不能为空"})
    model_id, err = fallback_model_id(user, b.get("model_id") or "", "text")
    if err:
        return err
    system = b.get("system") or None
    feature = b.get("feature") or "image_text"
    return ai_service.generate_text(user, prompt, model_id, system=system, feature=feature)


@router.post("/api/ai/image")
async def ai_image(request: Request, body: dict = None):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    b = body or {}
    prompt = (b.get("prompt") or "").strip()
    if not prompt:
        return ai_result(False, error={"code": "BAD_REQUEST", "message": "prompt 不能为空"})
    model_id, err = fallback_model_id(user, b.get("model_id") or "", "image")
    if err:
        return err
    return ai_service.generate_image(user, prompt, model_id, feature="image")


@router.post("/api/ai/tts")
async def ai_tts(request: Request, body: dict = None):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    b = body or {}
    text = (b.get("text") or "").strip()
    if not text:
        return ai_result(False, error={"code": "BAD_REQUEST", "message": "text 不能为空"})
    model_id, err = fallback_model_id(user, b.get("model_id") or "", "tts")
    if err:
        return err
    voice_id = b.get("voice_id")
    try:
        data = ai_service.generate_tts(user, text, model_id, voice_id=voice_id, feature="tts")
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})
    return Response(content=data, media_type="audio/mpeg",
                    headers={"Content-Disposition": "inline; filename=ai_voice.mp3"})


@router.post("/api/ai/tts/voices-preview")
async def ai_tts_voices_preview(request: Request, body: dict = None):
    """保存前预览配音角色列表：仅凭 base_url + token/model，不落库。

    TTSMaker 需真实拉取；百炼 Qwen-Audio-TTS 是静态系统音色，按 model 名直接返回。
    """
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    b = body or {}
    provider = (b.get("provider") or "").strip()
    if provider == "dashscope":
        model = (b.get("model") or "").strip()
        if not model:
            return ai_result(False, error={"code": "BAD_REQUEST",
                                           "message": "请先填写 model，例如 qwen-audio-3.1-tts-plus"})
        return ai_result(True, {"voices": dashscope_tts_voices(model),
                                "static": True,
                                "message": "百炼系统音色为官方固定清单，已按模型返回"})
    if provider != "ttsmaker":
        return ai_result(False, error={"code": "BAD_REQUEST", "message": "该服务商不需要拉取角色列表"})
    token = (b.get("api_key") or "").strip()
    if not token:
        return ai_result(False, error={"code": "NO_CREDENTIAL", "message": "请先填写 TTSMaker token"})
    base = (b.get("base_url") or PROVIDERS["ttsmaker"]["base_url"]).strip()
    try:
        be = TTSMakerBackend(base)
        voices = be.get_voices(token)
        return ai_result(True, {"voices": voices})
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})
    except Exception as e:
        return ai_result(False, error={"code": "PROVIDER_ERROR",
                                       "message": "获取角色列表失败：" + str(e)[:200]})


@router.post("/api/ai/tts/voice-audio")
async def ai_tts_voice_audio(request: Request, body: dict = None):
    """表单未保存时的角色试听：用当前填写的 provider/base_url/model/密钥即时合成一句短demo。

    已保存的模型请走 /api/ai/tts（带 model_id），权限与密钥都由服务端解析。
    """
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    b = body or {}
    provider = (b.get("provider") or "dashscope").strip()
    model = (b.get("model") or "").strip()
    voice = (b.get("voice") or "").strip()
    key = (b.get("api_key") or "").strip()
    text = (b.get("text") or "你好，这是配音角色的试听效果。").strip()[:80]
    if not key:
        return ai_result(False, error={"code": "NO_CREDENTIAL", "message": "请先填写 API Key"})
    try:
        if provider == "dashscope":
            be = DashScopeTTSBackend(b.get("base_url") or "", model,
                                     int(ai_cfg().get("timeout", 60) or 60))
            data = be.generate_tts(text, key, voice)
        elif provider == "ttsmaker":
            be = TTSMakerBackend(b.get("base_url") or PROVIDERS["ttsmaker"]["base_url"])
            data = be.generate_tts(text, key, int(voice) if str(voice).isdigit() else 0)
        else:
            be = OpenAIBackend(b.get("base_url") or "", model)
            data = be.generate_tts(text, key, voice or "alloy")
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})
    except Exception as e:
        return ai_result(False, error={"code": "PROVIDER_ERROR",
                                       "message": "试听合成失败：" + str(e)[:200]})
    return Response(content=data, media_type="audio/mpeg",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": "inline; filename=voice_preview.mp3"})


@router.post("/api/ai/models/{model_id}/default")
async def ai_model_default(request: Request, model_id: str):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    try:
        return ai_service.set_default(user, model_id)
    except AIError as e:
        return ai_result(False, error={"code": e.code, "message": e.message})


@router.post("/api/ai/selfcheck")
async def ai_selfcheck(request: Request, body: dict = None):
    user = _user(request)
    if (fb := _forbid_guest(user)):
        return fb
    model_id, err = fallback_model_id(user, (body or {}).get("model_id") or "", None)
    if err:
        return err
    return ai_service.selfcheck_model(user, model_id)
