@echo off
chcp 65001 >nul
rem 지금 한 바퀴만 수집합니다
call "%~dp0_run.bat" run-once
pause
