@echo off
setlocal

echo ============================================================
echo  No Man's Sky bHaptics Mod - First-time Setup
echo ============================================================
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found.
    echo Please install Python 3.9-3.11 from https://www.python.org/downloads/
    echo Make sure to tick "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM Check Python version is compatible (3.9-3.11, NOT 3.12+)
python -c "import sys; exit(0 if (3,9) <= sys.version_info < (3,12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Incompatible Python version detected.
    python --version
    echo NMS.py requires Python 3.9, 3.10, or 3.11.
    echo Please install one of those versions from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo Python version OK:
python --version
echo.

REM Create virtual environment
echo Creating virtual environment...
if exist venv (
    echo Removing old virtual environment...
    rmdir /s /q venv
)
python -m venv venv
if errorlevel 1 (
    echo ERROR: Failed to create virtual environment.
    pause
    exit /b 1
)

REM Install packages
echo.
echo Installing packages (this may take a minute)...
venv\Scripts\pip install --upgrade pip --quiet
venv\Scripts\pip install nmspy bhaptics_python
if errorlevel 1 (
    echo ERROR: Package installation failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Setup complete! Run "Launch.bat" to start the mod.
echo ============================================================
echo.
pause
