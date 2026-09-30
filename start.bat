@echo off
rem ===================================================================
rem  start.bat —— Windows 启动脚本（双击即用）
rem  行尾必须是 CRLF（.gitattributes 强制 *.bat eol=crlf）：
rem    cmd.exe 对 LF 行尾的 .bat 在 goto/:label、多行 if 括号块等场景会解析失败。
rem  依赖：Python 3.11+ 已安装（不做免安装打包，见 ADR-0003）
rem  不依赖外网、不使用 Docker（ADR-0003）
rem ===================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem 让 Python 的 stdout/stderr 一律使用 UTF-8：
rem   控制台显示与"输出被重定向到文件"两种情况都不会出现中文乱码
rem   （输出被重定向时 Python 默认用系统 locale 编码，中文会变成乱码）
set "PYTHONUTF8=1"

set "PY_CMD="
where python >nul 2>nul
if not errorlevel 1 set "PY_CMD=python"
if not defined PY_CMD (
    where py >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3"
)

if not defined PY_CMD (
    echo ====================================================================
    echo  [启动失败] 未找到 Python。
    echo  处理办法：安装 Python 3.11 或更高版本，并在安装时勾选
    echo            "Add python.exe to PATH"，然后重新双击本脚本。
    echo ====================================================================
    pause
    exit /b 1
)

%PY_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo ====================================================================
    echo  [启动失败] Python 版本过低，本项目需要 3.11 或更高版本。
    %PY_CMD% -c "import sys; print('  当前版本: ' + sys.version)"
    echo ====================================================================
    pause
    exit /b 1
)

echo 正在启动服务（首次启动会自动建库并导入种子数据，请稍候）...
%PY_CMD% run.py %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo ====================================================================
    echo  [启动失败] 退出码 %EXIT_CODE%。常见原因与处理办法见上方提示。
    echo  端口被占用时：改端口再启动，例如  start.bat --port 8010
    echo ====================================================================
    pause
)

endlocal & exit /b %EXIT_CODE%
