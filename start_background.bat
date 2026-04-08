@echo off
chcp 65001 >nul
title Polymarket Bot (Background)

echo [INFO] Запуск бота в фоновом режиме...

:: Создаём папку логов
if not exist logs mkdir logs

:: Запускаем без окна, перенаправляя вывод в лог
start /B pythonw bot.py > logs\stdout.log 2>&1

echo [OK] Бот запущен в фоне.
echo [INFO] Логи: logs\bot.log
echo [INFO] Дашборд: http://localhost:5000
echo.
echo Для остановки:
echo   taskkill /F /IM pythonw.exe
echo   или через дашборд
