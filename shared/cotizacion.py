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

# Lo que cabe en las columnas: precio_unitario es NUMERIC(14,4) y cantidad
# NUMERIC(14,3), o sea 10 y 11 digitos enteros. Se usa el limite mas estrecho
# para los dos: ningun precio ni cantidad de una cotizacion real se acerca a
# diez mil millones.
#
#   POR QUE IMPORTA: "1E+30" es un Decimal valido y finito. Pasaba el filtro,
#   llegaba al UPDATE y Postgres respondia "numeric field overflow": un 500
#   en vez de "el precio no es un numero valido".
TOPE = Decimal("1E10")
_DIEZMILESIMA = Decimal("0.0001")


class FueraDeRango(ValueError):
    """Un precio o una cantidad que no cabe en la columna, o no es un numero."""


def _cabe(d: Decimal) -> bool:
    """Si `d` entra en NUMERIC(14,4) una vez redondeado como lo redondea Postgres.

    Se mira DESPUES de redondear a 4 decimales: 9999999999.99999 es menor que el
    tope, pero Postgres lo guarda como 10000000000.0000 y desborda igual.
    """
    if not d.is_finite():
        return False
    try:
        return abs(d.quantize(_DIEZMILESIMA, rounding=ROUND_HALF_UP)) < TOPE
    except InvalidOperation:   # mas digitos de los que el contexto puede redondear
        return False


def validar_rango(valor, nombre: str = "valor") -> None:
    """FueraDeRango si `valor` no se puede guardar. None pasa (columna nulable).

    `a_decimal` ya descarta todo esto para lo que llega de un formulario; esto
    protege a quien llame a `agregar`/`guardar` con un Decimal hecho a mano, que
    sin ello se encontraria el error de Postgres a mitad de transaccion.
    """
    if valor is None:
        return
    try:
        d = Decimal(valor)
    except (InvalidOperation, TypeError, ValueError):
        raise FueraDeRango(f"{nombre} no es un numero: {valor!r}") from None
    if not _cabe(d):
        raise FueraDeRango(f"{nombre} fuera de rango: {valor}")


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
    """Lo que llega de un formulario: "4", "4.00", "4,50", "" o basura.

    None tambien para lo que no cabe en la base ("1E+30", "nan", "inf"): los
    que llaman ya convierten None en "no es un numero valido" para el usuario,
    que es justo lo que hay que decirle. Lanzar aqui seria un 500.
    """
    if valor is None:
        return None
    crudo = str(valor).strip().replace(",", ".")
    if not crudo:
        return None
    try:
        d = Decimal(crudo)
    except InvalidOperation:
        return None
    return d if _cabe(d) and d >= 0 else None


def subtotal(item) -> Decimal | None:
    if item["precio_unitario"] is None:
        return None
    return (Decimal(item["cantidad"]) * Decimal(item["precio_unitario"])).quantize(
        CENTIMO, rounding=ROUND_HALF_UP)


def total(items) -> Decimal:
    return sum((subtotal(i) or Decimal(0) for i in items), Decimal(0))


def _paso(precio: Decimal) -> Decimal:
    """Precio "redondo": de 50 en 50 centimos por debajo de S/ 10, de sol en sol
    por encima. Nada de S/ 2.32."""
    return Decimal("0.50") if precio < 10 else Decimal(1)


