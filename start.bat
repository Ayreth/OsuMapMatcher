@echo off
setlocal
cd /d "%~dp0"
title osu!MapMatcher

echo ================================================
echo   osu!MapMatcher
echo ================================================
echo.

REM Delayed expansion eats "!" in echoed text, so it is only switched on here,
REM below the banner, where !errorlevel! is actually needed.
setlocal EnableDelayedExpansion

REM ---- find a usable Python -------------------------------------------------
set "PY="
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    goto :run
)

for %%C in ("py -3.12" "py -3" "python") do (
    %%~C -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
    if !errorlevel! equ 0 (
        set "BASEPY=%%~C"
        goto :havepython
    )
)

REM the Microsoft Store stub answers to "python" but is not a real interpreter
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "BASEPY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :havepython
)

echo Python 3.11 or newer was not found.
echo.
where winget >nul 2>&1
if %errorlevel% equ 0 (
    echo Installing Python 3.12 with winget. Accept the prompt if one appears.
    winget install --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "BASEPY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        goto :havepython
    )
)
echo Install Python from https://www.python.org/downloads/ and run this again.
echo.
pause
exit /b 1

:havepython
echo Creating the virtual environment ^(one time^)...
%BASEPY% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo Failed to create the virtual environment.
    pause
    exit /b 1
)
set "PY=.venv\Scripts\python.exe"

echo Installing dependencies ^(one time, a couple of minutes^)...
"%PY%" -m pip install --quiet --upgrade pip
"%PY%" -m pip install --quiet -e ".[corpus,index,server]"
if errorlevel 1 (
    echo Dependency install failed. Check your internet connection.
    pause
    exit /b 1
)

:run
"%PY%" -m osumapmatcher.launcher %*
if errorlevel 1 (
    echo.
    echo osu!MapMatcher stopped with an error.
    pause
)
endlocal
