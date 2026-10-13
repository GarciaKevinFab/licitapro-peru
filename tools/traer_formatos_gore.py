"""Baja la SC y la DJ de las cotizaciones de GOREMAD que aun estan abiertas.

QUE ARREGLA

  Desde la migracion 0017 el scraper de gore_portals guarda en cada pasada los
  documentos de la ficha (licitacion_documentos), y la web llena la SC y la DJ
  de la entidad sin que el usuario suba nada. Las solicitudes cosechadas antes
  no los tienen: esto los trae. Solo toca las que siguen abiertas -- para una
  cerrada ya no se cotiza -- y salta las que ya tienen su SC guardada.

DONDE SE CORRE

  Desde una conexion PERUANA (la PC puente o la de desarrollo): el portal no
  contesta al VPS. Con DATABASE_URL apuntando a la base de produccion.

    python tools/traer_formatos_gore.py              # cuenta cuantas faltan
    python tools/traer_formatos_gore.py --aplicar

  Una ficha por segundo, como rellenar_detalle_gore.py.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

import httpx

from radar_bot.scrapers.orchestrator import HEADERS, _guardar_documentos, _leer_detalle
from shared.db import connection


async def _traer(aplicar: bool, limite: int | None) -> None:
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT l.id, l.nomenclatura, l.url FROM licitaciones l
                WHERE l.fuente = 'gore_portals' AND l.url LIKE '%/solicitud/%'
                  AND l.fecha_cierre > (NOW() AT TIME ZONE 'America/Lima')
                  AND NOT EXISTS (SELECT 1 FROM licitacion_documentos d
                                   WHERE d.licitacion_id = l.id AND d.tipo = 'sc'
                                     AND d.contenido IS NOT NULL)
                ORDER BY l.fecha_cierre""")
    if limite:
        filas = filas[:limite]
    print(f"{len(filas)} solicitudes abiertas sin su SC guardada")
    if not aplicar:
        for f in filas[:10]:
            print(f"  {f['nomenclatura']}")
        print("(sin --aplicar no se baja nada)")
        return

    con_sc = 0
    async with httpx.AsyncClient(timeout=40, headers=HEADERS,
                                 follow_redirects=True, verify=False) as client:
        for i, f in enumerate(filas, 1):
            docs: list[dict] = []
            await _leer_detalle(client, f["url"], docs)
            await _guardar_documentos(client, f["id"], docs)
            tipos = sorted({d["tipo"] for d in docs})
            con_sc += "sc" in tipos
            print(f"  {f['nomenclatura']}: {', '.join(tipos) or 'sin documentos'}")
            if i % 25 == 0:
                print(f"  ... {i}/{len(filas)}")
            await asyncio.sleep(1)
    print(f"{con_sc} de {len(filas)} con su SC publicada")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--aplicar", action="store_true")
    p.add_argument("--limite", type=int)
    a = p.parse_args()
    asyncio.run(_traer(a.aplicar, a.limite))


if __name__ == "__main__":
    main()
