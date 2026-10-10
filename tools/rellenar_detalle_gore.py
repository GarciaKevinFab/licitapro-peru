"""Rellena `descripcion` de las cotizaciones de GOREMAD guardadas sin items.

QUE ARREGLA

  Hasta el 2026-10-09 el scraper de gore_portals guardaba solo el concepto del
  listado. Los items (sal, atun, arroz...) estan en la ficha /solicitud/<id> y
  no se leian, asi que quien vigilaba "aceite" no veia una SC que pedia 54
  litros bajo el concepto "ADQUISICION DE ALIMENTOS NO PERECIBLE". Desde el
  cambio el scraper los lee en cada pasada; esto pone al dia las filas viejas.

  Solo escribe donde `descripcion` es NULL: correrlo dos veces no rehace nada
  y no pisa lo que el scraper ya guardo.

DONDE SE CORRE

  Desde una conexion PERUANA (la PC puente o la de desarrollo): el portal no
  contesta al VPS. Con DATABASE_URL apuntando a la base de produccion.

    python tools/rellenar_detalle_gore.py                     # cuenta y muestra
    python tools/rellenar_detalle_gore.py --aplicar
    python tools/rellenar_detalle_gore.py --aplicar --solo-vigentes

  Va de a una ficha por segundo: 775 filas son unos 13 minutos. Es el portal
  de una entidad regional; no se le pide mas rapido.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Se lanza desde la raiz del proyecto (`python tools/...`), que no entra sola
# en sys.path. Mismo arreglo que reparar_departamentos.py.
sys.path.insert(0, os.getcwd())

import httpx  # noqa: E402

from radar_bot.scrapers.orchestrator import HEADERS, _leer_detalle  # noqa: E402
from shared.db import connection  # noqa: E402


async def _rellenar(aplicar: bool, solo_vigentes: bool, limite: int | None) -> None:
    vigencia = ("AND fecha_cierre > (NOW() AT TIME ZONE 'America/Lima')"
                if solo_vigentes else "")
    async with connection() as conn:
        filas = await conn.fetch(
            f"""SELECT id, nomenclatura, url FROM licitaciones
                 WHERE fuente = 'gore_portals' AND descripcion IS NULL
                   AND url LIKE '%/solicitud/%' {vigencia}
                 ORDER BY fecha_cierre DESC NULLS LAST""")
    if limite:
        filas = filas[:limite]
    print(f"{len(filas)} filas sin detalle")

    escritas = vacias = 0
    async with httpx.AsyncClient(timeout=30, headers=HEADERS,
                                 follow_redirects=True, verify=False) as client:
        for i, f in enumerate(filas, 1):
            detalle = await _leer_detalle(client, f["url"])
            if not detalle:
                vacias += 1
            else:
                if i <= 3 or not aplicar:
                    print(f"  {f['nomenclatura']}: {detalle.splitlines()[0][:90]}"
                          f" (+{len(detalle.splitlines()) - 1})")
                if aplicar:
                    async with connection() as conn:
                        await conn.execute(
                            """UPDATE licitaciones SET descripcion = $2, updated_at = NOW()
                                WHERE id = $1 AND descripcion IS NULL""",
                            f["id"], detalle)
                escritas += 1
            if i % 50 == 0:
                print(f"  ... {i}/{len(filas)}")
            await asyncio.sleep(1)

    verbo = "escritas" if aplicar else "por escribir (sin --aplicar)"
    print(f"{escritas} {verbo}; {vacias} fichas sin items o que no cargaron")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--aplicar", action="store_true")
    p.add_argument("--solo-vigentes", action="store_true")
    p.add_argument("--limite", type=int)
    a = p.parse_args()
    asyncio.run(_rellenar(a.aplicar, a.solo_vigentes, a.limite))


if __name__ == "__main__":
    main()
