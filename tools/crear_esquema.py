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

import psycopg2

RUTA_ESQUEMA = "/app/shared/schema.sql"


def main() -> int:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        print("DATABASE_URL sin definir: no hay base donde crear nada.", file=sys.stderr)
        return 1

    # Supabase solo acepta TLS; sin esto la conexion se rechaza con un mensaje
    # que no apunta a la causa.
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"

    with open(RUTA_ESQUEMA, encoding="utf-8") as f:
        sql = f.read()

    con = psycopg2.connect(url)
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
