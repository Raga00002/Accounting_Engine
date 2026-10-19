@echo off
REM Accounting Engine launcher (Windows)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo  ==^> Python 3 was not found. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) and run this again.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
)
REM (Re)install packages if a previous install was interrupted (e.g. no internet the first time).
".venv\Scripts\python.exe" -c "import streamlit, anthropic, openpyxl, rapidfuzz, pydantic, cryptography, authlib" >nul 2>&1
if errorlevel 1 (
    echo Installing packages...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo  ==^> Package installation failed. Check your internet connection and run this again.
        pause
        exit /b 1
    )
)
if not exist ".env" (
    copy .env.example .env
    echo.
    echo  ==^> A new .env file was created. Open it and paste your ANTHROPIC_API_KEY, then run this again.
    echo.
    notepad .env
    exit /b
)
REM Skip Streamlit's first-run e-mail prompt and usage statistics.
set STREAMLIT_SERVER_SHOW_EMAIL_PROMPT=false
set STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
".venv\Scripts\python.exe" -m streamlit run app.py
REM Keep the window open if Streamlit exits with an error, so the message can be read.
if errorlevel 1 pause
