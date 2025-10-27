@echo off
chcp 65001 > nul
title Остановить бота

echo ====================================
echo    Остановка AI Assistant Bot
echo ====================================
echo.

REM Ищем процесс Python, запущенный с ai_assistant_bot.py
echo [INFO] Поиск запущенного процесса бота...

REM Получаем список процессов Python и фильтруем по имени файла
for /f "tokens=2" %%i in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| find "PID:"') do (
    wmic process where "ProcessId=%%i" get CommandLine 2>nul | find /i "ai_assistant_bot.py" >nul
    if not errorlevel 1 (
        echo [INFO] Найден процесс с PID: %%i
        echo [INFO] Останавливаю процесс...
        taskkill /F /PID %%i >nul 2>&1
        if not errorlevel 1 (
            echo [УСПЕХ] Бот остановлен!
        ) else (
            echo [ОШИБКА] Не удалось остановить процесс.
        )
        goto :found
    )
)

echo [INFO] Процесс бота не найден. Возможно, бот уже остановлен.
:found

echo.
pause


