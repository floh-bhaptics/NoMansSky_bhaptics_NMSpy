@echo off
setlocal

echo ============================================================
echo  No Man's Sky bHaptics Mod - Update
echo ============================================================
echo.

if not exist venv (
    echo Virtual environment not found.
    echo Please run "01 Setup.bat" first.
    echo.
    pause
    exit /b 1
)

echo Step 1: Upgrading pip...
venv\Scripts\python -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: pip upgrade failed.
    pause
    exit /b 1
)

echo.
echo Step 2: Upgrading nmspy and bhaptics_python...
venv\Scripts\pip install --upgrade nmspy bhaptics_python
if errorlevel 1 (
    echo ERROR: Package upgrade failed.
    pause
    exit /b 1
)

echo.
echo Step 3: Downloading latest mod files...
curl -L -o NoMansSky_bhaptics_NMSpy_update.zip "https://github.com/floh-bhaptics/NoMansSky_bhaptics_NMSpy/releases/latest/download/NoMansSky_bhaptics_NMSpy.zip"
if errorlevel 1 (
    echo ERROR: Download failed. Check your internet connection and try again.
    pause
    exit /b 1
)

echo.
echo Step 4: Extracting mod files...
REM Extract only the two mod .py files, overwriting existing ones.
REM The zip contains a subfolder, so we strip it with --strip-components equivalent:
REM tar strips the leading folder name automatically with the wildcard below.
tar -xf NoMansSky_bhaptics_NMSpy_update.zip --strip-components=1 --wildcards "*/NoMansSky_bhaptics_nmspy.py" "*/bhaptics_library.py"
if errorlevel 1 (
    echo ERROR: Extraction failed.
    del NoMansSky_bhaptics_NMSpy_update.zip
    pause
    exit /b 1
)

echo.
echo Step 5: Cleaning up...
del NoMansSky_bhaptics_NMSpy_update.zip

echo.
echo ============================================================
echo  Update complete!
echo ============================================================
echo.
pause