def precios_rebajados(items: list[dict], porcentaje: Decimal) -> dict[int, Decimal]:
    """Precios unitarios redondos cuyo total queda un `porcentaje` por debajo
    del de `items`, lo mas cerca posible sin pasarse. {item_id: precio}.

    Primero cada precio baja el porcentaje y se redondea HACIA ABAJO a su paso
    (asi el total arranca por debajo del objetivo). Luego se reparte lo que
    sobra subiendo un paso los items que mas acercan al objetivo, sin que
    ninguno pase de su precio original (alguno puede quedarse en el: con
    precios tan pequenos, bajar un paso a todos ya rebaja mas del 2%). Los
    items sin precio se quedan sin.
    """
    con_precio = [i for i in items if i.get("precio_unitario") is not None]
    if not con_precio:
        return {}
    factor = 1 - Decimal(porcentaje) / 100
    objetivo = (total(con_precio) * factor).quantize(CENTIMO, rounding=ROUND_HALF_UP)
    nuevos: dict[int, Decimal] = {}
    for it in con_precio:
        original = Decimal(it["precio_unitario"])
        paso = _paso(original * factor)
        precio = (original * factor / paso).to_integral_value(rounding="ROUND_FLOOR") * paso
        if precio >= original:          # un precio ya redondo y pequeno
            precio = original - paso
        # El piso de un paso no puede dejarlo POR ENCIMA del original: un item
        # de S/ 0.30 tiene paso 0.50, y max(precio, paso) lo subia a 0.50 en
        # una "rebaja". Sin precio redondo por debajo, se queda en el suyo.
        nuevos[it["id"]] = min(max(precio, paso), original)
    cantidades = {it["id"]: Decimal(it["cantidad"]) for it in con_precio}
    originales = {it["id"]: Decimal(it["precio_unitario"]) for it in con_precio}

    def suma() -> Decimal:
        return sum((cantidades[k] * v for k, v in nuevos.items()), Decimal(0))

    while True:
        resto = objetivo - suma()
        candidatos = [
            (cantidades[k] * _paso(v), k) for k, v in nuevos.items()
            if cantidades[k] * _paso(v) <= resto and v + _paso(v) <= originales[k]]
        if not candidatos:
            return {k: v.quantize(CENTIMO) for k, v in nuevos.items()}
        _, k = max(candidatos)
        nuevos[k] += _paso(nuevos[k])


async def items_de(propuesta_id: int) -> list[dict]:
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT id, orden, descripcion, cantidad, unidad, marca, presentacion,
                      precio_unitario
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


async def _recalcular(conn, propuesta_id: int, venia_de_items: bool = False) -> Decimal:
    """Pone en `precio_ofertado` la suma de los items. Devuelve la suma.

    Sin ningun precio puesto no se pisa un monto que el usuario fijo a mano...
    salvo que `venia_de_items`: entonces el monto guardado ERA la suma de los
    items y ya no es la suma de nada, asi que pasa a NULL. Era el fallo de
    `quitar`: al borrar el ultimo item con precio, la propuesta seguia
    ofertando el total viejo, y el validador y el ZIP lo daban por bueno.

    Se pasa el Decimal tal cual: asyncpg lo acepta para la columna REAL
    (comprobado contra Postgres 17), y el dia que la columna pase a NUMERIC no
    queda un float de por medio redondeando centimos.
    """
    filas = await conn.fetch(
        "SELECT cantidad, precio_unitario FROM propuesta_items WHERE propuesta_id=$1",
        propuesta_id)
    suma = total(filas)
    con_precio = any(f["precio_unitario"] is not None for f in filas)
    if con_precio or venia_de_items:
        await conn.execute(
            "UPDATE propuestas SET precio_ofertado=$2, updated_at=NOW() WHERE id=$1",
            propuesta_id, suma if con_precio else None)
    return suma


async def guardar(propuesta_id: int, cambios: dict[int, tuple], condiciones: dict) -> Decimal:
    """Marca, presentacion y precio de cada item, y las condiciones del pie.
    Devuelve el total.

    `cambios` va por id de item: (marca, precio) o (marca, presentacion, precio).
    Cada UPDATE filtra ademas por propuesta: el id llega de un formulario y no
    puede tocar items de otra propuesta.
    """
    limpias = {k: (condiciones.get(k) or "").strip()[:300] for k in CONDICIONES_INICIALES}
    for cambio in cambios.values():
        validar_rango(cambio[-1], "precio")
    async with connection() as conn, conn.transaction():
        for item_id, cambio in cambios.items():
            marca, presentacion, precio = cambio if len(cambio) == 3 else (cambio[0], None, cambio[1])
            await conn.execute(
                """UPDATE propuesta_items SET marca=$3, presentacion=$4, precio_unitario=$5,
                                              updated_at=NOW()
                        WHERE id=$1 AND propuesta_id=$2""",
                item_id, propuesta_id, (marca or "").strip()[:120] or None,
                (presentacion or "").strip()[:80] or None, precio)
        await conn.execute(
            "UPDATE propuestas SET cotizacion_condiciones=$2::jsonb WHERE id=$1",
            propuesta_id, json.dumps(limpias))
        return await _recalcular(conn, propuesta_id)


# Primera clave del candado de `agregar`: separa estos candados de cualquier
# otro pg_advisory_xact_lock que se use mas adelante con ids de otra tabla.
_CANDADO_ITEMS = 0x1C07


