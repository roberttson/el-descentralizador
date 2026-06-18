@echo off
title El Descentralizador
chcp 65001 >nul
cd /d "%~dp0"
start "" http://localhost:5000
venv\Scripts\python.exe app.py
pause
