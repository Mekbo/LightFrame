# 光影工坊｜多用户权限与 AI 网关架构设计 v1.0

> 阶段 0 定稿文档 · 2026-11-12
> 状态：**已确认并按第 9 节默认值执行**
> 适用范围：本地单机 + 局域网服务器部署版本
> 实施进度：**阶段 1 / 2 / 4 已完成**（2026-11-12 夜间）；阶段 3（AI 双通道）/ 5（局域网配置）/ 6（打包）待做

---

## 0. 决策摘要

| 项 | 结论 |
|---|---|
| 部署边界 | 本版本只支持**本地单机 + 局域网 HTTP**（`0.0.0.0:2026`），公网仅预留配置位与接口，不实现 |
| 最终形态 | 功能冻结后封装为「任意电脑可安装」的本地安装包（内嵌 Python + ffmpeg），无需公网 |
| 账户模型 | 三角色：管理员 / 普通用户 / 游客；**id 与 username 分离**；服务端会话 + HttpOnly Cookie |
| 认证原则 | **彻底不再信任任何来自请求参数的 uid**；所有归属从服务端会话解析 |
| AI 网关 | `AIBackend` 抽象 + `BuiltinBackend`（应用独立配置，默认）+ `MCPBackend`（接口占位）；密钥只落后端 |
| AI 去水 | **不做**（现有 cover 模式够用） |
| 架构约束 | 单进程单实例、任务/产物落本地盘、并发闸门（`DynamicSemaphore`）与混剪逻辑均不改 |
| 实施顺序 | 0 文档 → 1 认证底座 → 2 权限与归属 → 3 AI 双通道 → 4 前端 → 5 局域网 → 6 打包 |

---

## 1. 现状盘点与风险清单（本轮只读核对结果）

### 1.1 身份链路现状

| 位置 | 现状 |
|---|---|
| `static/index.html:808-809` | `userUid = localStorage.getItem('gy_uid')`，无则前端 `genUid()` 生成 |
| `static/index.html:1795-1798` | 保存设置时 `newUid = sanitizeUid(用户名)`，**改用户名即更换 uid**，历史配置与产物目录随之失联 |
| `static/index.html:1235 / 1679 / 1841` | 三类任务提交均以 `FormData.append('uid', userUid)` 传身份 |
| `static/index.html:1700 / 1801 / 1817` | 配置读写以 `/api/config?uid=...` 传身份 |
| `backend/core/config.py:145-185` | `sanitize_uid()` 只做字符白名单，`config/users/{uid}.json` 按该字符串命名，无任何归属校验 |
| `backend/main.py:41-53` | `out_base(kind, uid)` 按 uid 读取个人配置决定输出目录 |

**结论**：uid 是「前端自定义字符串」，不是身份。任何客户端可伪造。

### 1.2 高危接口现状

| 接口 | 现状 | 风险 |
|---|---|---|
| `GET /api/config?uid=` | 返回全量配置（含 `ffmpeg_path`、输出绝对路径） | 伪造 uid 可读他人配置 |
| `POST /api/config?uid=` | 直接写 `config/users/{uid}.json` | 可覆写他人/全局配置 |
| `GET /api/tasks` | `list(mgr.tasks.values())` **返回全部任务** | 跨用户可见全部任务与产物路径 |
| `POST /api/tasks/delete-all` | 用 `username in Path(out_dir).parts` 判归属后 `shutil.rmtree` | **伪造 uid 可删除他人产物**；路径包含判断可误伤 |
| `POST /api/tasks/clear` | `ids=None` 时清空全部任务 | 任意人可清全局 |
| `POST /api/tasks/delete` / `DELETE /api/tasks/{tid}` | 按 tid 删任务并 `rmtree` 产物，无归属校验 | 跨用户删除 |
| `POST /api/tasks/zip` | 按 ids 打包产物，无归属校验 | 跨用户读取文件 |
| `GET /api/output/{name}` | 未带 tid 时遍历所有任务目录找同名文件返回 | **跨用户读文件**；`name` 未做 basename 白名单 |
| `POST /api/tasks/{tid}/cancel\|pause\|resume\|open-folder` | 仅按 tid 操作 | 跨用户控制他人任务 |
| `GET /api/fonts?refresh=1` | 触发目录重扫（写操作）无鉴权 | 低 |
| `GET/POST/DELETE /api/text-templates`、`/api/video-templates` | 全局共享模板，无鉴权 | 游客可增删模板 |
| `POST /api/tasks/image\|video\|live` | 任务入口无鉴权 | 游客可跑满并发 |
| `GET /api/concurrency` | 全局并发状态无鉴权 | 信息泄露（低） |

### 1.3 数据与打包现状

- `backend/services/taskmgr.py:110-116`：`register()` 的任务结构**无 `owner_id`**；`config/tasks.json` 为全局单文件，终态任务跨重启保留。
- 产物目录实际形态：`文档/LightFrame/OutPut_{Image,Video}/{uid}/{tid}/`（`main.py:439/501/809`），属软隔离。
- `backend/core/ffmpeg.py:6-26`：ffmpeg 检测链 = 配置路径 → `tools/ffmpeg/` → imageio-ffmpeg → PATH，**打包时把 ffmpeg.exe 放进 `tools/ffmpeg/` 即零配置**。
- `backend/core/config.py:12-16`：输出根用 `Path.home()/Documents/LightFrame`，**天然适配任意电脑/账号**。
- 前端为单页静态 HTML（`static/index.html`），无构建步骤；`app.mount("/static", ...)` 直接托管。
- **缺失**：`.gitignore`、`requirements.txt`、启动入口脚本（`run.py` / `start.bat`）；`backend/api/` 为空模块，可作新代码落点。
- 存在遗留目录 `frontend/`、`output/`，打包前需确认是否排除。

### 1.4 风险定级（决定阶段 1/2 不能省）

局域网 ≠ 可信网络：同网段任何设备打开 `http://192.168.1.10:2026` 即为访客，按现状可读全量配置、全部任务、任意产物并删文件。因此「游客只读 + 后端强校验 + 密钥不出后端」三条**必须实现**，不能因「只内网用」而跳过。

