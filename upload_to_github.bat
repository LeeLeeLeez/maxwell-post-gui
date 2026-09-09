@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=
title 上传 Maxwell 后处理 GUI → GitHub

echo ============================================================
echo   Maxwell 绕组后处理 GUI  ——  上传到 GitHub
echo ============================================================
echo.
echo   没有 token？ https://github.com/settings/tokens
echo   Generate new token (classic)  →  勾 repo  →  复制粘贴到下面
echo.
set /p GH_TOKEN=粘贴 GitHub Token (ghp_ 开头) :
if "%GH_TOKEN%"=="" (
    echo.
    echo   [X] 没输 token，退出。
    pause
    exit /b 1
)
echo.
set /p GH_MSG=提交说明（直接回车用默认）:
if "%GH_MSG%"=="" set GH_MSG=更新：Maxwell 绕组后处理 GUI
echo.
echo   仓库名默认 maxwell-post-gui；想改名就改本文件里的 REPO
echo.

set REPO=maxwell-post-gui

where python >nul 2>nul && (
    python gh_upload.py --token "%GH_TOKEN%" --repo %REPO% --public -m "%GH_MSG%"
    goto :end
)
where py >nul 2>nul && (
    py -3 gh_upload.py --token "%GH_TOKEN%" --repo %REPO% --public -m "%GH_MSG%"
    goto :end
)
echo   [X] 未找到 Python，请先安装 Python 3.9+ 并勾选 Add to PATH

:end
echo.
pause
