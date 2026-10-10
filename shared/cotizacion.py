"""Cotizacion por items: la tabla que la entidad manda y espera de vuelta.

QUE ES UNA SOLICITUD DE COTIZACION

  En una contratacion menor (8 UIT o menos) la entidad no publica bases: manda
  una tabla con cantidad, unidad y descripcion de cada bien, y deja en blanco
  MARCA, PRECIO UNITARIO y PRECIO TOTAL. El proveedor la devuelve llena,
  firmada, con su RUC y su domicilio. Eso es todo lo que se evalua.

  Hasta 2026-10-10 la propuesta solo tenia `precio_ofertado`, un monto suelto.
  La prueba con la SC 5884-2026-GOREMAD (nueve alimentos, cada uno con su
  marca y su precio) no tenia donde ponerse.

DE DONDE SALEN LOS ITEMS

  De `licitaciones.descripcion`, que el scraper de gore_portals llena con una
  linea por item ("ARROZ EXTRA (399 KLG)"). Para las fuentes que no publican
  items, el usuario los agrega a mano desde la ficha de la propuesta.

`precio_ofertado` SIGUE SIENDO EL TOTAL

  Al guardar precios se recalcula como la suma de los items. Todo lo que ya
  lo leia -- la ficha, el validador, el ZIP -- sigue viendo el total sin
  enterarse de que ahora se calcula.
"""
from __future__ import annotations

import json
import logging
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from shared.db import connection

log = logging.getLogger("shared.cotizacion")

# Contratacion menor: "cotizacion" (portales de GORE y gob.pe) y "CM" (las
# contrataciones menores de OECE). En ellas no se pide partida registral ni
# vigencia de poder, ni equipo tecnico ni experiencia: se pide la cotizacion
# firmada, la declaracion jurada y el pacto de integridad.
TIPOS_MENORES = {"cotizacion", "CM"}

CONDICIONES_INICIALES = {
    "plazo_entrega": "",
    "validez": "30 días calendario",
    "garantia": "",
    "forma_pago": "Transferencia a cuenta interbancaria (CCI)",
}

# "ARROZ EXTRA (399 KLG)", "ALQUILER DE LOCAL (1 SERVICIO) [SEGUN TDR]".
# El nombre es perezoso y el resto va anclado al final, asi que un nombre con
# sus propios parentesis ("PAPEL BOND (A4) (10 MILLAR)") se corta en el
# ultimo grupo que si es una cantidad.
_LINEA = re.compile(
    r"^(?P<desc>.+?) \((?P<cant>\d+(?:\.\d+)?)(?: (?P<und>[^()\[\]]+))?\)"
    r"(?: \[(?P<carac>.*)\])?$")

CENTIMO = Decimal("0.01")


def es_cotizacion_menor(tipo: str | None) -> bool:
    return (tipo or "") in TIPOS_MENORES


def items_de_texto(texto: str | None) -> list[dict]:
    """Las lineas de `licitaciones.descripcion` convertidas en items.

    Una linea sin cantidad no se adivina: se descarta y el usuario la agrega a
    mano. Inventar "1 unidad" pondria un total equivocado con aspecto de bueno.
    """
    items = []
    for linea in (texto or "").splitlines():
        m = _LINEA.match(linea.strip())
        if not m:
            continue
        items.append({
            "descripcion": m["desc"].strip(),
            "cantidad": Decimal(m["cant"]),
            "unidad": (m["und"] or "").strip() or None,
        })
    return items


def a_decimal(valor) -> Decimal | None:
    """Lo que llega de un formulario: "4", "4.00", "4,50", "" o basura."""
    if valor is None:
        return None
    crudo = str(valor).strip().replace(",", ".")
    if not crudo:
        return None
    try:
        d = Decimal(crudo)
    except InvalidOperation:
        return None
    return d if d.is_finite() and d >= 0 else None


def subtotal(item) -> Decimal | None:
    if item["precio_unitario"] is None:
        return None
    return (Decimal(item["cantidad"]) * Decimal(item["precio_unitario"])).quantize(
        CENTIMO, rounding=ROUND_HALF_UP)


def total(items) -> Decimal:
    return sum((subtotal(i) or Decimal(0) for i in items), Decimal(0))


