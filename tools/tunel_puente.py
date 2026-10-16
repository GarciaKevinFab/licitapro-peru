"""Tunel SSH de la PC puente hasta la base del VPS.

Desde la mudanza la base de LicitaPro vive en el VPS y NO esta abierta a
internet: solo escucha en 127.0.0.1:5442 del servidor. La PC puente llega a
ella por SSH, con una llave que solo sirve para abrir ese tunel (el usuario
`puente` del VPS no tiene consola: su authorized_keys lo limita a reenviar
ese puerto).

El tunel lo abre el propio script de la tarea al empezar y lo cierra al
terminar. Un tunel permanente seria otro proceso que vigilar: si se cae a las
tres de la manana, la tarea de las seis falla sin que nadie sepa por que.

Configuracion, en el .env del puente (la escribe configurar_tunel_puente.ps1):

    PUENTE_TUNEL_SSH=puente@2.24.115.75
    DATABASE_URL=postgresql://puente:...@127.0.0.1:15432/licitapro

Sin PUENTE_TUNEL_SSH no se abre nada: en el VPS y en desarrollo la base se
alcanza directamente.
"""
import contextlib
import logging
import os
import socket
import subprocess
import time
from pathlib import Path

log = logging.getLogger("puente.tunel")

PUERTO_LOCAL = 15432
DESTINO = "127.0.0.1:5442"   # el Postgres del VPS, publicado solo en su loopback
ESPERA_MAX = 25              # segundos para que el tunel acepte conexiones
RAIZ = Path(__file__).resolve().parent.parent


def llave() -> Path:
    return Path(os.getenv("PUENTE_TUNEL_LLAVE")
                or Path.home() / ".ssh" / "licitapro_puente")


def _ruta_ssh(ruta) -> str:
    """La ruta como la entiende el valor de un -o de ssh.

    ssh parte ese valor en los espacios: "C:/Users/KEVIN GARCIA/..." se quedaba
    en "C:/Users/KEVIN". Las comillas lo arreglan en el OpenSSH moderno, pero
    el de Windows Server de la PC puente las rechaza ("invalid quotes"). Asi
    que: barras normales, que los dos aceptan, y comillas solo si hay espacios.
    """
    texto = ruta.as_posix()
    return f'"{texto}"' if " " in texto else texto


def comando(destino_ssh: str) -> list[str]:
    return [
        "ssh", "-N",
        "-L", f"127.0.0.1:{PUERTO_LOCAL}:{DESTINO}",
        "-i", str(llave()),
        # La tarea corre sin nadie delante: una pregunta de "are you sure?"
        # la colgaria para siempre. La huella se acepta la primera vez y
        # despues se exige, guardada junto al proyecto y no en el perfil.
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={_ruta_ssh(RAIZ / 'data' / 'puente_known_hosts')}",
        # Si el puerto local esta ocupado, que falle en vez de quedarse
        # abierto sin reenviar nada.
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ConnectTimeout=15",
        destino_ssh,
    ]


def _acepta(puerto: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", puerto)) == 0


@contextlib.contextmanager
def tunel():
    """Abre el tunel si el .env lo pide; si no, no hace nada."""
    destino_ssh = os.getenv("PUENTE_TUNEL_SSH", "").strip()
    if not destino_ssh:
        yield
        return
    (RAIZ / "data").mkdir(exist_ok=True)
    proceso = subprocess.Popen(
        comando(destino_ssh), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        # Sin ventana negra: la tarea corre con pythonw.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        limite = time.monotonic() + ESPERA_MAX
        while not _acepta(PUERTO_LOCAL):
            if proceso.poll() is not None:
                error = (proceso.stderr.read() or b"").decode(errors="replace").strip()
                raise RuntimeError(f"El tunel SSH al VPS no abrio: {error or 'sin detalle'}")
            if time.monotonic() > limite:
                raise RuntimeError(f"El tunel SSH no acepto conexiones en {ESPERA_MAX} s")
            time.sleep(0.3)
        log.info("Tunel SSH abierto: 127.0.0.1:%d -> VPS %s", PUERTO_LOCAL, DESTINO)
        yield
    finally:
        proceso.terminate()
        try:
            proceso.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proceso.kill()
