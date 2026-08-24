@echo off
REM VERIFIED - one-shot Windows launcher
where python >nul 2>nul || (echo Python 3.10+ is required. Install from https://www.python.org/downloads/ and tick "Add to PATH". & exit /b 1)
if not exist .venv (python -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
python -m verified.cli models
python -m verified.cli doctor
echo.
echo Starting the UI at http://127.0.0.1:8000  (Ctrl+C to stop)
python -m verified.cli serve