async def items_de(propuesta_id: int) -> list[dict]:
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT id, orden, descripcion, cantidad, unidad, marca, precio_unitario
                 FROM propuesta_items WHERE propuesta_id=$1 ORDER BY orden""",
            propuesta_id)
    salida = []
    for f in filas:
        d = dict(f)
        d["subtotal"] = subtotal(d)
        salida.append(d)
    return salida


async def condiciones_de(propuesta_id: int) -> dict:
    async with connection() as conn:
        crudo = await conn.fetchval(
            "SELECT cotizacion_condiciones FROM propuestas WHERE id=$1", propuesta_id)
    if isinstance(crudo, str):
        crudo = json.loads(crudo)
    return {**CONDICIONES_INICIALES, **(crudo or {})}


async def sembrar(propuesta_id: int) -> int:
    """Copia los items de la licitacion a la propuesta, UNA sola vez.

    La marca de "ya se sembro" es `cotizacion_condiciones` no nulo. Sin ella,
    alguien que borra todos los items para cargarlos a su manera los veria
    reaparecer al volver a abrir la ficha.
    """
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT p.cotizacion_condiciones, l.descripcion, l.tipo
                 FROM propuestas p LEFT JOIN licitaciones l ON l.id = p.licitacion_id
                WHERE p.id=$1""", propuesta_id)
        if not fila or fila["cotizacion_condiciones"] is not None:
            return 0
        items = items_de_texto(fila["descripcion"])
        if not items and not es_cotizacion_menor(fila["tipo"]):
            # Ni items ni cotizacion: una licitacion grande sigue con su monto
            # unico, y queda sin marcar por si mas adelante aparecen items.
            return 0
        async with conn.transaction():
            for orden, it in enumerate(items, 1):
                await conn.execute(
                    """INSERT INTO propuesta_items
                       (propuesta_id, orden, descripcion, cantidad, unidad)
                       VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING""",
                    propuesta_id, orden, it["descripcion"], it["cantidad"], it["unidad"])
            await conn.execute(
                "UPDATE propuestas SET cotizacion_condiciones=$2::jsonb WHERE id=$1",
                propuesta_id, json.dumps(CONDICIONES_INICIALES))
    return len(items)


async def _recalcular(conn, propuesta_id: int) -> Decimal:
    filas = await conn.fetch(
        "SELECT cantidad, precio_unitario FROM propuesta_items WHERE propuesta_id=$1",
        propuesta_id)
    suma = total(filas)
    # Sin ningun precio puesto no se pisa un monto que el usuario fijo a mano.
    if any(f["precio_unitario"] is not None for f in filas):
        await conn.execute(
            "UPDATE propuestas SET precio_ofertado=$2, updated_at=NOW() WHERE id=$1",
            propuesta_id, float(suma))
    return suma


async def guardar(propuesta_id: int, cambios: dict[int, tuple[str, Decimal | None]],
                  condiciones: dict) -> Decimal:
    """Marca y precio de cada item, y las condiciones del pie. Devuelve el total.

    `cambios` va por id de item, y cada UPDATE filtra ademas por propuesta: el
    id llega de un formulario y no puede tocar items de otra propuesta.
    """
    limpias = {k: (condiciones.get(k) or "").strip()[:300] for k in CONDICIONES_INICIALES}
    async with connection() as conn:
        async with conn.transaction():
            for item_id, (marca, precio) in cambios.items():
                await conn.execute(
                    """UPDATE propuesta_items SET marca=$3, precio_unitario=$4, updated_at=NOW()
                        WHERE id=$1 AND propuesta_id=$2""",
                    item_id, propuesta_id, (marca or "").strip()[:120] or None, precio)
            await conn.execute(
                "UPDATE propuestas SET cotizacion_condiciones=$2::jsonb WHERE id=$1",
                propuesta_id, json.dumps(limpias))
            return await _recalcular(conn, propuesta_id)


async def agregar(propuesta_id: int, descripcion: str, cantidad: Decimal,
                  unidad: str | None) -> None:
    async with connection() as conn:
        async with conn.transaction():
            orden = await conn.fetchval(
                "SELECT COALESCE(MAX(orden), 0) + 1 FROM propuesta_items WHERE propuesta_id=$1",
                propuesta_id)
            await conn.execute(
                """INSERT INTO propuesta_items (propuesta_id, orden, descripcion, cantidad, unidad)
                   VALUES ($1,$2,$3,$4,$5)""",
                propuesta_id, orden, descripcion.strip()[:500], cantidad,
                (unidad or "").strip()[:30] or None)
            await conn.execute(
                """UPDATE propuestas SET cotizacion_condiciones =
                       COALESCE(cotizacion_condiciones, $2::jsonb) WHERE id=$1""",
                propuesta_id, json.dumps(CONDICIONES_INICIALES))


async def quitar(propuesta_id: int, item_id: int) -> None:
    async with connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM propuesta_items WHERE id=$1 AND propuesta_id=$2",
                item_id, propuesta_id)
            await _recalcular(conn, propuesta_id)


def monto(valor) -> str:
    """12115.2 -> '12,115.20', como se escribe en una cotizacion peruana."""
    return f"{Decimal(valor).quantize(CENTIMO, rounding=ROUND_HALF_UP):,.2f}"


def precio_legible(valor) -> str:
    """Dos decimales siempre, y mas solo si el precio los tiene: 5.2 -> '5.20',
    0.125 -> '0.125'. Redondear un precio unitario a centimos al imprimirlo
    haria que la tabla no cuadre con el total, que se calcula sin redondear."""
    d = Decimal(valor).normalize()
    return f"{d:,.2f}" if -d.as_tuple().exponent <= 2 else f"{d:,f}"


def cantidad_legible(valor) -> str:
    """Decimal('54.000') -> '54', Decimal('2.500') -> '2.5'."""
    return f"{Decimal(valor).normalize():f}"
