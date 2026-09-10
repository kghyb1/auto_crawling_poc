@echo off
chcp 65001 >nul
rem 수집 ON (프로세스는 그대로 두고 수집만 재개)
call "%~dp0_run.bat" on
pause
