@echo off
title Running Streamlit App
cd /d "%~dp0"

echo [1/2] Checking and Installing libraries...
pip install streamlit pandas telethon plotly

echo.
echo [2/2] Starting Streamlit Application...
python -m streamlit run index.py

pause
