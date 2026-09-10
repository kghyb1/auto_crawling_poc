@echo off
chcp 65001 >nul
rem 처음 한 번만 실행하는 설치 스크립트 (가상환경 + 라이브러리 + 설정 파일)
cd /d "%~dp0..\.."

echo [1/4] 파이썬 확인
where python >nul 2>nul
if errorlevel 1 (
    echo   파이썬을 찾을 수 없습니다. https://www.python.org/downloads/ 에서 3.10 이상을 설치하고
    echo   설치할 때 "Add python.exe to PATH" 를 체크하세요.
    pause
    exit /b 1
)
python --version

echo [2/4] 가상환경 만들기 (.venv)
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo   가상환경 생성에 실패했습니다.
        pause
        exit /b 1
    )
) else (
    echo   이미 있습니다. 건너뜁니다.
)

echo [3/4] 라이브러리 설치
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo   라이브러리 설치에 실패했습니다.
    pause
    exit /b 1
)

echo [4/4] 설정 파일 준비
".venv\Scripts\python.exe" bot.py init

echo.
echo 설치가 끝났습니다. 다음 순서로 진행하세요.
echo   1) config\targets.yaml 에 수집할 홍보사이트 주소를 넣고 enabled: true 로 바꾸기
echo   2) scripts\windows\import-targets.bat 실행
echo   3) scripts\windows\start-bot.bat 실행
echo.
pause
