@echo off
chcp 65001 >nul
rem 봇을 실행합니다 (창을 닫으면 종료됩니다)
call "%~dp0_run.bat" start
pause