async def agregar(propuesta_id: int, descripcion: str, cantidad: Decimal,
                  unidad: str | None) -> None:
    """Agrega un item al final de la propuesta.

    POR QUE EL CANDADO

      Antes se leia MAX(orden)+1 y se insertaba en otra sentencia. Dos altas a
      la vez (doble clic, dos pestanas) leian el mismo maximo y la segunda
      chocaba con UNIQUE(propuesta_id, orden): un 500. Calcular el orden dentro
      del INSERT no basta por si solo, porque las dos transacciones ven la
      misma foto de la tabla. El candado de transaccion por propuesta pone a la
      segunda en cola hasta que la primera confirma, y entonces ya ve el item
      nuevo. Se suelta solo al terminar la transaccion.
    """
    validar_rango(cantidad, "cantidad")
    async with connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1, $2)",
                           _CANDADO_ITEMS, propuesta_id)
        await conn.execute(
            """INSERT INTO propuesta_items (propuesta_id, orden, descripcion, cantidad, unidad)
               SELECT $1, COALESCE(MAX(orden), 0) + 1, $2, $3, $4
                 FROM propuesta_items WHERE propuesta_id=$1""",
            propuesta_id, descripcion.strip()[:500], cantidad,
            (unidad or "").strip()[:30] or None)
        await conn.execute(
            """UPDATE propuestas SET cotizacion_condiciones =
                       COALESCE(cotizacion_condiciones, $2::jsonb) WHERE id=$1""",
            propuesta_id, json.dumps(CONDICIONES_INICIALES))


async def quitar(propuesta_id: int, item_id: int) -> None:
    async with connection() as conn, conn.transaction():
        # Si algun item tenia precio, el precio_ofertado salia de ellos y hay
        # que recalcularlo aunque ya no quede ninguno con precio (-> NULL).
        venia_de_items = await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM propuesta_items
                               WHERE propuesta_id=$1 AND precio_unitario IS NOT NULL)""",
            propuesta_id)
        await conn.execute(
            "DELETE FROM propuesta_items WHERE id=$1 AND propuesta_id=$2",
            item_id, propuesta_id)
        await _recalcular(conn, propuesta_id, venia_de_items)


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


_UNI = ("", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE", "DIEZ",
        "ONCE", "DOCE", "TRECE", "CATORCE", "QUINCE", "DIECISÉIS", "DIECISIETE", "DIECIOCHO",
        "DIECINUEVE", "VEINTE", "VEINTIUNO", "VEINTIDÓS", "VEINTITRÉS", "VEINTICUATRO",
        "VEINTICINCO", "VEINTISÉIS", "VEINTISIETE", "VEINTIOCHO", "VEINTINUEVE")
_DEC = ("", "", "", "TREINTA", "CUARENTA", "CINCUENTA", "SESENTA", "SETENTA", "OCHENTA",
        "NOVENTA")
_CEN = ("", "CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS",
        "SEISCIENTOS", "SETECIENTOS", "OCHOCIENTOS", "NOVECIENTOS")


def _hasta_999(n: int) -> str:
    if n == 100:
        return "CIEN"
    c, r = divmod(n, 100)
    partes = [_CEN[c]] if c else []
    if r < 30:
        partes.append(_UNI[r])
    else:
        d, u = divmod(r, 10)
        partes.append(_DEC[d] + (f" Y {_UNI[u]}" if u else ""))
    return " ".join(p for p in partes if p)


def _apocope(texto: str) -> str:
    """"VEINTIUNO MIL" no se dice: "VEINTIÚN MIL", "UN MILLÓN"."""
    if texto.endswith("VEINTIUNO"):
        return texto[:-9] + "VEINTIÚN"
    if texto.endswith("UNO"):
        return texto[:-3] + "UN"
    return texto


def monto_en_letras(valor) -> str:
    """12115.2 -> 'DOCE MIL CIENTO QUINCE CON 20/100 SOLES', como pide una cotizacion."""
    d = Decimal(valor).quantize(CENTIMO, rounding=ROUND_HALF_UP)
    entero, cent = int(d), int((d - int(d)) * 100)
    millones, resto = divmod(entero, 1_000_000)
    miles, unidades = divmod(resto, 1000)
    partes = []
    if millones:
        partes.append("UN MILLÓN" if millones == 1
                      else f"{_apocope(_hasta_999(millones))} MILLONES")
    if miles:
        partes.append("MIL" if miles == 1 else f"{_apocope(_hasta_999(miles))} MIL")
    if unidades:
        partes.append(_hasta_999(unidades))
    return f"{' '.join(partes) or 'CERO'} CON {cent:02d}/100 SOLES"
