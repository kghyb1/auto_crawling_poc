@echo off
chcp 65001 >nul
rem 봇 프로세스를 정상 종료합니다
call "%~dp0_run.bat" stop
pause
