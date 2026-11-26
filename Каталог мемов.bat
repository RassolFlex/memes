@echo off
chcp 65001 >nul
title Каталог мемов
python "%~dp0_catalog\memes.py" serve
if errorlevel 1 pause
