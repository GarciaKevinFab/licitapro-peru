@echo off
REM ============================================================
REM  LicitaPro Peru -- arranque de desarrollo
REM
REM  QUE ARRANCABA ANTES Y ESTABA MAL
REM
REM    Levantaba `shared.api_server` en 0.0.0.0:8100 y NO levantaba el panel
REM    web. Es decir: arrancaba la pieza que se acaba de borrar -- una API
REM    cuyo /api/contratos devuelve los contratos de TODOS los inquilinos, y
REM    ademas en 0.0.0.0, o sea visible desde la red local -- y no arrancaba el
REM    producto. Quien seguia estas instrucciones publicaba su cartera entera y
REM    no llegaba a ver la aplicacion.
REM
REM  EL PANEL VA EN EL 8200
REM
REM    El mismo puerto que expone el Dockerfile y que documenta DESPLIEGUE.md.
REM    Tenerlo distinto en desarrollo significa que lo que se prueba aqui no es
REM    lo que corre alli.
REM ============================================================

REM %~dp0 = carpeta de este script. Antes habia una ruta fija que quedo
REM apuntando a un directorio vacio al mover el proyecto: los bots no arrancaban.
cd /d "%~dp0"

set PYTHON="C:\Users\KEVIN GARCIA\AppData\Local\Programs\Python\Python312\python.exe"
set PYTHONPATH=%~dp0
set PYTHONIOENCODING=utf-8

echo ========================================
echo   LicitaPro Peru - Iniciando servicios
echo ========================================
echo.

if not exist ".env" (
  echo [ERROR] No existe .env. Copia la plantilla y rellenala:
  echo         copy .env.example .env
  pause
  exit /b 1
)

echo [0/6] Limpiando procesos anteriores...
taskkill /F /IM python.exe >nul 2>&1
timeout /t 3 /nobreak >nul

echo [1/6] Levantando Docker (PostgreSQL + Redis)...
docker compose up -d
timeout /t 5 /nobreak >nul

REM Las migraciones ANTES que nada que hable con la base. Arrancar el panel
REM contra un esquema viejo da errores de columna inexistente que parecen
REM fallos de codigo y no lo son.
echo [2/6] Aplicando migraciones...
%PYTHON% -m alembic upgrade head
if errorlevel 1 (
  echo [ERROR] Las migraciones fallaron. No se arranca sobre un esquema a medias.
  pause
  exit /b 1
)

echo [3/6] Iniciando panel web (puerto 8200)...
start "" /B %PYTHON% -m uvicorn web.app:app --host 127.0.0.1 --port 8200
timeout /t 3 /nobreak >nul

echo [4/6] Iniciando RadarBot...
start "" /B %PYTHON% -m radar_bot.main
timeout /t 2 /nobreak >nul

echo [5/6] Iniciando PrepBot...
start "" /B %PYTHON% -m prep_bot.main
timeout /t 2 /nobreak >nul

echo [6/6] Iniciando WinBot...
start "" /B %PYTHON% -m win_bot.main
timeout /t 3 /nobreak >nul

echo.
echo ========================================
echo   TODOS LOS SERVICIOS INICIADOS
echo ========================================
echo.
echo   Panel:    http://127.0.0.1:8200
echo   Salud:    http://127.0.0.1:8200/salud
echo.
echo   Para apagar: ejecuta stop.bat
echo ========================================
pause
