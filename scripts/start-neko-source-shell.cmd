@echo off
chcp 936 >nul
setlocal EnableExtensions

REM ============================================================================
REM  NekoSpire 一键启动：先起「源码版后端」，再开「Steam 桌宠壳」
REM  壳启动后会自己扫到已在跑的源码后端并 attach（复用），不另起宿主进程。
REM
REM  用法一（推荐，Steam 启动项一键）：
REM    Steam → 库 → N.E.K.O → 右键「属性」→「通用」→ 启动选项，填：
REM      cmd /c "F:\game\NEKO-src\start-neko-source-shell.cmd" %command%
REM    之后点 Steam 里的「开始游戏」即等于：先起源码后端 → 再开桌宠壳。
REM
REM  用法二：直接双击本脚本（等价于用法一，壳路径用下面 NEKO_SHELL 默认值）。
REM
REM  可覆盖的环境变量（不设就用默认值）：
REM    NEKO_SRC           源码目录（含 launcher.py 与 .venv）
REM    NEKO_SHELL         Steam 桌宠壳 N.E.K.O.exe 路径
REM    NEKO_HEALTH        后端健康检查 URL（默认 48911 main 服务）
REM    NEKO_WAIT_SECONDS  等后端就绪的最长秒数（默认 90）
REM ============================================================================

if not defined NEKO_SRC           set "NEKO_SRC=F:\game\NEKO-src"
if not defined NEKO_SHELL         set "NEKO_SHELL=F:\steam\steamapps\common\N.E.K.O\N.E.K.O.exe"
if not defined NEKO_HEALTH        set "NEKO_HEALTH=http://127.0.0.1:48911/health"
if not defined NEKO_WAIT_SECONDS  set "NEKO_WAIT_SECONDS=90"

set "PY=%NEKO_SRC%\.venv\Scripts\python.exe"
set "LOGDIR=%NEKO_SRC%\logs"
set "LOG=%LOGDIR%\backend-bootstrap.log"

echo [neko] source = %NEKO_SRC%
echo [neko] shell  = %NEKO_SHELL%

if not exist "%PY%" (
  echo [neko] ERROR: 找不到源码 venv python: %PY%
  echo [neko]        请先在该目录执行 uv sync 装好依赖。
  goto :launch_shell
)
if not exist "%NEKO_SRC%\launcher.py" (
  echo [neko] ERROR: 找不到 %NEKO_SRC%\launcher.py
  goto :launch_shell
)

call :health
if not errorlevel 1 (
  echo [neko] 后端已在运行，直接开壳。
  goto :launch_shell
)

echo [neko] 后端未运行，启动源码后端 ...
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
start "neko-backend" /min cmd /c ""%PY%" -u "%NEKO_SRC%\launcher.py" > "%LOG%" 2>&1"

call :wait_health
if errorlevel 1 (
  echo [neko] 警告：%NEKO_WAIT_SECONDS%s 内后端未就绪，仍尝试开壳（壳可能会自己补起宿主）。
) else (
  echo [neko] 后端已就绪。
)

:launch_shell
if "%~1"=="" (
  echo [neko] 启动桌宠壳（默认路径）...
  start "" "%NEKO_SHELL%"
) else (
  echo [neko] 启动桌宠壳（Steam 传入的命令行）...
  start "" %*
)
endlocal
exit /b 0

REM ---------------------------------------------------------------------------
:health
curl -s -m 2 "%NEKO_HEALTH%" >nul 2>&1
exit /b %errorlevel%

:wait_health
setlocal EnableDelayedExpansion
set /a "_t=0"
:wait_loop
call :health
if not errorlevel 1 ( endlocal & exit /b 0 )
set /a "_t+=2"
if !_t! GEQ %NEKO_WAIT_SECONDS% ( endlocal & exit /b 1 )
timeout /t 2 /nobreak >nul 2>&1
goto :wait_loop
