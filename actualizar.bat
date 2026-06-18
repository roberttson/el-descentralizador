@echo off
title El Descentralizador - Ingesta
chcp 65001 >nul
cd /d "%~dp0"
venv\Scripts\python.exe ingestar.py
pause