---

## 2. 部署边界

### 2.1 本版本必须实现

- 监听 `0.0.0.0:2026`（现状已满足），本机 `127.0.0.1:2026` 与局域网 `http://<内网IP>:2026` 均可访问。
- 服务端会话鉴权、三角色权限、资源按 `owner_id` 隔离。
- 单进程单实例；任务状态、产物、配置、密钥均落本地盘。
- 同源访问策略：CORS 保持同源，**禁止 `allow_origins=["*"]`**。
- 防火墙只放行 2026 端口给内网段。

### 2.2 公网只预留（写配置位，不写实现）

```
server.bind_host          = "0.0.0.0"     # 可收窄为 127.0.0.1
server.https_enabled      = false         # 预留，本版本不启用
server.cookie_secure      = false         # 由 https_enabled 联动，本版本 false
server.allowed_origins    = []            # 预留，本版本留空=同源
server.lan_allow          = []            # 预留网段白名单，空=不限制
server.trust_proxy_headers= false         # 预留反向代理
```

- 用户表预留 `tenant_id`；任务表预留 `owner_id`（本版本即使用）。
- `MCPBackend` 接口签名与 `AI_BACKEND` 环境变量位。
- 保持现有上传类型/大小白名单与路径白名单，公网化时可直接复用。
- **不把 `request.client.host` 硬编码进任何权限判断**（避免反代后误判）。
- 顶层 `port` 保持兼容，新增 `server` 组用 `deep_merge` 合并，不破坏现有 `config.json`。

### 2.3 明确不做

TLS 终止 / nginx、用户自助注册、密码找回、验证码、限流与风控、对象存储、多实例与 Redis/Celery、AI 高级去水印。

---

## 3. 账户模型

### 3.1 三角色定义

| 角色 | 来源 | 说明 |
|---|---|---|
| `admin` | 首次运行初始化生成 | 全控；管理用户、全局设置、共享密钥池、全部任务与文件 |
| `user` | 管理员在后台创建 | 登录后可用全部加工功能；管自己的账户/任务/文件；AI 默认自带密钥 |
| `guest` | 未登录（含未创建会话） | 只能浏览功能界面，不能使用任何加工与 AI 能力，不能浏览全局设置 |

> 不做注册/找回/验证码：账号由管理员在后台创建分发，符合局域网小团队场景。

### 3.2 id 与 username 分离（修正现有 bug）

- `id`：系统生成、不可变，形如 `u_<12位随机>`；用于会话、任务归属、目录名、个人配置文件名、密钥索引。
- `username`：可改，仅用于显示与登录；允许中文，唯一性由管理员侧校验。
- **改账户名不再改变 id**，历史配置、任务、产物目录保持归属不变。
- 前端 localStorage 的 `gy_uid` 在阶段 1 上线后**废弃并清除**，不再作为身份来源。

### 3.3 数据结构

`config/accounts.json`（新增，权限 600 / 不进版本库）
```json
{
  "version": 1,
  "users": [
    {
      "id": "u_a1b2c3d4e5f6",
      "username": "admin",
      "password": "pbkdf2_sha256$200000$<salt_b64>$<hash_b64>",
      "role": "admin",
      "status": "active",
      "tenant_id": "",
      "must_change_password": true,
      "created_at": "2026-11-12T22:00:00",
      "last_login_at": "",
      "last_login_ip": ""
    }
  ]
}
```

`config/sessions.json`（新增，重启不踢人）
```json
{ "<session_token>": {"user_id": "u_...", "expires_at": "<iso>", "created_at": "<iso>"} }
```

- 密码：`hashlib.pbkdf2_hmac("sha256", pw, salt, 200_000)`，校验用 `hmac.compare_digest`；**不引第三方依赖**，便于打包。
- 会话 token：`secrets.token_urlsafe(32)`；Cookie 名 `gy_session`，`HttpOnly=True`、`SameSite=Lax`、`Path=/`、`Secure=server.cookie_secure`；默认有效期 7 天，退出即删除。
- `config/users/{id}.json`：**保留**为个人配置（沿用 `load_config(uid)` / `save_config(uid, patch)`），主键由可伪造 uid 换成服务端下发的真实 id。
- `config/secrets.json`（新增）：所有 AI / 配音 / TTS 密钥集中存放，**任何 GET 接口不得返回明文**。

### 3.4 首次运行流程

1. 检测 `accounts.json` 不存在 → 生成管理员 `admin` + 一次性随机初始密码，写入并在启动日志/首次访问页显示一次，标记 `must_change_password=true`。
2. 创建 `文档/LightFrame/OutPut_Image|Video/`、`config/`、`backend/fonts/` 等目录。
3. 检测 2026 端口占用并给出明确提示（含占用进程 PID 建议）。
4. 管理员首次登录强制改密 → 进入后台创建普通用户。
5. 升级/重装时 `accounts.json`、`sessions.json`、`secrets.json`、`config/users/` 全部保留。

### 3.5 认证接口

| 接口 | 说明 |
|---|---|
| `POST /api/auth/login` | `{username, password}` → 成功下发 Cookie，返回 `{id, username, role, must_change_password}` |
| `POST /api/auth/logout` | 删除服务端会话 + 清 Cookie |
| `GET /api/auth/me` | 返回当前用户（未登录返回 `role:"guest"` 而非 401，便于前端渲染） |
| `POST /api/auth/password` | 改自己密码（需旧密码；管理员可重置任意用户密码） |
| `POST /api/auth/username` | 改自己账户名（仅改 username，id 不变） |
| `GET/POST/DELETE /api/admin/users` | 管理员：列出 / 创建（含初始密码）/ 停用 / 删除用户 |
| `GET /api/public/config` | **新增**：游客可访问的 UI 最小配置（应用名、版本、功能可见性、字体列表），不含路径与密钥 |

> 依赖注入 `current_user(request)` 统一从 Cookie 解析；`require(role, action)` 统一做 POLICY 判定。**所有业务接口禁止再声明 `uid` 参数**（保留兼容但忽略，或直接 400）。

