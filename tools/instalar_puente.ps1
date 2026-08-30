<#
.SINOPSIS
  Monta el puente de OECE en una maquina Windows peruana.

.DESCRIPCION
  OECE responde 403 a todo el trafico del VPS -- sale por una IP de datacenter
  fuera de Peru -- y 200 desde una conexion peruana. Mientras eso no se
  resuelva de raiz, una maquina en Peru hace de puente: pide los datos y los
  escribe en la MISMA base de Supabase que usa el servidor, asi que las
  licitaciones aparecen en el panel de los clientes igual que si las hubiera
  traido el bot.

  Este script deja esa maquina lista: crea el entorno, instala lo justo y
  programa la tarea. Correrlo dos veces no rompe nada.

.USO
  Boton derecho sobre PowerShell -> Ejecutar. Despues:
      cd C:\ruta\al\proyecto
      .\tools\instalar_puente.ps1
#>
$ErrorActionPreference = 'Stop'

$Raiz  = Split-Path -Parent $PSScriptRoot
$Venv  = Join-Path $Raiz '.venv-tarea'
$PyW   = Join-Path $Venv 'Scripts\pythonw.exe'
$Py    = Join-Path $Venv 'Scripts\python.exe'
$Tarea = 'LicitaPro - Traer OECE'

Write-Host "Proyecto: $Raiz"

# --- 1. El .env, que es lo unico que no viaja en el repositorio ---------------
#
#   Sin el, DATABASE_URL llega vacia y el scraper cae al Postgres local de las
#   variables POSTGRES_*. Eso NO da error: la tarea corre "bien", guarda las
#   licitaciones en una base de desarrollo, y el panel de los clientes sigue
#   vacio sin una sola linea en rojo. Por eso se para aqui.
$Env = Join-Path $Raiz '.env'
if (-not (Test-Path $Env)) {
    throw "Falta $Env. Copialo desde la otra maquina antes de seguir: lleva DATABASE_URL y sin el la tarea escribiria en la base equivocada sin avisar."
}
if ((Get-Content $Env -Raw) -notmatch '(?m)^DATABASE_URL=.+') {
    throw "El .env existe pero DATABASE_URL esta vacia. El puente escribiria en una base local en vez de en la de produccion."
}

# El puente va DIRECTO a OECE. Si alguien copio las variables del servidor, las
# peticiones darian la vuelta por Cloudflare para acabar bloqueadas igual.
if ((Get-Content $Env -Raw) -match '(?m)^OECE_PROXY_URL=.+') {
    throw "OECE_PROXY_URL esta puesta en el .env. Desde una maquina peruana hay que ir directo: vaciala."
}

# --- 2. Python ----------------------------------------------------------------
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "No hay python en el PATH. Instalalo desde python.org marcando 'Add python.exe to PATH'." }
Write-Host "Python del sistema: $python"

if (-not (Test-Path $Py)) {
    Write-Host "Creando el entorno en $Venv ..."
    & $python -m venv $Venv
}
Write-Host "Instalando dependencias ..."
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet -r (Join-Path $Raiz 'requirements-puente.txt')

# --- 3. La tarea programada ----------------------------------------------------
#
#   S4U, o sea "ejecutar tanto si el usuario inicio sesion como si no", SIN
#   guardar contrasena.
#
#   La tarea anterior estaba como Interactive: solo corria con la sesion
#   abierta. En una PC que se deja encendida como servidor eso es una trampa,
#   porque la maquina puede estar encendida y bloqueada, o reiniciarse y quedar
#   en la pantalla de inicio de sesion, y el puente no correr durante dias sin
#   que nada lo diga.
#
#   S4U no puede alcanzar recursos de red que pidan credenciales -- carpetas
#   compartidas y demas --, pero salir a internet por HTTPS si, que es todo lo
#   que hace esto.
$accion = New-ScheduledTaskAction -Execute $PyW `
    -Argument (Join-Path $Raiz 'tools\traer_oece.py') -WorkingDirectory $Raiz

$disparo = New-ScheduledTaskTrigger -Once -At (Get-Date -Hour 6 -Minute 0 -Second 0) `
    -RepetitionInterval (New-TimeSpan -Hours 4)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

# -StartWhenAvailable recupera la pasada que se perdio si la maquina estuvo
# apagada; sin el, un reinicio a las 5:59 se come la corrida de las 6 y hay que
# esperar cuatro horas.
$ajustes = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $Tarea -Action $accion -Trigger $disparo `
    -Principal $principal -Settings $ajustes -Force | Out-Null
Write-Host "Tarea '$Tarea' registrada: cada 4 horas, corra o no sesion."

# --- 4. Aviso sobre la suspension ---------------------------------------------
#
#   No se cambia la configuracion de energia por cuenta propia: es un ajuste
#   del sistema y el dueno de la maquina decide. Pero si se suspende, el puente
#   no corre, asi que al menos se dice.
$susp = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>$null | Select-String 'Indice de configuracion actual de CA|Current AC Power Setting Index')
if ($susp -and $susp -notmatch '0x00000000') {
    Write-Warning "Esta maquina se suspende sola. Suspendida no corre el puente. Cambialo en: Configuracion -> Sistema -> Inicio/apagado -> Suspension -> Nunca."
}

# --- 5. Comprobar que de verdad trae datos ------------------------------------
Write-Host ""
Write-Host "Ejecutando una pasada de prueba (puede tardar un minuto) ..."
Start-ScheduledTask -TaskName $Tarea
Start-Sleep -Seconds 45
$log = Join-Path $Raiz 'data\traer_oece.log'
if (Test-Path $log) {
    Write-Host "--- ultimas lineas de $log ---"
    Get-Content $log -Tail 8 | ForEach-Object { "  $_" }
} else {
    Write-Warning "No se creo $log todavia. Revisa el Programador de tareas."
}
Write-Host ""
Write-Host "Listo. Si arriba dice 'Guardadas N licitaciones nuevas', el puente funciona."
