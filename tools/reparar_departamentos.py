"""Repara `licitaciones.departamento` de una vez, tras corregir el detector.

QUE ARREGLA

  1. Las filas de OECE etiquetadas por SUBCADENA. `ocds_oece._departamento`
     hacia `"ica" in texto` y etiquetaba como Ica cualquier compra con
     "publica", "tecnica", "electrica" o "medica" en el objeto. Se recalcula
     el departamento de TODAS las filas de ocds_oece con el detector
     corregido, a partir de entidad + objeto + nomenclatura, y se escribe
     donde cambie. Donde el detector nuevo no encuentre nada queda NULL: es
     preferible a dejar un Ica inventado, y el paso 2 recupera muchas.

  2. Las filas sin departamento cuyo RUC lo delata
     (shared/departamentos.completar_por_ruc). Va despues del paso 1 a
     proposito: con los Ica falsos todavia dentro, el RUC del Gobierno
     Regional de Puno repartia sus filas entre Puno e Ica y no pasaba el corte
     de claridad.

USO (en el servidor, dentro del contenedor web o radar)

  docker exec -w /app licitapro-web-1 python tools/reparar_departamentos.py
  docker exec -w /app licitapro-web-1 python tools/reparar_departamentos.py --aplicar

  Sin --aplicar solo cuenta y ensena una muestra. Correrlo dos veces no rompe
  nada: la segunda no encuentra que cambiar.
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter

# Se lanza desde la raiz del proyecto (`python tools/...`), que no entra sola
# en sys.path. Mismo arreglo que culqi_planes.py y renovar_suscripciones.py.
sys.path.insert(0, os.getcwd())

from radar_bot.scrapers.ocds_oece import _departamento
from radar_bot.scrapers.orchestrator import _detectar_depto
from shared.db import connection
from shared.departamentos import completar_por_ruc


async def _recalcular(aplicar: bool) -> tuple[int, Counter]:
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT id, fuente, entidad, objeto, nomenclatura, departamento
                 FROM licitaciones
                WHERE fuente = 'ocds_oece'
                   OR (fuente = 'gob_pe' AND departamento IS NULL)""")
    cambios: list[tuple[str, str | None]] = []
    resumen: Counter = Counter()
    for f in filas:
        if f["fuente"] == "ocds_oece":
            texto = " ".join(x for x in (f["entidad"], f["objeto"], f["nomenclatura"]) if x)
            nuevo = _departamento(texto, f["entidad"])
        else:
            # gob_pe usa el detector del orquestador, igual que al scrapear.
            nuevo = _detectar_depto(f"{f['entidad']} {f['objeto']}")
        # NUNCA SE REGRESA A NULL
        #
        #   La fila puede tener el departamento por dos caminos que este
        #   script no ve: el texto de los items de OCDS (que no se guarda) y
        #   `completar_por_ruc`. Si el detector no encuentra nada en lo que si
        #   se guarda, eso no demuestra que el valor este mal; solo que aqui
        #   falta contexto. La primera pasada (2026-10-07) si borro a NULL a
        #   proposito -- eran los 672 "Ica" falsos --, pero eso ya paso y no
        #   se repite: en una segunda corrida, "Arequipa -> None" eran 34
        #   filas completadas por RUC que se habrian deshecho.
        if nuevo is None or nuevo == f["departamento"]:
            continue
        cambios.append((f["id"], nuevo))
        resumen[f"{f['departamento']} -> {nuevo}"] += 1
    if aplicar and cambios:
        async with connection() as conn:
            await conn.executemany(
                "UPDATE licitaciones SET departamento = $2, updated_at = NOW() WHERE id = $1",
                cambios)
    return len(cambios), resumen


async def _sin_departamento() -> int:
    async with connection() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM licitaciones WHERE departamento IS NULL")


async def main(aplicar: bool) -> None:
    antes = await _sin_departamento()
    n, resumen = await _recalcular(aplicar)
    print(f"Paso 1 - detector corregido: {n} filas de ocds_oece cambian de departamento")
    for cambio, k in resumen.most_common(12):
        print(f"    {k:>5}  {cambio}")
    if aplicar:
        tocadas = await completar_por_ruc()
        print(f"Paso 2 - por RUC: {tocadas} filas completadas")
        print(f"Sin departamento: {antes} antes -> {await _sin_departamento()} despues")
    else:
        print(f"Sin departamento ahora: {antes}. Nada escrito: anade --aplicar.")


if __name__ == "__main__":
    asyncio.run(main("--aplicar" in sys.argv))
