@echo off
REM JML Python 侧测试运行入口（阶段 1）
REM
REM 用法（在仓库根目录执行）:
REM   run_python_tests.bat               跑全部 Python/tests/
REM   run_python_tests.bat -k manifest  只跑名字含 manifest 的用例
REM   run_python_tests.bat --tb=short   自定义传给 pytest 的其它参数
REM
REM 说明：本机全局 Python 环境无写入权限（pip install 会 PermissionError），
REM 因此仓库内自带一份 pytest 运行环境（_pytest_env/，纯 Python 解包即用），
REM 本脚本把它加入 PYTHONPATH，不改动系统 Python，也不需要联网安装。

setlocal

set "REPO_ROOT=D:\vscode\Project\JML"
set "PYTEST_ENV=%REPO_ROOT%\_pytest_env"
set "PY_SRC=%REPO_ROOT%\Python"

if not exist "%PYTEST_ENV%\pytest" (
    echo [ERROR] pytest env not found: %PYTEST_ENV%
    exit /b 1
)

set "PYTHONPATH=%PYTEST_ENV%;%PY_SRC%;%PYTHONPATH%"

python -m pytest "%PY_SRC%\tests" -v -p no:cacheprovider %*

endlocal & exit /b %ERRORLEVEL%
