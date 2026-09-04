"""Crea el esquema base en una base de datos vacia. Se corre UNA sola vez.

POR QUE HACE FALTA UN PASO APARTE

  `migrations/versions/0001_baseline.py` no crea nada a proposito: nacio sobre
  una base que ya estaba montada a mano con shared/schema.sql, y solo marca el
  punto de partida. Eso funciona mientras la base exista.

  En una base nueva -- un Supabase recien creado -- no existe, y entonces la
  0002 intenta `ALTER TABLE empresas` sobre una tabla que nadie creo:

    sqlalchemy.exc.ProgrammingError: relation "empresas" does not exist

  El mensaje no menciona schema.sql por ningun lado, y como `migraciones` es
  dependencia de `web`, el compose frena el stack entero. Se parece a una
  migracion rota y es un paso de instalacion que faltaba.

  Si algun dia la 0001 pasa a crear el esquema ella misma, este script sobra.

USO

  docker compose -f docker-compose.prod.yml run --rm -T \
      --entrypoint python migraciones tools/crear_esquema.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2

# Todo lo de aqui se calcula desde __file__ y no desde el directorio actual.
#
#   `sys.path.insert(0, os.getcwd())`, que es lo que hacen otros scripts de
#   esta carpeta, solo funciona si quien lanza el script esta parado en la raiz
#   del proyecto. Ejecutar `python tools/crear_esquema.py` deja sys.path[0]
#   apuntando a tools/, y el import de shared falla.
#
#   Y la ruta del esquema estaba fija en /app/shared/schema.sql: dentro de la
#   imagen resuelve igual que esto, pero fuera de ella no existe. Por eso este
#   script no se podia usar en el CI, que es exactamente donde hacia falta.
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

RUTA_ESQUEMA = RAIZ / "shared" / "schema.sql"

from shared.db import _es_gestionado


def _conectar():
    """Con DATABASE_URL si la hay; si no, con las piezas POSTGRES_*.

    Exigir DATABASE_URL dejaba fuera los dos entornos que no la tienen: el
    desarrollo local y el CI, donde la base es un contenedor del runner y la
    conexion viene en piezas sueltas. Es la misma regla que sigue get_pool.

    Y el TLS va SOLO contra bases gestionadas. Supabase no acepta otra cosa,
    pero un Postgres local no habla TLS: forzar sslmode=require alli rechaza
    la conexion con un mensaje que tampoco apunta a la causa.
    """
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        if _es_gestionado(urlparse(url).hostname or "") and "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return psycopg2.connect(url)

    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5433")),
        dbname=os.getenv("POSTGRES_DB", "licitapro"),
        user=os.getenv("POSTGRES_USER", "licitapro"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
    )


def main() -> int:
    sql = RUTA_ESQUEMA.read_text(encoding="utf-8")
    con = _conectar()
    # DDL en autocommit: si una sentencia falla, lo hecho hasta ahi queda, y el
    # script es idempotente en la practica porque schema.sql usa IF NOT EXISTS.
    con.autocommit = True
    try:
        with con.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "select count(*) from information_schema.tables "
                "where table_schema = 'public'"
            )
            print(f"esquema base creado. Tablas en public: {cur.fetchone()[0]}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