---

## 4. 权限矩阵

记号：`✓` 允许｜`—` 禁止｜`△` 仅自己/受限｜`A` 仅管理员

| # | 能力 / 动作 | 游客 | 普通用户 | 管理员 | 后端校验点 |
|---|---|:---:|:---:|:---:|---|
| 1 | 浏览功能界面、看说明/演示 | ✓ | ✓ | ✓ | 静态页放行 |
| 2 | 浏览全局设置页 | **—** | △（只读个人项） | ✓ | `GET /api/config` 游客 403 |
| 3 | 修改全局设置（并发/ffmpeg 路径/输出目录） | — | — | ✓ | `POST /api/config` |
| 4 | 保存自己的加工参数 | — | ✓ | ✓ | `POST /api/config` 按 id 落 `users/{id}.json` |
| 5 | 图片格式转换 / 裁切 / 缩放 / 去水印 | — | ✓ | ✓ | `POST /api/tasks/image` |
| 6 | **图片加文字** | **—** | ✓ | ✓ | 同上 + `image.text.enabled` 校验 |
| 7 | **Live 图转换** | **—** | ✓ | ✓ | `POST /api/tasks/live` |
| 8 | **视频加工（含混剪/字幕/配音）** | **—** | ✓ | ✓ | `POST /api/tasks/video` |
| 9 | 提交任务 / 上传素材 | — | ✓ | ✓ | 三类任务入口 |
| 10 | 查看任务列表 | — | △（仅 `owner_id==me`） | ✓（全部） | `GET /api/tasks` 过滤 |
| 11 | 取消 / 暂停 / 继续任务 | — | △ | ✓ | `/api/tasks/{tid}/*` 归属校验 |
| 12 | 查看 / 下载自己的产物 | — | △ | ✓ | `GET /api/output/{name}` 归属 + basename 白名单 |
| 13 | 打开产物目录（open-folder） | — | △ | ✓ | 同上 |
| 14 | 打包下载产物（zip） | — | △ | ✓ | `/api/tasks/zip` 逐 id 校验 |
| 15 | 清除自己的任务记录 | — | △ | ✓ | `/api/tasks/clear` 强制带 ids 且逐条归属校验；`ids=None` 仅管理员 |
| 16 | 删除自己的任务与产物文件 | — | △ | ✓ | `/api/tasks/delete`、`DELETE /api/tasks/{tid}` |
| 17 | 一键清除自己全部文件 | — | △ | ✓ | `/api/tasks/delete-all`（不再接收 uid） |
| 18 | 删除他人任务/文件 | — | **—** | ✓ | 同上 |
| 19 | 改自己账户名 / 密码 | — | ✓ | ✓ | `/api/auth/username`、`/api/auth/password` |
| 20 | 用户管理、重置他人密码 | — | — | ✓ | `/api/admin/users` |
| 21 | 字体列表（只读） | ✓ | ✓ | ✓ | `GET /api/fonts` |
| 22 | 刷新字体目录（触发重扫） | — | — | ✓ | `GET /api/fonts?refresh=1` |
| 23 | 模板列表（只读） | ✓ | ✓ | ✓ | `GET /api/{text,video}-templates` |
| 24 | 模板预览 / 新建 / 删除 | — | ✓（可建个人模板） | ✓ | 模板写入类接口 |
| 25 | 查看并发状态 | — | ✓ | ✓ | `GET /api/concurrency` |
| 26 | **使用任何 AI 能力** | **—** | ✓（密钥按策略） | ✓ | `/api/ai/*` 统一 |
| 27 | 配置共享密钥池 / 允许共享 | — | — | ✓ | `POST /api/admin/ai` |
| 28 | 填写自己的 AI 密钥 | — | ✓ | ✓ | `POST /api/ai/keys`（只写不回明文） |
| 29 | 查看他人密钥明文 | — | **—** | **—** | 任何接口均不返回明文，仅 `has_key` + 掩码 |

### 4.1 强制实现规则

1. **后端强校验是唯一边界**；前端隐藏/禁用只作体验，不得作为安全依据。
2. 越权判定一律用 **`owner_id` 精确相等**（`task["owner_id"] == current.id`）；**禁止**再用 `uid in Path(...).parts` 这类字符串包含判断。
3. 删除类操作前校验 `out_dir` 必须位于允许根（`output_dir_for()` / 个人配置输出根）之下，且路径规范化后无 `..` 逃逸；删除仍需前端二次确认。
4. `/api/output/{name}` 的 `name` 做 `Path(name).name == name` 白名单校验，禁止 `..` 与路径分隔符。
5. 管理员跨用户操作记入 `config/audit.log`（时间、操作者、动作、目标 id）——为公网阶段预留审计字段，本版本只写不展示。
6. 未登录请求默认落到 `guest`；除白名单接口外一律 403，错误体 `{ok:false, error:{code:"FORBIDDEN"}}`。

### 4.2 权限表代码形态

`backend/core/policy.py` 集中定义 `POLICY: dict[action, set[role]]`，业务接口只写 `require("task.video.create")`，避免权限判断散落在 30 多个路由里。新增 `backend/api/` 作为认证/AI/管理接口落点。

---

## 5. AI 双通道接口定义

### 5.1 抽象签名（阶段 3 落地，阶段 0 只定契约）

```python
class AIBackend(Protocol):
    def generate_text(self, req: TextRequest,  ctx: AIContext) -> AIResult: ...
    def generate_image(self, req: ImageRequest, ctx: AIContext) -> AIResult: ...
    def generate_tts(self, req: TTSRequest,   ctx: AIContext) -> AIResult: ...
    def selfcheck(self, ctx: AIContext) -> HealthResult: ...
```

统一返回：`{"ok": bool, "data": ..., "usage": {...}, "error": {"code", "message"}}`

