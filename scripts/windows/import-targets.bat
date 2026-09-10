@echo off
chcp 65001 >nul
rem config\targets.yaml 의 홍보사이트 목록을 DB 에 반영합니다
call "%~dp0_run.bat" import-targets
pause
