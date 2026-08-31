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
$version = (& $python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))")
Write-Host "Python del sistema: $python  (version $version)"

# $ErrorActionPreference NO alcanza a los programas externos: PowerShell no
# lanza excepcion porque pip termine en error, solo por errores de cmdlets. Sin
# comprobar $LASTEXITCODE a mano, el script seguia adelante con el entorno a
# medias y llegaba a registrar la tarea. Paso de verdad: pip murio pidiendo un
# compilador de C++ y aun asi se imprimio "Tarea registrada" y "Listo".
#
# Una tarea que apunta a un entorno incompleto no avisa de nada: corre cada 4
# horas con pythonw -- sin ventana --, revienta al importar y no deja ni
# registro, porque el fallo ocurre antes de que se configure el log.
function Comprobar([string]$que) {
    if ($LASTEXITCODE -ne 0) {
        throw "$que fallo (codigo $LASTEXITCODE). NO se registra la tarea: mejor sin puente que con uno que falla en silencio cada 4 horas."
    }
}

if (-not (Test-Path $Py)) {
    Write-Host "Creando el entorno en $Venv ..."
    & $python -m venv $Venv
    Comprobar "La creacion del entorno"
}
Write-Host "Instalando dependencias ..."
& $Py -m pip install --quiet --upgrade pip
Comprobar "La actualizacion de pip"

# --only-binary=:all: prohibe compilar desde codigo fuente.
#
#   Sin esto, una dependencia sin binario para tu version de Python se intenta
#   compilar y el fallo llega envuelto en 267 lineas de salida de setuptools,
#   con el motivo real -- "Microsoft Visual C++ 14.0 or greater is required" --
#   enterrado al final. Con esto, pip dice en una linea que no hay binario, que
#   es un problema distinto y con otra solucion.
& $Py -m pip install --quiet --only-binary=:all: -r (Join-Path $Raiz 'requirements-puente.txt')
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Warning "No hay binarios para Python $version. Las dependencias se publican con retraso para cada version nueva de Python."
    Write-Warning "Lo mas rapido: instalar Python 3.13 desde python.org, borrar la carpeta .venv-tarea y volver a correr este script."
    throw "La instalacion de dependencias fallo. NO se registra la tarea."
}

# La prueba que de verdad decide: que el codigo del puente IMPORTE. Una lista de
# dependencias puede instalarse entera y aun asi faltar algo que el codigo usa.
Write-Host "Comprobando que el puente pueda importar ..."
& $Py -c "import sys; sys.path.insert(0, r'$Raiz'); import radar_bot.scrapers.ocds_oece; import tools.traer_oece"
Comprobar "La comprobacion de imports del puente"
Write-Host "  imports correctos."

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

# --- 3b. El vigilante del sitio -----------------------------------------------
#
#   Segunda tarea y no un anadido a la primera, porque miden cosas distintas y
#   a ritmos distintos: el puente TRAE datos cada 4 horas, y esto PREGUNTA si la
#   web responde cada 5 minutos.
#
#   POR QUE AQUI, SI YA HAY UN VIGIA EN GITHUB
#
#     Al de GitHub le fallan dos cosas, medidas el 31/08/2026:
#
#       1. Su cron pide una comprobacion cada 15 minutos y GitHub le entrega
#          una cada 2 a 4 horas: estrangula los cron frecuentes en repos
#          publicos. Una caida real puede tardar cuatro horas y media en
#          avisar.
#       2. Pregunta desde un centro de datos de Estados Unidos. Los clientes
#          entran desde Peru, y este proyecto ya sabe que no es lo mismo: OECE
#          responde 200 a una conexion peruana y 403 al VPS.
#
#     Los dos se quedan. Si esta PC se apaga, este vigilante se apaga con ella
#     y no puede avisar de su propio silencio; el de GitHub sigue corriendo.
$TareaVigia = 'LicitaPro - Vigia del sitio'
$accionVigia = New-ScheduledTaskAction -Execute $PyW `
    -Argument (Join-Path $Raiz 'tools\vigia_puente.py') -WorkingDirectory $Raiz

$disparoVigia = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)

# Limite de 5 minutos: una pasada son tres peticiones HTTP. Si alguna se
# cuelga, no puede solaparse con la siguiente ni quedarse viva para siempre.
$ajustesVigia = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TareaVigia -Action $accionVigia `
    -Trigger $disparoVigia -Principal $principal -Settings $ajustesVigia -Force | Out-Null
Write-Host "Tarea '$TareaVigia' registrada: cada 5 minutos."

# Una pasada inmediata, para que el estado quede escrito y no se avise de una
# recuperacion inventada en la primera comprobacion automatica.
Start-ScheduledTask -TaskName $TareaVigia

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