| 错误码 | 含义 |
|---|---|
| `FORBIDDEN` | 角色无权（游客调用 AI） |
| `NO_CREDENTIAL` | 无可用密钥（自带为空且未开共享） |
| `NOT_IMPLEMENTED` | 选中 MCP 但未配置 endpoint |
| `PROVIDER_ERROR` / `TIMEOUT` / `QUOTA_EXCEEDED` | 上游错误 / 超时 / 额度 |

`AIContext`：`user_id / role / key_scope / timeout / trace_id / secrets`（**secrets 只在后端注入，永不下发前端**）。

### 5.2 两种实现

| 后端 | 说明 |
|---|---|
| `BuiltinBackend` | **默认**。读应用独立配置 `ai.provider / base_url / model` + `secrets.json`，直连厂商 HTTP API |
| `MCPBackend` | 接口占位。由 `AI_BACKEND=mcp` + `MCP_ENDPOINT` / `MCP_TOKEN` 驱动，方法签名与 Builtin 完全一致；未配置时 `selfcheck()` 返回 `NOT_IMPLEMENTED` 并给前端明确提示，**不静默失败** |

> 与用户已有 `model-gateway-mcp` 项目对齐：后续可直接把 `MCP_ENDPOINT` 指向该网关，无需改动业务接入点。

### 5.3 配置结构（新增 `ai` 组，写入 `config/config.json`）

```json
"ai": {
  "backend": "builtin",
  "provider": "",
  "base_url": "",
  "model": "",
  "key_scope": "personal",
  "allow_shared": false,
  "timeout": 60,
  "mcp": {"enabled": false, "endpoint": ""},
  "features": {
    "image_text": true, "video_script": true,
    "video_tts": true, "video_title": true, "video_cover": true
  }
}
```

密钥单独存 `config/secrets.json`：
```json
{
  "shared":  {"api_key": "..."},
  "personal": {"u_a1b2c3d4e5f6": {"api_key": "..."}}
}
```
- 所有 GET 只返回 `has_key: true` + 掩码（末 4 位），**永不返回明文**。
- 打包/版本控制时 `secrets.json`、`accounts.json`、`sessions.json` 必须进 `.gitignore`。

### 5.4 密钥归属与优先级

`key_scope`：`personal`（默认，普通用户自带） / `shared`（使用管理员共享池） / `none`（禁用 AI）

解析顺序：
1. 角色为 `guest` → 直接 `FORBIDDEN`。
2. `personal` 且本人有密钥 → 用本人密钥。
3. 本人无密钥 且 `ai.allow_shared=true` 且共享池有密钥 → 用共享池（管理员授权后可给登录用户用）。
4. 其余 → `NO_CREDENTIAL`，前端提示「请到账户设置填写自己的密钥，或联系管理员开启共享」，**不回落静默跳过**。

管理员可随时开关 `allow_shared`；普通用户**不能**查看共享池或他人密钥。

### 5.5 业务接入点映射

| # | 场景 | 调用 | 产物落点 |
|---|---|---|---|
| 1 | 图片加文字·文案生成 | `generate_text` | `image.text.content`（沿用现有 `_draw_text_layers`） |
| 2 | 视频脚本生成 | `generate_text` | `video.scripts[].subtitle/title` |
| 3 | 脚本配音 | `generate_tts` | 生成音频 → 按**音频真实时长**对齐字幕（复用现有 `_segment_and_time` 与 `align_mode=audio_duration`） |
| 4 | 视频标题 + 封面 | `generate_text` + `generate_image` | `video.scripts[].title`、`video.cover` |
| — | AI 高级去水印 | **不做** | 保留现有 `image.watermark.mode=cover` |

> `video.ai_script` 现有配置位（`config.py:79-82`）保留并扩展，不推翻；`auto_voice` 与 `align_mode` 语义不变。

### 5.6 自检与降级

- 全局设置「AI 大模型」页提供 **自检按钮** → `POST /api/ai/selfcheck`，返回连通性、模型、密钥来源（掩码）、耗时。
- AI 失败不得拖垮主流程：文案生成失败时保留原文并提示重试；TTS 失败时保留「无配音」路径；封面生成失败回落 `cover=none`。
- 所有 AI 调用记 `usage`（tokens / 张数 / 字符数、耗时、trace_id）到 `config/ai_usage.json`，**不记密钥、不记完整文案**。

---

## 6. 数据归属与路径规范

### 6.1 owner_id 注入

- `TaskManager.register(tid, kind, total, out_dir, owner_id)` 新增必填 `owner_id`。
- `config/tasks.json` 每条任务携带 `owner_id`；`load()` 恢复时兼容旧数据（无 owner_id 的判为 `legacy`）。
- `GET /api/tasks`：普通用户过滤 `owner_id == me`；管理员返回全部（含 legacy）；游客 403。
- `clear(ids=None)` 语义收紧：仅管理员可无 ids 清空；普通用户必须带 ids 且逐条校验归属。

### 6.2 目录规范

```
文档/LightFrame/
  OutPut_Image/{user_id}/{tid}/...
  OutPut_Video/{user_id}/{tid}/...
```
- 目录名统一用**不可变 id**，不再用 username / 前端 uid。
- `config/users/{user_id}.json` 为个人加工参数。
- 全局共享只读资源：`backend/fonts/`、`backend/templates/`、`backend/video_templates/`。

### 6.3 删除安全规则

1. 归属判定：`owner_id` 精确相等（管理员除外）。
2. 路径校验：`out_dir.resolve()` 必须以允许根开头，拒绝 `..` 与软链接逃逸。
3. `/api/tasks/delete-all` 不再接收任何 uid 参数，删除范围 = 当前用户 id 目录 + 其 `owner_id` 任务目录的并集。
4. 前端保留二次确认；管理员批量操作写 `audit.log`。

### 6.4 历史数据迁移（默认策略）

- 现有 `config/users/*.json`（demo/qa/u_* 等）与 `OutPut_*/{旧uid}/{tid}` 产物标记为 `legacy`。
- **默认不自动认领**：legacy 任务与产物仅管理员可见，普通用户从零开始，避免误归属。
- 提供管理员侧「认领历史配置」可选操作：把某个 `config/users/{旧uid}.json` 复制到指定账户的 `{id}.json`。
- 阶段 1 上线后前端清除 `localStorage.gy_uid`，避免旧身份继续流转。

