"""Trae convocatorias peruanas desde ESTA maquina y las guarda en produccion.

EL ARCHIVO SE SIGUE LLAMANDO traer_oece AUNQUE YA TRAIGA DOS FUENTES

  La tarea del Programador de Windows apunta a esta ruta exacta
  (tools/instalar_puente.ps1). Renombrarlo dejaria la tarea instalada
  apuntando a un archivo que no existe, y eso se descubre cuando el panel
  lleva dias sin datos. El nombre miente un poco; una tarea rota miente
  entera.

POR QUE EXISTE ESTO

  OECE devuelve 403 a todo el trafico del VPS: sale por una IP de datacenter
  fuera de Peru. Desde una conexion peruana la misma API responde 200. Mientras
  se resuelve el enrutado del servidor -- Worker con Smart Placement, VPS
  peruano o que OECE habilite el acceso --, esta maquina hace de puente.

  Escribe en la MISMA base de Supabase que usa el servidor, asi que las
  convocatorias aparecen en el panel de los clientes igual que si las hubiera
  traido el bot.

Y LO MISMO PASA CON LAS COMPRAS MENORES DE MADRE DE DIOS

  El portal de cotizaciones del GORE de Madre de Dios tampoco responde al VPS,
  y ahi esta el unico dato de todo el sistema que SEACE no publica: las
  compras por debajo de 8 UIT. Comprobado el 30/08/2026 desde esta maquina:
  responde 200 y salen 25 convocatorias vigentes; desde el servidor, dos
  errores por pasada durante 21 pasadas seguidas.

  Por eso viaja en el mismo puente. No es un anadido oportunista: es
  exactamente el mismo bloqueo por origen y la misma solucion.

ES UN PUENTE, NO UNA SOLUCION

  Depende de que esta PC este encendida y con internet. Si se apaga, deja de
  entrar dato nuevo y nadie avisa. Sirve para no tener a los clientes con el
  panel vacio mientras se arregla bien; no para quedarse.

  Y de paso comprueba la hipotesis: si desde aqui entra sin problema durante
  dias, confirma que el bloqueo es por origen y no otra cosa.

USO
  .venv-tarea/Scripts/python.exe tools/traer_oece.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# EL .env HAY QUE CARGARLO POR RUTA ABSOLUTA, NO POR DESCUBRIMIENTO
#
#   `load_dotenv()` sin argumentos busca desde el directorio ACTUAL hacia
#   arriba. El Programador de tareas de Windows arranca en C:\Windows\System32,
#   donde no hay ningun .env, asi que DATABASE_URL llegaria vacia y el scraper
#   caeria al Postgres local de POSTGRES_* -- que en esta maquina existe. Es
#   decir: la tarea correria "bien", guardaria las licitaciones en la base de
#   desarrollo, y el panel de los clientes seguiria vacio sin un solo error.
#
#   Ejecutandolo a mano desde la carpeta del proyecto funciona igual, y por eso
#   este fallo solo aparece cuando ya nadie mira.
from dotenv import load_dotenv

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
log = logging.getLogger("puente.oece")

load_dotenv(RAIZ / ".env")

REGISTRO = RAIZ / "data" / "traer_oece.log"


def _preparar_log() -> None:
    REGISTRO.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        # Con pythonw.exe (que es como lo lanza la tarea, sin ventana negra)
        # sys.stdout es None, y un StreamHandler contra None revienta al
        # escribir. El archivo es el registro que importa; la consola solo
        # sirve cuando se ejecuta a mano.
        handlers=[h for h in (
            logging.FileHandler(REGISTRO, encoding="utf-8"),
            logging.StreamHandler(sys.stdout) if sys.stdout else None,
        ) if h],
    )


async def _principal() -> int:
    from radar_bot.scrapers.ocds_oece import _destino, scrape_ocds_oece

    url, _ = _destino()
    if "workers.dev" in url:
        # Desde esta maquina hay que ir DIRECTO. Si alguien copio las variables
        # del servidor al .env local, las peticiones darian la vuelta por
        # Cloudflare para acabar bloqueadas igual, y costaria entender por que.
        log.error(
            "OECE_PROXY_URL esta configurada en el .env local. Desde esta "
            "maquina hay que ir directo: vacia esa variable."
        )
        return 2

    nuevas = await scrape_ocds_oece(max_paginas=40, dias_atras=7)
    log.info("OECE: guardadas %d licitaciones nuevas.", len(nuevas))

    # SI GORE FALLA, LA TAREA NO SE PONE EN ROJO. A PROPOSITO.
    #
    #   OECE es el producto: si no entra, el panel de todos los clientes se
    #   queda con lo viejo y la tarea TIENE que verse en rojo. Madre de Dios es
    #   una fuente secundaria; ponerla al mismo nivel haria que el rojo saltara
    #   por averias de distinta gravedad, y un indicador que significa dos
    #   cosas distintas acaba sin significar ninguna.
    #
    #   El fallo no se pierde: el propio scraper lo deja en `scraping_log` con
    #   el motivo (ver la Sonda del orquestador) y sale en el parte al
    #   administrador.
    try:
        from radar_bot.scrapers.orchestrator import _run_gore_portals
        gore = await _run_gore_portals(0)
        log.info("GORE cotizaciones: guardadas %d nuevas.", len(gore))
    except Exception as exc:
        log.error("GORE cotizaciones fallo (OECE si entro): %s", exc)

    return 0


def main() -> int:
    _preparar_log()
    log.info("--- inicio %s ---", fechas.ahora().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        codigo = asyncio.run(_principal())
    except Exception as exc:
        # Se registra y se devuelve fallo para que el Programador de tareas lo
        # marque en rojo. Terminar en 0 tras un error es como esta fuente
        # estuvo doce corridas caida sin que nadie se enterara.
        log.exception("La pasada fallo: %s", exc)
        return 1
    log.info("--- fin (codigo %d) ---", codigo)
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
