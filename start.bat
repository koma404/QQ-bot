@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ---- 可选：填 NapCat 启动脚本的完整路径，这里就会一起拉起（留空则需自己先启动 NapCat）----
set "NAPCAT_BAT="

if not exist config.yaml (
  echo [错误] 没有 config.yaml，请先复制 config.example.yaml 并修改。
  pause & exit /b 1
)
where python >nul 2>nul || (echo [错误] 没找到 Python，请安装并勾选 "Add to PATH" & pause & exit /b 1)

if not exist .venv (
  echo 首次运行：创建虚拟环境并安装依赖...
  python -m venv .venv || (pause & exit /b 1)
  .venv\Scripts\python -m pip install -q -r requirements.txt || (pause & exit /b 1)
)

if defined NAPCAT_BAT start "NapCat" "%NAPCAT_BAT%"

.venv\Scripts\python bot.py
pause
