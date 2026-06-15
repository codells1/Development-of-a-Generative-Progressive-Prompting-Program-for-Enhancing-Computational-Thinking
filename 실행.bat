@echo off
chcp 65001 >nul
title CT 프롬프팅 프로그램
cd /d "%~dp0"

echo ============================================================
echo  컴퓨팅 사고력(CT) 프롬프팅 프로그램 실행
echo ============================================================
echo.
echo  [필수] LM Studio가 켜져 있고, 로컬 서버(포트 1234)가
echo         실행 중이어야 합니다. (모델: qwen/qwen3.5-9b)
echo.

REM --- flask가 설치된 Python 3.13을 우선 사용 (WindowsApps 스텁 회피) ---
set "PYEXE="
py -3.13 -c "import flask" >nul 2>&1 && set "PYEXE=py -3.13"
if not defined PYEXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
        set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    )
)
if not defined PYEXE set "PYEXE=python"

echo  사용 Python: %PYEXE%
echo  접속 주소  : http://127.0.0.1:5000
echo ============================================================
echo.

REM --- 잠시 후 브라우저 자동 열기 ---
start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:5000"

%PYEXE% app.py

echo.
echo ============================================================
echo  서버가 종료되었습니다. 창을 닫으려면 아무 키나 누르세요.
echo ============================================================
pause >nul
