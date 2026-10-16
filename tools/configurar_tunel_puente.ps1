# Conecta la PC puente con la base del VPS por tunel SSH.
#
# Se corre en la PC puente, desde la carpeta del proyecto, en PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File tools\configurar_tunel_puente.ps1
#
# PRIMERA VEZ: crea la llave (~\.ssh\licitapro_puente), copia su parte publica
# al portapapeles y se detiene. Esa linea publica se registra en el VPS
# (usuario `puente`, que solo puede abrir el tunel hacia la base).
#
# SEGUNDA VEZ, ya registrada: le pide al VPS la conexion de la base -- el VPS
# la entrega a esta llave y a nadie mas -- y la escribe en el .env junto con
# PUENTE_TUNEL_SSH. Asi la contrasena no pasa por el chat ni por el portapapeles.
# Al final prueba la base a traves del tunel.

$ErrorActionPreference = 'Stop'
$Raiz  = Split-Path -Parent $PSScriptRoot
$Env   = Join-Path $Raiz '.env'
$Llave = Join-Path $HOME '.ssh\licitapro_puente'
$Vps   = 'puente@2.24.115.75'
$Hosts = Join-Path $Raiz 'data\puente_known_hosts'

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "No hay cliente ssh. En Windows: Configuracion > Aplicaciones > Caracteristicas opcionales > Cliente OpenSSH."
}
if (-not (Test-Path $Env)) { throw "Falta $Env." }
New-Item -ItemType Directory -Force (Join-Path $Raiz 'data') | Out-Null

if (-not (Test-Path $Llave)) {
    New-Item -ItemType Directory -Force (Split-Path $Llave) | Out-Null
    ssh-keygen -t ed25519 -f $Llave -N '""' -C "licitapro-puente@$env:COMPUTERNAME" | Out-Null
    $pub = Get-Content "$Llave.pub" -Raw
    Set-Clipboard -Value $pub.Trim()
    Write-Host ""
    Write-Host "Llave creada. Su parte PUBLICA ya esta copiada en el portapapeles:" -ForegroundColor Green
    Write-Host $pub
    Write-Host "Pegasela a Claude en el chat. Cuando la registre, vuelve a correr este script."
    exit 0
}

Write-Host "Pidiendo al VPS la conexion de la base..."
# -T: sin terminal. Sin el, ssh avisa "Pseudo-terminal will not be allocated"
# y ese aviso se mezclaba con la conexion. Se toma solo la linea que importa.
$salida = ssh -T -i $Llave -o BatchMode=yes -o StrictHostKeyChecking=accept-new `
             -o "UserKnownHostsFile=`"$Hosts`"" -o ConnectTimeout=15 $Vps 2>&1
$linea = @($salida) | Where-Object { "$_" -match '^DATABASE_URL=postgresql://' } | Select-Object -First 1
if (-not $linea) {
    throw "El VPS no entrego la conexion. Si la llave aun no esta registrada, pasale a Claude esta linea:`n$(Get-Content "$Llave.pub" -Raw)`nDetalle: $($salida -join ' ')"
}

$respaldo = "$Env.antes-del-tunel-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
Copy-Item $Env $respaldo
$resto = Get-Content $Env | Where-Object { $_ -notmatch '^(DATABASE_URL|PUENTE_TUNEL_SSH|POSTGRES_ZONA_SESION)=' }
# La sesion en UTC, como el servidor: sin esto el pool pediria hora de Lima y
# lo que el puente guarde con NOW() quedaria cinco horas corrido.
$nuevo = @($resto) + "$linea".Trim() + "PUENTE_TUNEL_SSH=$Vps" + "POSTGRES_ZONA_SESION=UTC"
# UTF-8 sin BOM: python-dotenv leeria el BOM como parte del primer nombre.
[System.IO.File]::WriteAllLines($Env, $nuevo, (New-Object System.Text.UTF8Encoding $false))
Write-Host ".env actualizado (copia del anterior: $respaldo)."

Write-Host "Probando la base a traves del tunel..."
$Py = Join-Path $Raiz '.venv-tarea\Scripts\python.exe'
if (-not (Test-Path $Py)) { $Py = 'python' }
$prueba = @'
import asyncio, sys
sys.path.insert(0, r".")
from dotenv import load_dotenv
load_dotenv(".env", override=True)
from tools.tunel_puente import tunel
from shared.db import connection
async def main():
    async with connection() as c:
        n = await c.fetchval("SELECT count(*) FROM licitaciones")
        print(f"OK: la base del VPS responde ({n} licitaciones).")
with tunel():
    asyncio.run(main())
'@
Push-Location $Raiz
try { $prueba | & $Py - } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw "La prueba fallo. El .env anterior esta en $respaldo." }
Write-Host "Listo. La tarea 'LicitaPro - Traer OECE' ya usa el tunel." -ForegroundColor Green
