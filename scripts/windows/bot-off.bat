@echo off
chcp 65001 >nul
rem 수집 OFF (프로세스는 그대로 두고 수집만 정지)
call "%~dp0_run.bat" off
pause
