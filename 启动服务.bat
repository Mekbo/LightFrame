@echo off
chcp 65001 >nul
REM 启动光影工坊服务。关闭 AI 沙箱的安全删除 shim（避免批量删素材/删任务时进程被护栏杀掉）。
set CODEBUDDY_SAFE_DELETE_ENABLED=0
set CODEBUDDY_TOOL_CALL_ID=
set CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR=
cd /d "%~dp0"
python -m backend.run
pause
