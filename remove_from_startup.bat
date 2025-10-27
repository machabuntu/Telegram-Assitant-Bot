@echo off
chcp 65001 > nul
title Удалить бота из автозагрузки Windows

echo ====================================
echo  Удаление бота из автозагрузки
echo ====================================
echo.

REM Получаем путь к папке автозагрузки
set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP_FOLDER%\AI Assistant Bot.lnk"

REM Проверяем существование ярлыка
if not exist "%SHORTCUT%" (
    echo [INFO] Бот не найден в автозагрузке.
    echo Ярлык не существует: %SHORTCUT%
    echo.
    pause
    exit /b 0
)

REM Удаляем ярлык
del "%SHORTCUT%"

if not exist "%SHORTCUT%" (
    echo [УСПЕХ] Бот успешно удален из автозагрузки!
) else (
    echo [ОШИБКА] Не удалось удалить ярлык из автозагрузки.
    echo Попробуйте удалить его вручную: %SHORTCUT%
)

echo.
pause


