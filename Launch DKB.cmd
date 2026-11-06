@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHONW=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" (
    echo The repository Python environment is missing. Run the project setup first.
    pause
    exit /b 1
)
start "" "%PYTHONW%" "%ROOT%tools\launch_dkb.py" %*