---

## 7. 接口改造清单

| 接口 | 现状 → 目标 | 归属校验 | 阶段 |
|---|---|---|---|
| `GET/POST /api/config` | 去掉 `uid` 参数 → 按会话 id；游客 403；新增 `/api/public/config` | 会话 | 1,2 |
| `POST /api/auth/*`、`/api/admin/users` | 新增 | 会话 + POLICY | 1 |
| `GET /api/tasks` | 全量 → 按 owner 过滤 | `owner_id` | 2 |
| 三类任务提交 | 忽略传入 uid → 从会话取 id 并写入 `owner_id` | 会话 | 2 |
| `/api/tasks/{tid}/*` | 增加归属校验 | `owner_id` | 2 |
| `/api/tasks/clear\|delete\|zip`、`DELETE /api/tasks/{tid}` | 逐 id 归属校验；`ids=None` 仅管理员 | `owner_id` | 2 |
| `/api/tasks/delete-all` | 去掉 uid，按会话 id 精确删除 | `owner_id` + 路径白名单 | 2 |
| `GET /api/output/{name}` | 归属校验 + basename 白名单 | `owner_id` | 2 |
| `GET /api/fonts?refresh=1` | 刷新仅管理员 | POLICY | 2 |
| 模板写入类接口 | 登录可写，游客只读列表 | POLICY | 2 |
| `/api/ai/*` | 新增：generate / selfcheck / keys | 会话 + `key_scope` | 3 |
| `/api/admin/ai` | 新增：共享池开关与密钥写入 | 仅管理员 | 3 |

---

## 8. 前端改造清单

1. 新增登录页 / 顶部当前用户态 / 退出入口；`GET /api/auth/me` 驱动渲染。
2. 游客：隐藏「全局设置」入口、图片加文字 / Live 转换 / 视频加工的提交按钮与 AI 入口；功能说明与演示保持可见。
3. 全局设置新增「AI 大模型」页：provider / base_url / model / 密钥来源 / 自检按钮 / 功能开关。
4. 普通用户「账户设置」：改账户名、改密码、清自己任务、删自己文件。
5. 管理员「用户管理」页 + 「共享密钥池」开关。
6. **移除所有 `uid` 传参**（`1235 / 1679 / 1700 / 1801 / 1817 / 1841` 行附近），改走 Cookie；清除 `gy_uid`。
7. 失败/无权限统一 toast：`FORBIDDEN` → 「请登录后使用」；`NO_CREDENTIAL` → 「请填写自己的 AI 密钥」。
8. 首次登录 `must_change_password` 时强制跳转改密页。

---

## 9. 待确认项（默认值已给出，未回复即按默认执行）

| # | 问题 | 默认值 |
|---|---|---|
| 1 | 管理员初始账号与密码生成方式 | 账号 `admin`，首次启动生成一次性随机密码，仅在启动日志与首次访问页显示一次，登录后强制改密 |
| 2 | 普通用户能否看到他人任务/产物 | **不能**，仅见自己的；管理员见全部 |
| 3 | 管理员能否查看用户自填密钥明文 | **不能**，任何接口只返回掩码；管理员只能覆盖写入 |
| 4 | 游客态任务列表呈现 | 隐藏任务区，改为「登录后可查看任务与产物」提示 |
| 5 | 历史 `anon/旧uid` 产物归属 | 不自动认领，标记 legacy，仅管理员可见；提供手动认领配置 |
| 6 | 字体与模板是否全局共享 | 共享只读；刷新字体目录、新建/删除模板需登录，删除仅管理员 |
| 7 | AI 首个接入的 provider | 阶段 3 先做 `BuiltinBackend` 通用 OpenAI 兼容协议（base_url + api_key + model），用户可直接填已有网关地址；`MCPBackend` 只留接口 |
| 8 | 是否记录操作日志 | 只写 `config/audit.log`（管理员跨用户操作 + AI 调用），不做前端展示 |
| 9 | 会话有效期 | 7 天，退出即失效，重启不丢 |

---

## 10. 验收标准

**阶段 1**
- [ ] 伪造任意 `?uid=` 无法读写他人配置；未登录 `GET /api/config` 返回 403。
- [ ] 改账户名后 id 不变，历史个人配置与产物目录保持可访问。
- [ ] 重启服务后会话仍有效；退出后 Cookie 失效。

**阶段 2**
- [ ] 游客无法提交任何任务、无法访问全局设置、无法读 `/api/output`。
- [ ] A 用户 `GET /api/tasks` 不含 B 的任务；删除/打包/取消他人任务均 403。
- [ ] `/api/tasks/delete-all` 不影响他人目录；含 `..` 的路径被拒绝。

**阶段 3**
- [ ] 游客调用 `/api/ai/*` 返回 `FORBIDDEN`。
- [ ] 普通用户无密钥且未开共享 → `NO_CREDENTIAL` 并给明确提示；管理员开共享后可用。
- [ ] 所有响应体不含密钥明文；`MCP` 未配置时自检返回 `NOT_IMPLEMENTED`。
- [ ] 4 个接入点各自跑通一次生成 → 产物正确写入对应配置。

**阶段 4**
- [ ] 三角色逐项点完第 4 节矩阵，前端可见性与后端权限一致。

**阶段 5**
- [ ] 本机 `127.0.0.1:2026` 与另一台机 `http://<内网IP>:2026` 均可访问；两账号并发跑任务互不干扰。
- [ ] `server.*` 5 个配置项存在且默认安全；CORS 未开放通配。

**阶段 6**
- [ ] 一台未装 Python、未装 ffmpeg 的干净电脑：安装 → 启动 → 图片/视频各跑一条 → 局域网另一台访问成功。
- [ ] 卸载重装后 `config/`、`文档/LightFrame/` 产物与账户全部保留。

---

## 11. 后续阶段衔接

