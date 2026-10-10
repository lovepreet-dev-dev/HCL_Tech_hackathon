@echo off
rem One command to run the Operations Copilot on Windows: double-click start.bat, or run it in a terminal.
rem Creates the virtual environment, installs dependencies when requirements.txt
rem changes, creates .env on first run, then starts the server.
setlocal
cd /d "%~dp0"
if not defined PORT set PORT=8000

if not exist venv\Scripts\python.exe (
  echo Creating the virtual environment...
  py -3.12 -m venv venv 2>nul || py -3 -m venv venv 2>nul || python -m venv venv
)
if not exist venv\Scripts\python.exe (
  echo Python was not found. Install Python 3.12 from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH", then run start.bat again.
  pause
  exit /b 1
)

fc /b requirements.txt venv\requirements.installed >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies ^(the first run takes 5-10 minutes^)...
  venv\Scripts\python.exe -m pip install --upgrade pip || goto :failed
  venv\Scripts\python.exe -m pip install -r requirements.txt || goto :failed
  copy /y requirements.txt venv\requirements.installed >nul
)

if not exist .env (
  copy .env.example .env >nul
  echo Created .env. Put your key in OPENROUTER_API_KEY for GLM-written answers;
  echo without a key the copilot still works and writes template answers.
)

echo Starting the copilot on http://localhost:%PORT%/  ^(Ctrl+C to stop^)
venv\Scripts\python.exe -m uvicorn app.main:app --port %PORT%
exit /b %errorlevel%

:failed
echo Installing dependencies failed. See the error above.
pause
exit /b 1
