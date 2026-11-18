# 光影工坊 LightFrame

你的图片与视频加工厂——图片加文字、Live 图提取、混剪视频、文案生产、AI 配音，一站式完成。简单易用，支持本机使用与局域网多人在线使用。

## 特性

- **图片加文字**：批量图片加文字水印/文案，多模板样式
- **Live 图提取**：iPhone Live Photo（LIVP）静态帧提取
- **视频创作**：图+视频混剪（保持各自格式不变）、配音、字幕、封面、BGM
- **文案生产**：脚本创作、配音、封面齐套打包，一键导入视频创作
- **AI 能力**：可接入自有 AI 密钥（DashScope / OpenAI 兼容接口），脚本生成、配音 TTS
- **多用户**：本地用户 + 在线用户（局域网），每用户独立空间隔离与配额，管理员统一管理
- **任务系统**：全局 FIFO 并发调度，任务卡片/列表双视图，排队提示、下载计数

## 安装（推荐）

1. 到 [v1.0.0 完整首发包](https://github.com/Mekbo/LightFrame/releases/tag/v1.0.0) 下载 **LightFrame_Setup_v1.0.exe**（约 100 MB，GitHub 附件为 ASCII 文件名）
2. 双击运行，按中文向导选择安装位置与选项（开机自启 / 局域网放行）
3. 安装完成即自动启动，浏览器打开 `http://127.0.0.1:2026`，首次访问自行设置管理员密码

> **全新安装只需下载上面这一个完整包。** v1.0.1 及之后的 Release 是**增量补丁包**，仅供已安装 v1.0.0 的用户升级使用，不能用于首次安装；升级方式见 [增量补丁包使用说明](docs/增量补丁包使用说明.md)。

**装在哪台机器，哪台就是服务端**：本机用 `127.0.0.1:2026`；同一局域网的其他设备（手机/平板/电脑）用安装完成页或「启动设置」控制台显示的局域网地址访问。需要外网访问时，在路由器把 2026 端口映射到该机器即可。

## 从源码运行（开发者）

要求：Python 3.10+，`pip install fastapi uvicorn pillow pillow-heif python-multipart httpx`，系统 `PATH` 中有 `ffmpeg`（或使用 `imageio-ffmpeg`）。

```bash
python -m backend.run
# 浏览器打开 http://127.0.0.1:2026
```

## 目录结构

```
backend/    FastAPI 后端（路由 / 服务 / 核心逻辑）
static/     单页前端 index.html
installer/  一键安装包构建（build.py）与实装验证（verify_install.py）
docs/       架构设计文档
config/     运行时配置（首次运行自动生成，不入库）
space/      用户素材空间（运行时生成，不入库）
data/       用户物料（运行时生成，不入库）
output/     加工产物与安装包（不入库）
```

## 构建安装包（维护者）

```bash
python installer/build.py            # 完整构建（运行时自检 + EXE 编译 + 打包）
python installer/build.py --exes-only  # 增量重建 EXE
python installer/verify_install.py   # 实装回归（安装→启动→卸载→零残留）
```

产物：`output/光影工坊_一键安装包_v1.0.exe`（约 100 MB，仅作为 GitHub Release 附件发布，**不提交进仓库**）。

## 版本维护约定

- 应用内「系统更新」每周匿名检查本仓库 Releases（回退读 main 分支 `VERSION` 文件）
- 发新版本步骤：
  1. 更新 `backend/services/app_update.py` 中的 `APP_VERSION` 与 `RELEASE_DATE`，在 `CHANGELOG` 列表头部插入更新说明
  2. 同步更新根目录 `VERSION` 文件
  3. `git tag vX.Y.Z` 并推送，创建 GitHub Release（说明贴 CHANGELOG），安装包 EXE 作为附件上传

## 说明

- 内置字体仅供软件界面排版使用，二次分发前请自行确认字体授权
- 所有密钥、账号数据仅存于本机 `config/` 目录，仓库与更新检查均不包含任何用户数据