| 阶段 | 交付物 | 依赖 | 主要落点 |
|---|---|---|---|
| 1 认证底座 | `core/auth.py`、`core/policy.py` 骨架、`api/auth.py`、账户/会话文件 | 本文档 | 新增 |
| 2 权限与归属 | POLICY 全量接入、`owner_id`、路径白名单、接口过滤 | 1 | `main.py`、`taskmgr.py` |
| 3 AI 双通道 | `api/ai.py`、secrets、共享池、4 个接入点 | 1,2 | `api/`、`video.py`、`image.py` |
| 4 前端 | 登录/账户/用户管理/AI 设置页、移除 uid | 1,2 | `static/index.html` |
| 5 局域网 | `server.*` 配置、同源策略、双机验证 | 1–4 | `config.py`、启动脚本 |
| 6 打包 | `requirements.txt`、`run.py`/`start.bat`、内嵌运行时 + ffmpeg、安装器、`.gitignore` | 功能冻结 | 项目根 |

> 打包放最后：避免每改一次功能就重打一次包。安装器不删除用户数据目录，升级只替换程序文件。

---

## 12. 本阶段不做（阶段 0 时的约束，现已解除）

阶段 0 只产出本定稿文档，不改业务代码、不改混剪/字幕/运镜/字体逻辑、不启服务、不装依赖、不写前端。用户确认后已进入实施。

---

## 13. 实施记录（阶段 1 / 2 / 4，2026-11-12 夜间）

### 13.1 新增 / 改动文件

| 文件 | 内容 |
|---|---|
| `backend/core/auth.py` | 账户表 `config/accounts.json`、会话表 `config/sessions.json`、pbkdf2_hmac 密码哈希、Cookie 下发/清除、审计日志 `config/audit.log`、id 与 username 分离 |
| `backend/core/policy.py` | 24 条 ROLE×ACTION 矩阵、`evaluate()` 默认拒绝鉴权、`owns()` 精确归属判断 |
| `backend/api/auth.py` | 初始化/登录/登出/me/改密/改名/用户管理/公开配置共 11 个接口，统一错误体 |
| `backend/main.py` | HTTP 中间件集中鉴权、`cu()/visible_tasks()/owned_task()/allowed_roots()/path_allowed()/basename_ok()/resolve_base()/save_personal_config()`；30+ 接口去除对前端 uid 的信任 |
| `backend/services/taskmgr.py` | `register(owner_id=...)`；旧任务恢复时补 `owner_id=""` 标为 legacy |
| `static/index.html` | 登录 / 首次运行初始化 / 账户设置 / 用户管理弹窗、游客提示条、角色可见性控制、移除全部 `uid` 传参与 `localStorage.gy_uid` |
| `backend/core/ffmpeg.py` | 修复旧版引用不存在 `config` 对象的潜在 ImportError（改为 `load_config()`） |

### 13.2 相对文档的实现差异（均为增强，不推翻设计）

1. **首次运行改为管理员自设密码**：`/api/auth/init` 由首次访问者在页面设置 ≥8 位密码并自动登录，替代「随机密码打印在启动日志」。GUI / 安装包场景更友好，也避免密码留在日志里。
2. **鉴权集中到 HTTP 中间件**：`policy.evaluate()` 默认拒绝 + 白名单，避免权限判断散落在 30 多个路由；函数内再做归属校验。
3. **普通用户个人配置只写差异键**：`save_personal_config()` 不把全局配置快照写进 `config/users/{id}.json`，否则管理员后续改并发/输出目录会被旧值覆盖。
4. **顺带修复**：`resume` 按任务 kind 选择恢复协程（旧版统一走图片循环，Live/视频上下文结构不同会出错）；游客读字体列表不再返回服务端绝对路径；`/api/public/config` 移除并发等全局字段。
5. **管理员写全局、普通用户写个人**：`POST /api/config` 对非管理员过滤 `GLOBAL_ONLY_KEYS`，全局项前端隐藏。

### 13.3 验收结果

- `py_compile` 全通过、内联 JS `node --check` 通过、`from backend.main import app` 导入 35 条路由。
- **TestClient 端到端 34 项全通过**：游客拦截（配置/任务/提交/字体刷新）、公开配置不含路径、首次初始化与重复初始化 409、错误密码 401、伪造 `?uid=` 读不到他人配置、A/B 任务列表隔离、管理员见全部含 legacy、B 删除/取消/打包/读取 A 的任务均 403、普通用户清空全部任务 403、路径遍历被拒、改名后 id 不变且旧密码仍可登录、会话可解析、真实提交任务写入 `owner_id` 且目录用不可变 id。
- HTTP 烟测：`/` 200 且含新 UI，`/api/auth/me` guest + `initialized=false`，游客 `/api/config`、`/api/tasks`、`/api/fonts?refresh=1` 均 403，公开配置与字体列表已脱敏。
- 测试数据已全部恢复/删除（`accounts.json`、`sessions.json`、`audit.log`、`tasks.json`、个人配置、测试产物目录）。
- 服务已在 `0.0.0.0:2026` 重启并加载新代码。**当前为全新未初始化状态**：首次打开页面会让使用者自行设置管理员密码。

### 13.4 后续待做

- 阶段 6：`.gitignore`、`requirements.txt`、`run.py`/`start.bat`（启动器 `backend/run.py` 已先行落地于阶段 5）、内嵌 Python + `tools/ffmpeg/ffmpeg.exe`、安装器与升级保留数据（**待用户通知后再执行**）。

### 13.5 阶段 3 实施记录（AI 双通道，2026-11-13）

**新增 / 改动文件**

