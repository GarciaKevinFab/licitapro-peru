@echo off
echo ========================================
echo   LicitaPro Peru - Iniciando servicios
echo ========================================
echo.

cd /d E:\Proyectos\licitapro-peru

set PYTHON="C:\Users\KEVIN GARCIA\AppData\Local\Programs\Python\Python312\python.exe"
set PYTHONPATH=E:\Proyectos\licitapro-peru
set PYTHONIOENCODING=utf-8

echo [0/5] Limpiando procesos anteriores...
taskkill /F /IM python.exe >nul 2>&1
timeout /t 3 /nobreak >nul

echo [1/5] Levantando Docker (PostgreSQL + Redis + N8N)...
docker compose up -d
timeout /t 5 /nobreak >nul

echo [2/5] Iniciando API (puerto 8100)...
start "" /B %PYTHON% -m uvicorn shared.api_server:app --host 0.0.0.0 --port 8100
timeout /t 3 /nobreak >nul

echo [3/5] Iniciando RadarBot...
start "" /B %PYTHON% -m radar_bot.main
timeout /t 2 /nobreak >nul

echo [4/5] Iniciando PrepBot...
start "" /B %PYTHON% -m prep_bot.main
timeout /t 2 /nobreak >nul

echo [5/5] Iniciando WinBot...
start "" /B %PYTHON% -m win_bot.main
timeout /t 3 /nobreak >nul

echo.
echo ========================================
echo   TODOS LOS SERVICIOS INICIADOS
echo ========================================
echo.
echo   API:      http://localhost:8100/api/health
echo   N8N:      http://localhost:5678
echo   Radar:    @LicitaRadar_SI_bot
echo   Prep:     @LicitaPrep_SI_bot
echo   Win:      @LicitaWin_SI_bot
echo.
echo   Para apagar: ejecuta stop.bat
echo ========================================
pause
