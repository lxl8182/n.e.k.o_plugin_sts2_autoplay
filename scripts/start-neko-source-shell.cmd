@echo off
chcp 936 >nul
setlocal EnableExtensions

REM ============================================================================
REM  NekoSpire 一键启动：先起「源码版后端」，再开「Steam 桌宠壳」
REM  壳启动后会自己扫到已在跑的源码后端并 attach（复用），不另起宿主进程。
REM
REM  用法一（推荐，Steam 启动项一键）：
REM    Steam → 库 → N.E.K.O → 右键「属性」→「通用」→ 启动选项，填：
REM      cmd /c "F:\game\projectNEKO\plugin\nekospire\scripts\start-neko-source-shell.cmd" %command%
REM    之后点 Steam 里的「开始游戏」即等于：先起源码后端 → 再开桌宠壳。
REM
REM  用法二：直接双击本脚本（等价于用法一，壳路径用下面 NEKO_SHELL 默认值）。
REM
REM  可覆盖的环境变量（不设就用默认值）：
REM    NEKO_SRC            源码目录（含 launcher.py 与 .venv）
REM    NEKO_SHELL          Steam 桌宠壳 N.E.K.O.exe 路径
REM    NEKO_HEALTH         后端健康检查 URL（默认 48911 main 服务）
REM    NEKO_WAIT_SECONDS   等后端就绪的最长秒数（默认 180）
REM    NEKO_COMPANION_PROBE 同伴会话路由探测地址（默认 48911 /api/sts2-companion/poll）
REM    NEKO_COMPANION_ROUTE 路由标识，仅用于日志/提示（默认 /api/sts2-companion/poll）
REM
REM  加固说明（2026-10-19）：
REM    1. 源码后端未就绪时直接报错退出，不再启动壳。否则壳会自己拉起 Steam
REM       内置后端（projectneko_server.exe），它没有 /api/sts2-companion/* 路由，
REM       表现为「同伴会话连接异常」且很难从表面定位。
REM    2. 后端已在运行时先核对同伴会话路由；缺路由说明当前是 Steam 内置后端
REM       或未打宿主补丁，直接报错退出，不再开壳。
REM    3. 等待期间监控 backend-bootstrap.log，启动器一旦判定启动失败立即退出，
REM       不再空等到超时。
REM    5. 路由探测改用 HTTP 状态码（GET 返回 404 才视为缺失），不再依赖 openapi.json；
REM       启动瞬间 openapi 首次生成可能超过探针超时，拿到空响应会误报缺路由导致不开壳。
REM    4. 启动/失败结果追加到 logs\start-shell.log，Steam 启动时控制台一闪而过也能查。
REM ============================================================================

if not defined NEKO_SRC             set "NEKO_SRC=F:\game\projectNEKO\N.E.K.O"
if not defined NEKO_SHELL           set "NEKO_SHELL=F:\steam\steamapps\common\N.E.K.O\N.E.K.O.exe"
if not defined NEKO_HEALTH          set "NEKO_HEALTH=http://127.0.0.1:48911/health"
if not defined NEKO_WAIT_SECONDS    set "NEKO_WAIT_SECONDS=180"
if not defined NEKO_COMPANION_PROBE set "NEKO_COMPANION_PROBE=http://127.0.0.1:48911/api/sts2-companion/poll"
if not defined NEKO_COMPANION_ROUTE set "NEKO_COMPANION_ROUTE=/api/sts2-companion/poll"

set "PY=%NEKO_SRC%\.venv\Scripts\python.exe"
set "LOGDIR=%NEKO_SRC%\logs"
set "LOG=%LOGDIR%\backend-bootstrap.log"
set "START_LOG=%LOGDIR%\start-shell.log"

echo [neko] source = %NEKO_SRC%
echo [neko] shell  = %NEKO_SHELL%
call :log "start src=%NEKO_SRC% shell=%NEKO_SHELL%"

if not exist "%PY%" (
  echo [neko] ERROR: 找不到源码 venv python: %PY%
  echo [neko]        请先在该目录执行 uv sync 装好依赖。
  echo [neko]        已停止，不启动桌宠壳（避免回落到 Steam 内置后端）。
  call :log "ERROR missing venv python: %PY%"
  goto :fail
)
if not exist "%NEKO_SRC%\launcher.py" (
  echo [neko] ERROR: 找不到 %NEKO_SRC%\launcher.py
  echo [neko]        已停止，不启动桌宠壳（避免回落到 Steam 内置后端）。
  call :log "ERROR missing launcher.py: %NEKO_SRC%\launcher.py"
  goto :fail
)

call :health
if not errorlevel 1 (
  call :companion_route
  if not errorlevel 1 (
    echo [neko] 后端已在运行且包含同伴会话路由，直接开壳。
    call :log "existing backend ok; launching shell"
    goto :launch_shell
  )
  echo [neko] ERROR: 48911 上已有后端，但缺少 %NEKO_COMPANION_ROUTE% 路由。
  echo [neko]        这通常是 Steam 内置后端（projectneko_server.exe），它不包含宿主补丁。
  echo [neko]        请先完全退出 N.E.K.O（含后台 projectneko_server.exe），再重新运行本脚本。
  call :log "ERROR existing backend missing route %NEKO_COMPANION_ROUTE%; refuse to launch shell"
  goto :fail
)

echo [neko] 后端未运行，启动源码后端 ...
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
start "neko-backend" /min cmd /c ""%PY%" -u "%NEKO_SRC%\launcher.py" > "%LOG%" 2>&1"

call :wait_health
if errorlevel 1 (
  echo [neko] ERROR: %NEKO_WAIT_SECONDS%s 内源码后端未就绪，已停止，不启动桌宠壳。
  echo [neko]        常见原因：Main Server 导入超过启动器 90 秒上限（Startup timeout）。
  echo [neko]        详见: %LOG%
  call :log "ERROR backend not ready within %NEKO_WAIT_SECONDS%s; refuse to launch shell"
  goto :fail
)

call :companion_route
if errorlevel 1 (
  echo [neko] ERROR: 后端已就绪但缺少 %NEKO_COMPANION_ROUTE% 路由，已停止，不启动桌宠壳。
  echo [neko]        请确认运行的是 %NEKO_SRC% 源码后端，且包含 sts2_companion_router。
  call :log "ERROR backend missing route %NEKO_COMPANION_ROUTE%; refuse to launch shell"
  goto :fail
)
echo [neko] 后端已就绪（含同伴会话路由）。
call :log "backend ready; launching shell"

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

:fail
if "%~1"=="" (
  echo [neko] 按任意键关闭...
  pause >nul
)
endlocal
exit /b 1

REM ---------------------------------------------------------------------------
:log
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
>>"%START_LOG%" echo [%date% %time%] %~1
exit /b 0

:health
curl -s -m 2 "%NEKO_HEALTH%" >nul 2>&1
exit /b %errorlevel%

:companion_route
set "CODE="
for /f %%i in ('curl -s -o NUL -w "%%{http_code}" -m 4 "%NEKO_COMPANION_PROBE%" 2^>nul') do set "CODE=%%i"
if not defined CODE exit /b 1
if "%CODE%"=="000" exit /b 1
if "%CODE%"=="404" exit /b 1
exit /b 0

:wait_health
setlocal EnableDelayedExpansion
set /a "_t=0"
:wait_loop
call :health
if not errorlevel 1 ( endlocal & exit /b 0 )
call :backend_failed
if not errorlevel 1 (
  endlocal & exit /b 1
)
set /a "_t+=2"
if !_t! GEQ %NEKO_WAIT_SECONDS% ( endlocal & exit /b 1 )
timeout /t 2 /nobreak >nul 2>&1
goto :wait_loop

:backend_failed
if not exist "%LOG%" exit /b 1
findstr /C:"Startup timeout" /C:"Startup aborted" /C:"启动失败" /C:"启动超时" "%LOG%" >nul 2>&1
exit /b %errorlevel%
