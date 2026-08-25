@echo off
echo ========================================
echo   LicitaPro Peru - Apagando servicios
echo ========================================
echo.

echo [1/2] Deteniendo bots y API...
taskkill /F /IM python.exe 2>nul
echo   Bots y API detenidos.

echo [2/2] Deteniendo Docker...
cd /d "%~dp0"
docker compose down
echo   Docker detenido.

echo.
echo ========================================
echo   TODOS LOS SERVICIOS APAGADOS
echo ========================================
pause
