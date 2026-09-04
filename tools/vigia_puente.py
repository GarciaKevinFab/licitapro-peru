"""Vigila el sitio publico DESDE PERU, en la maquina que hace de puente.

POR QUE HACE FALTA UN TERCER VIGILANTE

  Ya hay dos y ninguno cubre esto:

    - `shared/vigilancia.py` mira si ENTRAN DATOS. Avisa cuando OECE deja de
      dar cosecha; no sabe nada de si la web responde.
    - `.github/workflows/vigia.yml` si pregunta por `/salud`. Le fallan dos
      cosas, las dos medidas el 31/08/2026:

        1. Su cron pide una comprobacion cada 15 minutos y GitHub le entrega
           una cada 2 a 4 horas. Estrangula los cron frecuentes en
           repositorios publicos, asi que una caida real puede tardar cuatro
           horas y media en avisar. No se arregla pidiendo mas.
        2. Pregunta desde un centro de datos de Estados Unidos. Los clientes
           entran desde Peru, y este proyecto ya sabe de sobra que eso NO es
           lo mismo: OECE contesta 200 a una conexion peruana y 403 al VPS.

  Esta maquina ya esta encendida siempre y ya tiene conexion peruana. Preguntar
  desde aqui cuesta una peticion cada cinco minutos y mide lo unico que
  importa: lo que ve un cliente.

NO SUSTITUYE AL DE GITHUB, Y ES DELIBERADO

  Si esta PC se apaga, este vigilante se apaga con ella y no puede avisar de su
  propio silencio. El de GitHub corre en otra parte y sigue. Cada uno cubre el
  punto ciego del otro, y por eso los mensajes dicen DESDE DONDE se miro: sin
  eso, dos avisos distintos del mismo corte parecen dos averias.

SOLO HABLA CUANDO CAMBIA EL ESTADO

  Cada cinco minutos son 288 comprobaciones al dia. Avisar en todas seria
  ruido, y el ruido termina en silenciar el chat, que es la forma callada de
  quedarse sin vigilante.

USO
  .venv-tarea/Scripts/python.exe tools/vigia_puente.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# Por ruta absoluta y no por descubrimiento: el Programador de tareas de
# Windows arranca en C:\Windows\System32, donde no hay ningun .env. Sin esto la
# tarea correria sin token y no avisaria de nada, en silencio. Es el mismo
# tropiezo que ya documenta tools/traer_oece.py.
from dotenv import load_dotenv

load_dotenv(RAIZ / ".env")

import httpx

from shared import fechas

# El logger del modulo, en vez de las funciones sueltas de `logging`.
#
# `log.info(...)` escribe en el logger RAIZ. Funciona mientras este script
# sea lo unico que corre, pero en cuanto alguien importe algo de `shared` -- que
# ya trae sus propios loggers -- los mensajes de los dos se mezclan sin que se
# pueda distinguir de donde sale cada uno, ni silenciar uno sin silenciar el
# otro. Con un logger propio, el nombre viaja en cada linea.
#
# basicConfig() se queda: configurar los manejadores SI es cosa del script de
# entrada, y este lo es.
log = logging.getLogger("puente.vigia")

REGISTRO = RAIZ / "data" / "vigia_puente.log"
ARCHIVO_ESTADO = RAIZ / "data" / "vigia_puente.estado"

# Tres intentos, no uno. El vigia de GitHub abrio dos incidencias falsas en
# siete horas por creerse el primer tropiezo de red. Una caida de verdad
# sobrevive a los tres; un tropiezo, no.
INTENTOS = 3
ESPERA = 10


def _preparar_log() -> None:
    REGISTRO.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        # Con pythonw.exe -- que es como lo lanza la tarea, sin ventana negra --
        # sys.stdout es None, y un StreamHandler contra None revienta al
        # escribir.
        handlers=[h for h in (
            logging.FileHandler(REGISTRO, encoding="utf-8"),
            logging.StreamHandler(sys.stdout) if sys.stdout else None,
        ) if h],
    )


def url_salud() -> str:
    base = os.getenv("LICITAPRO_URL_PUBLICA") or "https://licitapro.sisac.pe"
    return base.rstrip("/") + "/salud"


# ─── Lo que se comprueba ─────────────────────────────────

def sondear(url: str, solo_ipv4: bool = False) -> tuple[int, dict | None, str]:
    """(codigo, json, detalle_tecnico). Codigo 0 = no hubo respuesta.

    POR QUE EXISTE EL MODO solo_ipv4

      El 31/08/2026, en su primer dia, este vigilante mando DIEZ avisos en
      ocho horas: caidas de 39 a 180 minutos... con el sitio respondiendo 200
      desde otra conexion peruana y desde GitHub durante esas mismas horas. Y
      los propios avisos rojos LLEGARON por Telegram desde esta PC, o sea que
      internet habia.

      La ruta IPv6 del proveedor hacia Cloudflare va por rachas. Un navegador
      no lo nota -- cae a IPv4 en milisegundos --, asi que los clientes entran
      con normalidad; httpx sin ese mecanismo se queda colgado en la familia
      rota y reporta una caida que ningun humano ve. Forzar IPv4 imita al
      navegador: `local_address="0.0.0.0"` restringe el socket a AF_INET.
    """
    kwargs = {}
    if solo_ipv4:
        kwargs["transport"] = httpx.HTTPTransport(local_address="0.0.0.0")
    try:
        with httpx.Client(timeout=25, follow_redirects=True, **kwargs) as c:
            r = c.get(url)
    except Exception as e:  # noqa: BLE001
        return 0, None, f"{type(e).__name__}: {e}"[:200]
    try:
        return r.status_code, r.json(), ""
    except Exception:  # noqa: BLE001
        return r.status_code, None, "el cuerpo no es JSON"


TESTIGO = "https://www.gob.pe"


def hay_internet() -> bool:
    """Si esta PC llega a un tercero que no somos nosotros.

    Sin esto, un corte del proveedor se contaria como caida del sitio y la
    duracion del "estuvo caido X minutos" seria mentira. Con el testigo caido
    no se puede saber nada del sitio: la pasada se salta sin tocar el estado.
    """
    try:
        with httpx.Client(timeout=10,
                          transport=httpx.HTTPTransport(local_address="0.0.0.0")) as c:
            return c.get(TESTIGO).status_code < 500
    except Exception:  # noqa: BLE001
        return False


def evaluar(codigo: int, datos: dict | None) -> tuple[bool, str]:
    """(sano, motivo). El motivo se escribe para leerse en un movil.

    Los casos se separan porque llevan a sitios distintos: sin conexion se
    mira el tunel o el DNS; un 200 con la base caida se mira en Supabase. Un
    aviso que no distingue eso hace perder la primera media hora.
    """
    if codigo == 0:
        return False, "no responde en absoluto (sin conexion, DNS o tunel)"
    if codigo == 503:
        # `/salud` devuelve 503 cuando la aplicacion vive pero no alcanza la
        # base. Decir solo "codigo 503" seria exacto e inutil: manda a mirar el
        # VPS cuando el problema esta en Supabase.
        return False, ("la web responde pero no alcanza la base de datos "
                       "(Supabase o el pooler)")
    if codigo != 200:
        return False, f"responde con codigo {codigo}"
    if not datos:
        return False, "responde 200 pero el cuerpo no es el JSON de /salud"
    if datos.get("base") != "ok":
        return False, f"la web responde pero la base dice '{datos.get('base')}'"
    if datos.get("estado") != "ok":
        return False, f"la aplicacion se declara '{datos.get('estado')}'"
    return True, "ok"


# ─── Cuando hablar ───────────────────────────────────────

def _minutos(desde: str | None, ahora: datetime) -> int | None:
    if not desde:
        return None
    try:
        transcurrido = ahora - datetime.fromisoformat(desde)
    except ValueError:
        return None
    return max(0, int(transcurrido.total_seconds() // 60))


def decidir(previo: dict | None, sano: bool, motivo: str,
            ahora: datetime) -> tuple[dict, str | None]:
    """Nuevo estado y el mensaje a enviar, o None si no hay que decir nada.

    None es el caso NORMAL: 288 comprobaciones al dia y, con suerte, ningun
    aviso. Solo se habla en las transiciones.

    LA PRIMERA EJECUCION NO FELICITA

      Sin estado previo y con el sitio sano no se manda nada. Si mandara "ya
      responde", cada instalacion empezaria anunciando la recuperacion de una
      caida que nunca existio, y el primer mensaje de un vigilante nuevo no
      puede ser uno falso: es lo que ensena a no creerselo.
    """
    nombre = "sano" if sano else "caido"
    estado = {"estado": nombre,
              "desde": ahora.isoformat(timespec="seconds"),
              "motivo": motivo}

    if previo and previo.get("estado") == nombre:
        # Sin cambio: se conserva el "desde" original, que es lo que permite
        # decir cuanto duro la caida cuando termine.
        estado["desde"] = previo.get("desde") or estado["desde"]
        return estado, None

    if not sano:
        return estado, (
            f"🔴 LicitaPro no responde DESDE PERU: {motivo}. "
            f"Comprobado {INTENTOS} veces desde la PC del puente. "
            f"Se avisara tambien cuando vuelva.")

    if previo is None:
        return estado, None

    duro = _minutos(previo.get("desde"), ahora)
    cuanto = f" (estuvo caido {duro} minutos)" if duro is not None else ""
    return estado, f"✅ LicitaPro responde otra vez desde Peru{cuanto}."


# ─── Estado en disco ─────────────────────────────────────

def leer_estado() -> dict | None:
    try:
        return json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        # Un estado ilegible se trata como "no habia": se pierde la memoria de
        # una caida en curso, no el vigilante entero.
        return None


def guardar_estado(estado: dict) -> None:
    ARCHIVO_ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVO_ESTADO.write_text(json.dumps(estado, ensure_ascii=False),
                              encoding="utf-8")


# ─── Aviso ───────────────────────────────────────────────

def avisar(texto: str) -> bool:
    token = os.getenv("RADAR_BOT_TOKEN") or os.getenv("WIN_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_ADMIN_ID")
    if not token or not chat:
        log.error("Sin RADAR_BOT_TOKEN o TELEGRAM_ADMIN_ID: no se puede "
                      "avisar. El vigilante esta mudo.")
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": texto[:4000],
                             "disable_web_page_preview": True},
                       timeout=20)
        if r.status_code != 200:
            log.error("Telegram respondio %s: %s", r.status_code,
                          r.text[:200])
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.error("No se pudo avisar por Telegram: %s", e)
        return False


def main() -> int:
    _preparar_log()
    url = url_salud()

    sano, motivo, detalle = False, "sin comprobar", ""
    for n in range(1, INTENTOS + 1):
        codigo, datos, detalle = sondear(url)
        sano, motivo = evaluar(codigo, datos)
        if sano:
            break
        log.info("Intento %d de %d: %s%s", n, INTENTOS, motivo,
                     f" [{detalle}]" if detalle else "")
        if n < INTENTOS:
            time.sleep(ESPERA)

    if not sano:
        # Cuarta pregunta forzando IPv4: si asi responde, el sitio ESTA sano
        # -- los navegadores caen a IPv4 solos -- y lo roto es la ruta IPv6
        # del proveedor. Se registra, no se grita.
        codigo, datos, _ = sondear(url, solo_ipv4=True)
        sano4, _ = evaluar(codigo, datos)
        if sano4:
            log.warning("La ruta por defecto fallo pero IPv4 responde: "
                            "el sitio esta sano; la IPv6 del proveedor esta "
                            "rota a ratos. No se avisa.")
            sano, motivo = True, "ok"
        elif not hay_internet():
            # Sin salida a internet no se sabe nada del sitio. No se toca el
            # estado: contarlo como caida inventaria la duracion del proximo
            # "estuvo caido X minutos".
            log.warning("Sin salida a internet (el testigo %s tampoco "
                            "responde). Pasada saltada sin tocar el estado.",
                            TESTIGO)
            return 0
        else:
            # Caida real, con el detalle tecnico en el propio aviso: el
            # primer dia de este vigilante se perdio una tarde por un mensaje
            # que no decia si era DNS, timeout o rechazo.
            if detalle:
                motivo = f"{motivo} [{detalle[:120]}]"

    ahora = fechas.ahora()
    estado, mensaje = decidir(leer_estado(), sano, motivo, ahora)
    guardar_estado(estado)

    if mensaje:
        log.warning("CAMBIO DE ESTADO: %s", mensaje)
        if not avisar(mensaje):
            # Se devuelve fallo para que el Programador de tareas lo marque en
            # rojo: un vigilante que detecta la caida y no consigue contarla
            # sirve exactamente igual que no tenerlo.
            return 1
    else:
        log.info("Sin cambios: %s (%s)", estado["estado"], motivo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
