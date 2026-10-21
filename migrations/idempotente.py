"""Operaciones de Alembic que no fallan si lo que crean ya existe.

POR QUE

  Las migraciones 0002-0008 nacieron con `op.create_table`, `add_column`,
  `create_index`... a pelo. Sobre una base que ya tiene esas tablas o columnas
  -- creada con un shared/schema.sql que las incluye, restaurada de un volcado
  o con `alembic_version` atrasada respecto al esquema real -- revientan con
  "relation already exists" y frenan el stack entero (el servicio
  `migraciones` es dependencia de `web`).

  Estas funciones comprueban si el objeto existe (por su nombre, en el esquema
  de la conexion) y solo lo crean si no esta. Lo que crean es EXACTAMENTE lo
  mismo que la llamada de `op` que envuelven: mismos nombres, columnas y
  opciones. Las revisiones no cambian.

  Se consulta el catalogo en cada llamada y no se usa un Inspector guardado:
  el Inspector cachea, y una tabla creada dos lineas antes no saldria.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


def _hay(sql: str, **parametros) -> bool:
    return op.get_bind().execute(sa.text(sql), parametros).first() is not None


def hay_tabla(tabla: str) -> bool:
    return _hay("SELECT 1 FROM pg_class WHERE oid = to_regclass(:t) AND relkind IN ('r', 'p')",
                t=tabla)


def hay_columna(tabla: str, columna: str) -> bool:
    return _hay("""SELECT 1 FROM pg_attribute
                    WHERE attrelid = to_regclass(:t) AND attname = :c
                      AND attnum > 0 AND NOT attisdropped""", t=tabla, c=columna)


def hay_indice(nombre: str) -> bool:
    return _hay("SELECT 1 FROM pg_class WHERE oid = to_regclass(:n) AND relkind IN ('i', 'I')",
                n=nombre)


def hay_restriccion(tabla: str, nombre: str) -> bool:
    return _hay("""SELECT 1 FROM pg_constraint
                    WHERE conrelid = to_regclass(:t) AND conname = :n""",
                t=tabla, n=nombre)


def create_table(nombre: str, *columnas, **opciones) -> None:
    if not hay_tabla(nombre):
        op.create_table(nombre, *columnas, **opciones)


def add_column(tabla: str, columna: sa.Column) -> None:
    if not hay_columna(tabla, columna.name):
        op.add_column(tabla, columna)


def create_index(nombre: str, tabla: str, columnas, **opciones) -> None:
    if not hay_indice(nombre):
        op.create_index(nombre, tabla, columnas, **opciones)


def create_unique_constraint(nombre: str, tabla: str, columnas, **opciones) -> None:
    if not hay_restriccion(tabla, nombre):
        op.create_unique_constraint(nombre, tabla, columnas, **opciones)


def create_foreign_key(nombre: str, origen: str, referida: str, columnas_origen,
                       columnas_referidas, **opciones) -> None:
    if not hay_restriccion(origen, nombre):
        op.create_foreign_key(nombre, origen, referida, columnas_origen,
                              columnas_referidas, **opciones)
