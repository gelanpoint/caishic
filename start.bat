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

rem ===================================================================
rem  演示数据目录放到**用户数据目录**（通常在 C: 这类快盘），而不是仓库内 data\。
rem  依据（实测，不是猜）：同一段代码、同一负载，「点按口径」的接口响应时间在 C: 上
rem    p50 43ms / p95 73ms，在 D:（仓库所在卷）上 p50 297~354ms / p95 683~770ms；
rem    根因是**磁盘 fsync 成本**（256KB 写 + fsync 的 p50：C: 2ms vs D: 37ms，仓库内外一样），
rem    与接口实现无关。详见 docs\standards\quality-gates.md §1.1 的
rem    「响应时间阈值必须连口径一起读」注。
rem
rem  两条纪律：
rem    1) **不覆盖**用户已显式设置的 MT_DATA_DIR —— 现场要换盘/换目录时直接设它即可；
rem    2) LOCALAPPDATA 不存在时**什么也不设**，于是行为与改动前**完全一致**（仓库内 data\）。
rem  `python run.py` 的默认值**没有改变**（仍是仓库内 data\）：改默认值需要有
rem  "不设置时不变量"的证据，本脚本只影响"双击启动"这一条演示路径。
rem
rem  写法注意：`set` 与 `echo %MT_DATA_DIR%` **不能放在同一个括号块里** ——
rem  cmd 在解析整个括号块时就展开了 `%VAR%`，那时变量还是旧值（第一版就是这么写的，
rem  实测打印出来是空路径）。故拆成两行、且都放在块外。
rem ===================================================================
if not defined MT_DATA_DIR if defined LOCALAPPDATA set "MT_DATA_DIR=%LOCALAPPDATA%\MarketTradeDemo\data"
if defined MT_DATA_DIR echo [演示] 数据目录：%MT_DATA_DIR%（如需改回仓库内 data\，先执行：set "MT_DATA_DIR="）

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
