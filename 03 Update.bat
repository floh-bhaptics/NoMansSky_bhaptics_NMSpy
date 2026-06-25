@echo off
setlocal

echo ============================================================
echo  No Man's Sky bHaptics Mod - Update
echo ============================================================
echo.

if not exist venv (
    echo Virtual environment not found.
    echo Please run "setup.bat" first.
    echo.
    pause
    exit /b 1
)

echo Updating nmspy and bhaptics_python to latest versions...
echo.
venv\Scripts\pip install --upgrade nmspy bhaptics_python
if errorlevel 1 (
    echo ERROR: Update failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Update complete!
echo ============================================================
echo.
pause
