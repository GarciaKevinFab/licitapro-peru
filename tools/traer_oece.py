"""Trae convocatorias de OECE desde ESTA maquina y las guarda en produccion.

POR QUE EXISTE ESTO

  OECE devuelve 403 a todo el trafico del VPS: sale por una IP de datacenter
  fuera de Peru. Desde una conexion peruana la misma API responde 200. Mientras
  se resuelve el enrutado del servidor -- Worker con Smart Placement, VPS
  peruano o que OECE habilite el acceso --, esta maquina hace de puente.

  Escribe en la MISMA base de Supabase que usa el servidor, asi que las
  convocatorias aparecen en el panel de los clientes igual que si las hubiera
  traido el bot.

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
from datetime import datetime
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
from dotenv import load_dotenv  # noqa: E402
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
    from radar_bot.scrapers.ocds_oece import scrape_ocds_oece, _destino

    url, _ = _destino()
    if "workers.dev" in url:
        # Desde esta maquina hay que ir DIRECTO. Si alguien copio las variables
        # del servidor al .env local, las peticiones darian la vuelta por
        # Cloudflare para acabar bloqueadas igual, y costaria entender por que.
        logging.error(
            "OECE_PROXY_URL esta configurada en el .env local. Desde esta "
            "maquina hay que ir directo: vacia esa variable."
        )
        return 2

    nuevas = await scrape_ocds_oece(max_paginas=40, dias_atras=7)
    logging.info("Guardadas %d licitaciones nuevas.", len(nuevas))
    return 0


def main() -> int:
    _preparar_log()
    logging.info("--- inicio %s ---", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        codigo = asyncio.run(_principal())
    except Exception as exc:  # noqa: BLE001
        # Se registra y se devuelve fallo para que el Programador de tareas lo
        # marque en rojo. Terminar en 0 tras un error es como esta fuente
        # estuvo doce corridas caida sin que nadie se enterara.
        logging.exception("La pasada fallo: %s", exc)
        return 1
    logging.info("--- fin (codigo %d) ---", codigo)
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
