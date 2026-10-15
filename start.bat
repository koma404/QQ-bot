@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ---- 可选：填 NapCat 启动脚本的完整路径，这里就会一起拉起（留空则需自己先启动 NapCat）----
set "NAPCAT_BAT="

if not exist config.yaml (
  echo [错误] 没有 config.yaml，请先复制 config.example.yaml 并修改。
  pause & exit /b 1
)

rem ---- 虚拟环境：优先用较稳定的 Python 版本（太新的版本常常装不上依赖）----
if not exist .venv\Scripts\python.exe (
  set "PYCMD="
  for %%V in (3.13 3.12 3.11 3.10) do if not defined PYCMD py -%%V -c "" >nul 2>nul && set "PYCMD=py -%%V"
  if not defined PYCMD set "PYCMD=python"
  echo 首次运行：创建虚拟环境，使用 %PYCMD% ...
  %PYCMD% -m venv .venv || (
    echo [错误] 创建虚拟环境失败，请确认已安装 Python 3.10-3.13 并勾选 Add to PATH
    rmdir /s /q .venv >nul 2>nul
    pause & exit /b 1
  )
)

rem ---- 依赖：缺什么补什么 ----
.venv\Scripts\python -c "import websockets, httpx, yaml" >nul 2>nul || (
  echo 安装依赖...
  .venv\Scripts\python -m pip install -q -r requirements.txt || (
    echo [错误] 依赖安装失败。若上面提到需要 Microsoft C++，说明 Python 版本太新：
    echo        请安装 Python 3.13，删除 .venv 文件夹后重新运行本脚本。
    pause & exit /b 1
  )
)

if defined NAPCAT_BAT start "NapCat" "%NAPCAT_BAT%"

.venv\Scripts\python bot.py
pause
