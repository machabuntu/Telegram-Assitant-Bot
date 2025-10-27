' Скрипт для скрытого запуска бота без окна консоли
' Используйте этот файл для добавления в автозагрузку

Set WshShell = CreateObject("WScript.Shell")

' Получаем путь к текущей папке
ScriptPath = WshShell.CurrentDirectory
If ScriptPath = "" Then
    ' Если не удалось получить текущую директорию, используем путь к скрипту
    ScriptPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
End If

' Путь к батнику
BatPath = ScriptPath & "\start_bot.bat"

' Запускаем батник скрыто (0 = скрытое окно, False = не ждать завершения)
WshShell.Run """" & BatPath & """", 0, False

Set WshShell = Nothing


