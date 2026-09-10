@echo off
rem 다른 배치 파일들이 공통으로 쓰는 실행기. 인자로 받은 명령을 봇에 전달합니다.
chcp 65001 >nul
cd /d "%~dp0..\.."

if exist ".venv\Scripts\python.exe" (
    set "BOT_PYTHON=.venv\Scripts\python.exe"
) else (
    set "BOT_PYTHON=python"
)

"%BOT_PYTHON%" bot.py %*
exit /b %errorlevel%
