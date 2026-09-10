@echo off
chcp 65001 >nul
rem 엑셀을 다시 만듭니다
call "%~dp0_run.bat" export
pause
