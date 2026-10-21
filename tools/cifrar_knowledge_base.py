"""Cifra el CCI, la cuenta corriente y el DNI que ya estaban en claro en la KB.

POR QUE HACE FALTA, SI shared/db.py YA CIFRA AL GUARDAR

  `kb_set` cifra lo que se guarda desde que existe CAMPOS_KB_CIFRADOS, y
  `kb_get` lee igual lo viejo en claro que lo nuevo cifrado. Pero lo que ya
  estaba se queda en claro hasta que alguien lo vuelva a escribir, que puede
  ser nunca: el bot no vuelve a preguntar lo que ya sabe. Este script lo cifra
  de una vez.

  Y de paso tapa la otra copia: `preguntas.respuesta` guardaba la respuesta
  tal cual, CCI incluido. Ahi se deja solo el final (`kb_para_registro`), que
  es lo que ya apunta el codigo nuevo.

USO (en el servidor, dentro del contenedor web: es el que lleva
LICITAPRO_ENTORNO=produccion y por tanto se niega a cifrar sin la clave real)

  docker exec -w /app licitapro-web-1 python tools/cifrar_knowledge_base.py --dry-run
  docker exec -w /app licitapro-web-1 python tools/cifrar_knowledge_base.py

  Idempotente: lo cifrado lleva el prefijo y no se vuelve a tocar, asi que una
  segunda pasada dice "0" y no escribe nada.

ANTES DE CORRERLO SIN --dry-run: un respaldo reciente (tools/respaldar.sh) y la
LICITAPRO_SECRET_KEY guardada fuera del servidor. Sin esa clave, lo cifrado no
se recupera nunca: perderla es perder los CCI de todos los clientes.
"""
from __future__ import annotations

import asyncio
import os
import sys

# Se lanza desde la raiz del proyecto (`python tools/...`), que no entra sola
# en sys.path. Mismo arreglo que reparar_departamentos.py.
sys.path.insert(0, os.getcwd())

from shared.db import (
    CAMPOS_KB_CIFRADOS,
    PREFIJO_CIFRADO,
    connection,
    kb_cifrar_valor,
    kb_descifrar_valor,
    kb_para_registro,
)


async def cifrar_existentes(conn, aplicar: bool) -> tuple[int, int]:
    """(filas de la KB cifradas, respuestas de preguntas tapadas).

    Todo en una transaccion: o queda todo cifrado o nada. Cada valor se
    comprueba descifrandolo antes de escribirlo; si no vuelve igual se aborta
    sin tocar nada, que es mejor que guardar un CCI que luego no se abre.
    """
    pares = sorted(CAMPOS_KB_CIFRADOS)
    categorias = [c for c, _ in pares]
    claves = [k for _, k in pares]
    async with conn.transaction():
        filas = await conn.fetch(
            """SELECT kb.id, kb.categoria, kb.clave, kb.valor
                 FROM knowledge_base kb
                 JOIN unnest($1::text[], $2::text[]) AS c(categoria, clave)
                   ON c.categoria = kb.categoria AND c.clave = kb.clave
                WHERE kb.valor IS NOT NULL AND kb.valor NOT LIKE $3
                FOR UPDATE OF kb""",
            categorias, claves, PREFIJO_CIFRADO + "%")
        cambios = []
        for f in filas:
            cifrado = kb_cifrar_valor(f["categoria"], f["clave"], f["valor"])
            if kb_descifrar_valor(cifrado) != f["valor"]:
                raise RuntimeError(
                    f"knowledge_base id={f['id']}: el valor cifrado no se descifra "
                    "igual. No se escribe nada.")
            cambios.append((f["id"], cifrado))

        preguntas = await conn.fetch(
            """SELECT p.id, p.kb_categoria, p.kb_clave, p.respuesta
                 FROM preguntas p
                 JOIN unnest($1::text[], $2::text[]) AS c(categoria, clave)
                   ON c.categoria = p.kb_categoria AND c.clave = p.kb_clave
                WHERE p.respuesta IS NOT NULL AND p.respuesta NOT LIKE '****%'
                FOR UPDATE OF p""",
            categorias, claves)
        tapadas = [(p["id"], kb_para_registro(p["kb_categoria"], p["kb_clave"],
                                              p["respuesta"]))
                   for p in preguntas]

        if aplicar:
            # Sin tocar updated_at: el dato no cambio, solo como se guarda.
            if cambios:
                await conn.executemany(
                    "UPDATE knowledge_base SET valor = $2 WHERE id = $1", cambios)
            if tapadas:
                await conn.executemany(
                    "UPDATE preguntas SET respuesta = $2 WHERE id = $1", tapadas)
    return len(cambios), len(tapadas)


async def main(aplicar: bool) -> None:
    async with connection() as conn:
        n_kb, n_preg = await cifrar_existentes(conn, aplicar)
        quedan = await conn.fetchval(
            """SELECT COUNT(*) FROM knowledge_base kb
                JOIN unnest($1::text[], $2::text[]) AS c(categoria, clave)
                  ON c.categoria = kb.categoria AND c.clave = kb.clave
               WHERE kb.valor IS NOT NULL AND kb.valor NOT LIKE $3""",
            [c for c, _ in sorted(CAMPOS_KB_CIFRADOS)],
            [k for _, k in sorted(CAMPOS_KB_CIFRADOS)], PREFIJO_CIFRADO + "%")
    campos = ", ".join(f"{c}/{k}" for c, k in sorted(CAMPOS_KB_CIFRADOS))
    print(f"Campos cifrados: {campos}")
    print(f"knowledge_base: {n_kb} valores en claro {'cifrados' if aplicar else 'por cifrar'}")
    print(f"preguntas: {n_preg} respuestas {'tapadas' if aplicar else 'por tapar'}")
    print(f"Quedan en claro: {quedan}")
    if not aplicar:
        print("Nada escrito (--dry-run).")


if __name__ == "__main__":
    asyncio.run(main("--dry-run" not in sys.argv))
