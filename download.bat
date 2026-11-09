@echo off
rem 用户入口：结束时保留窗口，便于查看成功结果或启动错误。
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if not exist "~temp\logs" mkdir "~temp\logs"
echo [准备] 正在检查运行环境...
if not exist ".venv-browser\Scripts\python.exe" (
    py -3 -m venv .venv-browser >> "~temp\logs\bat-setup.log" 2>&1
    if errorlevel 1 goto :fail
)
".venv-browser\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check --quiet >> "~temp\logs\bat-setup.log" 2>&1
if errorlevel 1 goto :fail
echo [准备] 运行环境就绪。
set "PYTHONPATH=%~dp0src"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
echo [启动] tele-harbor · Telegram 群组下载
".venv-browser\Scripts\python.exe" -m telegg %*
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
    echo [结束] 正常结束。
) else if "%RESULT%"=="130" (
    echo [结束] 用户中断，重跑可从已保存进度继续。
) else if "%RESULT%"=="2" (
    echo [结束] 参数有误，未启动下载；可运行 download.bat --help。
) else if "%RESULT%"=="1" (
    echo [结束] 任务未完成，请查看上方原因和日志；未完成文件与断点保留。
) else (
    echo [结束] 异常退出，请保留退出码和日志用于排查。
)
goto :finish
:fail
echo 运行环境准备失败，详情见 ~temp\logs\bat-setup.log。
set "RESULT=1"
:finish
echo 程序退出码: %RESULT%
echo 按任意键关闭窗口...
pause >nul
exit /b %RESULT%
