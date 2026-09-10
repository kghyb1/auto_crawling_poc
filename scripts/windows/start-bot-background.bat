@echo off
chcp 65001 >nul
rem 봇을 백그라운드로 실행합니다 (창을 닫아도 계속 동작)
call "%~dp0_run.bat" start --detach
pause
