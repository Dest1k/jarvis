@echo off
REM ============================================================================
REM  jarvis.bat — единая точка запуска JARVIS-OS (двойной клик на Windows).
REM  Без аргументов поднимает ВСЁ: RPC-мост + стек + дашборд + браузер.
REM  Примеры: jarvis.bat install   |   jarvis.bat stop   |   jarvis.bat status
REM ============================================================================
cd /d "%~dp0"
chcp 65001 >nul
python jarvis.py %*
echo.
echo [JARVIS] Готово. Окно можно закрыть.
pause
