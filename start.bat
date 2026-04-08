@echo off
chcp 65001 >nul
title Polymarket Copy-Bot

echo ============================================================
echo  Polymarket Copy-Trading Bot
echo ============================================================

:: Проверяем Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python не найден. Установите Python 3.9+ и добавьте в PATH.
    pause
    exit /b 1
)

:: Проверяем .env
if not exist .env (
    echo [INFO] .env не найден, создаю из шаблона...
    copy .env.example .env
    echo [!] Отредактируйте .env перед запуском! Как минимум заполните:
    echo     - TELEGRAM_BOT_TOKEN
    echo     - TELEGRAM_CHAT_ID
    echo.
    notepad .env
    pause
    exit /b 0
)

:: Устанавливаем зависимости при первом запуске
if not exist .deps_installed (
    echo [INFO] Установка зависимостей...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Ошибка установки зависимостей.
        pause
        exit /b 1
    )
    echo. > .deps_installed
)

:: Запуск бота
echo.
echo [START] Запуск бота в режиме DRY-RUN...
echo [INFO]  Дашборд: http://localhost:5000
echo [INFO]  Для остановки нажмите Ctrl+C
echo.
python bot.py

pause