| 文件 | 内容 |
|---|---|
| `backend/api/ai.py` | 阶段 3 全套：`AIBackend` 抽象 + `BuiltinBackend`（OpenAI 兼容 `/chat/completions`、`/audio/speech`、`/images/generations`）+ `MCPBackend`（接口占位，未配置 endpoint 返回 `NOT_IMPLEMENTED`）；`AIService` 编排；`config/secrets.json` 密钥管理（掩码返回、绝不回明文）；密钥优先级 personal→shared→`NO_CREDENTIAL`；游客 `FORBIDDEN`；用量只记错误码不记密钥/文案；8 个路由（text/image/tts/selfcheck/keys/status/admin） |
| `backend/services/video.py` | `generate_ai_scripts(video_cfg, user)` 接入：逐句脚本→标题→逐句 TTS→封面，按音频真实时长对齐字幕；`load_ai_features()` 读 `ai.features` 开关 |
| `backend/main.py` | 视频任务创建与 `_run_video_job` 透传 `user`（身份不可变 id） |
| `backend/core/config.py` | DEFAULTS 新增 `ai` 组（backend/provider/base_url/model/key_scope/allow_shared/timeout/features） |
| `static/index.html` | 菜单「🤖 AI 大模型」入口（游客隐藏）；`panel-ai`（我的密钥 / 全局配置 / 共享密钥池）；视频面板 AI 盒（启用/提示词/配音/封面/自检）；图片加文字「AI 生成文案」；账户页个人密钥输入；`loadAiStatus` / `saveMyKey` / `clearMyKey` / `aiSelfcheck` / `aiVideoSelfcheck` / `saveAdminAi` / `aiGenerateText` 全部接线；统一 `NO_CREDENTIAL→请填密钥` 提示 |

**验收结果（阶段 3）**

- `py_compile` 全通过、内联 JS `node --check` 通过、`from backend.main import app` 导入 36 条路由。
- 核心逻辑单测全通过：MCP 4 接口均 `NOT_IMPLEMENTED`；密钥掩码（`sk-1***********cdef`，无明文泄露）；`resolve_key` 游客 `FORBIDDEN`、无密钥 `NO_CREDENTIAL`；`key_status` 不返回 `api_key` 字段。
- HTTP 烟测：游客对 8 个 AI 接口全部 `403 FORBIDDEN`（含 `/keys/status`、`/keys`、`/admin/ai`）。
- 密钥写回单测：个人密钥保存 + 掩码返回；管理员共享池 + 全局配置保存（均不回传明文）；`secrets.json` 落盘正常；测试后已清理 `secrets.json` 与配置冗余段。

### 13.6 阶段 5 实施记录（局域网部署边界，2026-11-13）

**新增 / 改动文件**

| 文件 | 内容 |
|---|---|
| `backend/core/config.py` | DEFAULTS 新增 `server` 组：`bind_host`(0.0.0.0)、`https_enabled`、`cookie_secure`、`allowed_origins`(空=同源)、`lan_allow`(空=不限制)、`trust_proxy_headers` |
| `backend/core/auth.py` | `cookie_secure()` 联动 `https_enabled OR cookie_secure`（启用 HTTPS 时 Cookie 自动 Secure） |
| `backend/main.py` | `client_ip()` / `ip_allowed()` / `apply_cors()`；`security_guard` 中间件增加 `lan_allow` 白名单拦截（loopback 永远放行）；同源 CORS 反射（仅显式 origins，绝不回 `*`） |
| `backend/run.py` | **新增启动器**：读取 `server.bind_host` 拉起 uvicorn（替代硬编码 host），`trust_proxy_headers` 映射到 `forwarded_allow_ips` |

**验收结果（阶段 5）**

- 逻辑单测全通过：`ip_allowed` 覆盖空列表/loopback/单 IP/CIDR/多段；`client_ip` 直连与代理透传（`X-Forwarded-For`）；`cookie_secure` 随 `https_enabled` 联动。
- 启动器实测：`[光影工坊] 启动服务 -> http://0.0.0.0:2026 (trust_proxy_headers=False)`，监听 `0.0.0.0:2026`。
- **双机访问验证**：本机回环 `127.0.0.1:2026` → 200；局域网 IP `192.168.1.10:2026` → 200；局域网真实客户端 `192.168.1.30` 已实时连上 `/api/tasks`（200 OK），证明 `0.0.0.0` 绑定 + 双机并发可用。
- 同源策略验证：响应无 `Access-Control-Allow-Origin` 头（纯同源，未开放 `*`）。
- 测试期间临时写入的 `server.lan_allow` 等配置已清理还原，运行服务恢复正常（LAN 访问重新 200）。

### 13.7 AI 多模型注册表改造（2026-11-13）

> 需求：① 管理员与普通用户均可添加多种大模型（OpenAI / DeepSeek / 腾讯混元 / 智谱 GLM / 阿里百炼 / Kimi / MiniMax / 自定义本地模型）；② 管理员可任选一个或多个模型开启共享供用户使用；③ 支持配音模型，使用中可选角色并试听（TTSMaker 免费额度）；④ 各 AI 使用点由用户自行选择模型；⑤ **使用点只加载已配密钥的可用模型**。

**架构调整**：单 provider（`base_url` + `model` + 单把共享密钥）→ **模型注册表** `ai.models: [{id,name,provider,kind,base_url,model,owner,shared,enabled,is_default,default_voice_id,voices_cache,extra}]`。密钥按模型维度存于 `config/secrets.json`（`personal[uid][model_id]` / `shared[model_id]`），任何响应只返回 `has_key` 布尔，绝不回明文。

| 文件 | 内容 |
|---|---|
| `backend/core/config.py` | `ai` 默认组改为 `{timeout, features, models: []}`；`video.ai_script` 增 `model_text/model_tts/tts_voice_id/model_image` |
| `backend/api/ai.py` | 重写为多方网关：9 个 provider 预设（含 `ttsmaker` 专用 TTS 与 `custom` 本地模型）；`OpenAIBackend` + `TTSMakerBackend`（`get-voice-list` / `create-tts-order`，免费 `ttsmaker_demo_token` 5 万字符/周）；模型增删改、共享开关、设默认、角色列表与预览、自检；16 个路由 |
| `backend/services/video.py` | `generate_ai_scripts()` 按使用点分别取 `model_text/model_tts/model_image`，未指定时回落到该能力**已配密钥**的模型（优先默认），缺失即抛明确错误 |
| `static/index.html` | `panel-ai` 重构为「我的模型 / 可用共享模型（只读）/ 模型管理（管理员）/ 功能开关」；图片文案、视频脚本·配音·封面均加模型下拉；配音角色下拉 + 试听（中文优先分组）；管理员模型行「开启/取消共享」按钮 |

