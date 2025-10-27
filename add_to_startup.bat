@echo off
chcp 65001 > nul
title Добавить бота в автозагрузку Windows

echo ====================================
echo  Добавление бота в автозагрузку
echo ====================================
echo.

REM Переходим в директорию со скриптом
cd /d "%~dp0"

REM Получаем путь к папке автозагрузки
set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

REM Проверяем существование VBS файла
if not exist "start_bot_hidden.vbs" (
    echo [ОШИБКА] Файл start_bot_hidden.vbs не найден!
    pause
    exit /b 1
)

REM Создаем ярлык в автозагрузке
set "SCRIPT=%TEMP%\create_shortcut.vbs"
echo Set oWS = WScript.CreateObject("WScript.Shell") > "%SCRIPT%"
echo sLinkFile = "%STARTUP_FOLDER%\AI Assistant Bot.lnk" >> "%SCRIPT%"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%SCRIPT%"
echo oLink.TargetPath = "%CD%\start_bot_hidden.vbs" >> "%SCRIPT%"
echo oLink.WorkingDirectory = "%CD%" >> "%SCRIPT%"
echo oLink.Description = "AI Assistant Bot - Telegram Bot" >> "%SCRIPT%"
echo oLink.Save >> "%SCRIPT%"

cscript //nologo "%SCRIPT%"
del "%SCRIPT%"

echo.
echo [УСПЕХ] Бот добавлен в автозагрузку Windows!
echo.
echo Теперь бот будет автоматически запускаться при входе в систему.
echo Бот будет работать в фоновом режиме без видимого окна.
echo.
echo Для удаления из автозагрузки запустите: remove_from_startup.bat
echo.
pause


