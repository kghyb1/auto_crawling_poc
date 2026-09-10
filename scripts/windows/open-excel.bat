@echo off
chcp 65001 >nul
rem 최신 엑셀 결과를 엽니다
cd /d "%~dp0..\.."
start "" "data\exports\불법사이트_URL목록.xlsx"