**关键实现与修复**

- **只加载有密钥模型**：`/api/ai/models` 默认 `usable=1` 走 `list_usable()`；`usable=0` 才返回全量（管理用）。视频回落同样只认 `list_usable()`。
- **共享独立开关**：每个模型自带 `shared` 字段，可同时共享任意多个，互不干扰。修复了 `set_shared` / `set_default` / `cache_voices` 的**持久化根因**——原先改动 `_models()` 临时 dict 后又重新读盘保存，导致修改被丢弃；现统一在同一份列表上修改并写回。
- **TTSMaker 字段实测校正**：官方 `get-voice-list` 返回 530 个角色（中文 71 个），样例音频字段为 `audio_sample_file_url`（非 `sample_mp3_url`）。后端 `normalize_voice()` 统一为 `id/name/language/sample_url`，中文优先排序，前端按「中文/方言」「其他语言（前 150）」分组并支持在线试听。
- **空 `model_id` 回落**：下拉「默认/首个可用」为空值时，后端自动选标记为默认的模型（否则首个可用），无可用模型返回 `NO_CREDENTIAL` 而非 `BAD_REQUEST`；上游网络异常统一转 `PROVIDER_ERROR`，不再 500。
- **默认模型生效**：`is_default` 真正参与回落选择，且 `set_default` 只重置「同归属 + 同能力」的模型，避免误伤他人模型。

**验收结果**

- 编译通过、内联 JS `node --check` 通过、`app` 导入 36 条路由、9 个 provider 就绪。
- TestClient 9 组集成测试全通过：游客 10 个接口全 `FORBIDDEN`；有/无密钥模型过滤；响应无明文与 `api_key` 字段；3 个共享模型同时可见且开关互不干扰（含关闭→落盘→不可见→重开恢复）；空 model_id 回落 + 上游异常结构化；TTSMaker 真实联网返回 530 角色且字段归一化、缓存落盘；设默认/功能开关持久化；删除模型连带清密钥；清空后 `NO_CREDENTIAL` 与视频管线明确报错。
- 测试数据全部还原：`config.json` 无冗余 `ai/server` 段、模型注册表 0 个、`secrets.json` 已删除（默认无密钥）、`ai_usage.json` / `sessions.json` 还原至测试前快照。

### 13.8 细节优化：接入地址 / 模型编辑 / 模板归属权限（2026-11-13 下午）

**① 阿里百炼接入地址**：官方文档明确旧域名 `dashscope.aliyuncs.com` 自 2026-09-30 起不再支持新特性，推荐使用**业务空间专属域名** `https://{WorkspaceId}.{region}.maas.aliyuncs.com/compatible-mode/v1`。因 `WorkspaceId` 因账号/业务空间而异、不可硬编码，改为：`PROVIDERS["dashscope"].base_url` 置空并标记 `require_base_url`，用户粘贴控制台复制的 API Host；选百炼时前端显示格式提示与专用 placeholder，未填则 `BAD_REQUEST` 明确报错。存量模型保存的旧地址不做强制迁移（官方仍兼容）。

**② 豆包**：新增 `doubao` provider（火山方舟 OpenAI 兼容协议），预设 `https://ark.cn-beijing.volces.com/api/v3`，能力 `text`，前后端下拉与 `/api/ai/manage` 的 `providers` 元数据同步。

**③ 模型编辑**：`_prepare_update()` 统一校验归属（跨归属编辑 `FORBIDDEN`）、编辑时 `api_key` 留空表示**保持原密钥**、新建必须填密钥、编辑保留 `voices_cache`；`manage` 增加 `providers` 下发作为前后端单一数据源。前端模型卡片新增「编辑」按钮，回填名称/厂商/能力/地址/model/共享/默认/角色，支持「取消编辑」，密钥框留空即不改。

**④ 模板归属权限**（原为全局共享、任何登录用户可覆盖他人模板）：

| 层 | 改动 |
|---|---|
| `backend/core/policy.py` | 新增 `template_visible / can_edit / is_mine / is_legacy / public`；`template.delete` 放开给登录用户，归属由路由层按记录强校验；存量无 `owner` 模板视为「系统预置」——所有人只读可用、仅管理员可改 |
| `backend/services/templates.py`、`video_templates.py` | `save_template(data, meta)` 由服务端注入 `owner / owner_name / shared / created_at`，剥离 payload 伪造的归属字段；编辑保留原 owner；视频模板 normalize 白名单会丢弃额外字段，故在保存时补齐 |
| `backend/main.py` | 文字/视频模板的列表、单条、新建、删除共 8 个路由全部接入 `cu(request)`：列表按可见性过滤、单条越权 `FORBIDDEN`、写删越权 `FORBIDDEN`，并用 `basename_ok()` 防路径注入 |
| `static/index.html` | 列表按 `can_edit` 决定是否渲染编辑/删除（只读显示「🔒 只读 · 归属」）；两个创建器新增「共享给其他用户使用（他人只读）」开关；`applyAuthUI` 不再一刀切隐藏普通用户删除按钮；保存/删除/编辑失败给出明确提示 |

**验收**：编译通过、36 路由、10 个 provider、JS `node --check` 通过、56 个事件函数与新增控件无缺失。TestClient 27 项集成测试全通过：豆包地址自动填充、百炼未填域名明确报错、编辑保留/轮换密钥、跨归属编辑拒绝、新建无密钥拒绝；文字与视频模板的私有隔离、共享只读、他人写删 `FORBIDDEN`、管理员最高权限且编辑不改归属、伪造 owner 无效、存量公共模板仅管理员可改、非法 ID 不崩溃、创建者可删自己的模板。测试快照（config / secrets / accounts / sessions / 两类模板目录）全部还原，用户测试期间添加的 3 个真实模型与密钥完好保留。服务已重启加载新代码（`0.0.0.0:2026`，本机与局域网均 200）。
