"""Payment Tracker — Seguimiento de pagos pendientes y recibidos."""
import logging
from datetime import date

from shared.config import format_monto
from shared.db import connection

log = logging.getLogger("win.payments")


async def registrar_factura(contrato_id: int, monto: float, concepto: str,
                             factura_numero: str = None,
                             fecha_conformidad: date = None,
                             expediente_siaf: str = None) -> int:
    """Registra una factura emitida y calcula cuando vence el plazo legal.

    Antes ponia "factura + 30 dias corridos", un numero a ojo. La Ley 32069 fija
    10 DIAS HABILES desde la CONFORMIDAD, y los dos elementos estaban mal:

      - El ancla: el plazo corre desde la conformidad, no desde la factura.
      - La cuenta: habiles, no corridos.

    Con conformidad el 24 de julio de 2026 el limite real cae el 12 de agosto
    (dos Fiestas Patrias, la Batalla de Junin y tres fines de semana en medio).
    La formula vieja apuntaba al 23 de agosto y habria dejado al proveedor
    esperando mientras la entidad ya estaba en mora desde el 13.

    Sin conformidad NO se inventa fecha: el plazo aun no empezo a correr, y eso
    es informacion util, no un hueco que rellenar.
    """
    from shared.plazos_pago import fecha_limite_pago

    async with connection() as conn:
        # La categoria del proceso decide el plazo, y viaja por el contrato.
        categoria = await conn.fetchval(
            """SELECT l.categoria FROM contratos c
                 JOIN licitaciones l ON l.id = c.licitacion_id
                WHERE c.id = $1""",
            contrato_id)
        limite = fecha_limite_pago(fecha_conformidad, categoria)
        pago_id = await conn.fetchval(
            """INSERT INTO pagos (contrato_id, concepto, monto, fecha_factura,
                                  estado, numero_factura, fecha_conformidad,
                                  fecha_limite_pago, fecha_pago_esperada,
                                  expediente_siaf)
            VALUES ($1, $2, $3, $4, 'facturado', $5, $6, $7, $7, $8) RETURNING id""",
            contrato_id, concepto, monto, date.today(), factura_numero,
            fecha_conformidad, limite, expediente_siaf,
        )

    log.info("Factura registrada: contrato=%s monto=%s limite=%s",
             contrato_id, format_monto(monto), limite or "sin conformidad aun")
    return pago_id


async def registrar_conformidad(pago_id: int, fecha: date) -> dict:
    """Anota la conformidad y recalcula el vencimiento. Es el dato que importa.

    Se puede registrar despues de la factura porque en la practica llega
    despues: primero se entrega y se factura, y la entidad da la conformidad
    cuando revisa. Hasta ese momento no hay plazo que contar.
    """
    from shared.plazos_pago import fecha_limite_pago, plazo_legal

    async with connection() as conn:
        categoria = await conn.fetchval(
            """SELECT l.categoria FROM pagos p
                 JOIN contratos c ON c.id = p.contrato_id
                 JOIN licitaciones l ON l.id = c.licitacion_id
                WHERE p.id = $1""",
            pago_id)
        limite = fecha_limite_pago(fecha, categoria)
        await conn.execute(
            """UPDATE pagos SET fecha_conformidad=$2, fecha_limite_pago=$3,
                                fecha_pago_esperada=$3
                WHERE id=$1""",
            pago_id, fecha, limite)
    _, explicacion = plazo_legal(categoria)
    return {"fecha_limite": limite, "explicacion": explicacion}


async def pagos_vencidos(empresa_id: int = None) -> list[dict]:
    """Pagos cuyo plazo legal ya vencio, con los dias de mora.

    Es la consulta que da sentido a todo esto: no "cuando me pagan" sino "a
    quien puedo reclamarle ya". La mora se calcula al leer porque cambia sola
    cada dia habil que pasa; guardarla obligaria a un proceso nocturno que
    puede fallar en silencio.
    """
    from shared.plazos_pago import dias_de_mora, en_prorroga

    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT p.*, c.numero_contrato, c.empresa_id, l.entidad, l.categoria
                 FROM pagos p
                 JOIN contratos c ON c.id = p.contrato_id
                 LEFT JOIN licitaciones l ON l.id = c.licitacion_id
                WHERE p.fecha_limite_pago IS NOT NULL
                  AND p.fecha_pago_real IS NULL
                  AND p.fecha_limite_pago < CURRENT_DATE
                  AND ($1::int IS NULL OR c.empresa_id = $1)
                ORDER BY p.fecha_limite_pago""",
            empresa_id)

    salida = []
    for f in filas:
        d = dict(f)
        d["dias_mora"] = dias_de_mora(f["fecha_limite_pago"])
        d["en_prorroga"] = en_prorroga(f["fecha_limite_pago"])
        salida.append(d)
    return salida


async def registrar_pago(contrato_id: int, monto: float, concepto: str = None,
                          pago_id: int = None) -> int:
    """Registra un pago recibido."""
    async with connection() as conn:
        if pago_id:
            # Actualizar pago existente
            await conn.execute(
                """UPDATE pagos SET estado='pagado', fecha_pago_real=$2
                WHERE id=$1""",
                pago_id, date.today(),
            )
            return pago_id
        else:
            # Crear nuevo registro de pago recibido
            new_id = await conn.fetchval(
                """INSERT INTO pagos (contrato_id, concepto, monto, fecha_pago_real, estado)
                VALUES ($1, $2, $3, $4, 'pagado') RETURNING id""",
                contrato_id, concepto or "Pago recibido", monto, date.today(),
            )
            return new_id


async def obtener_resumen_pagos(empresa_id: int = None) -> dict:
    """Resumen completo de pagos."""
    async with connection() as conn:
        where = "WHERE c.empresa_id=$1" if empresa_id else ""
        params = [empresa_id] if empresa_id else []

        # Pagos pendientes
        pendientes = await conn.fetch(
            f"""SELECT pg.*, c.numero_contrato, l.entidad, l.objeto
            FROM pagos pg
            JOIN contratos c ON pg.contrato_id = c.id
            JOIN licitaciones l ON c.licitacion_id = l.id
            {where}
            AND pg.estado IN ('pendiente', 'facturado')
            ORDER BY pg.fecha_pago_esperada""",
            *params,
        ) if empresa_id else await conn.fetch(
            """SELECT pg.*, c.numero_contrato, l.entidad, l.objeto
            FROM pagos pg
            JOIN contratos c ON pg.contrato_id = c.id
            JOIN licitaciones l ON c.licitacion_id = l.id
            WHERE pg.estado IN ('pendiente', 'facturado')
            ORDER BY pg.fecha_pago_esperada"""
        )

        # Pagos recibidos
        cobrados = await conn.fetch(
            f"""SELECT pg.*, c.numero_contrato, l.entidad
            FROM pagos pg
            JOIN contratos c ON pg.contrato_id = c.id
            JOIN licitaciones l ON c.licitacion_id = l.id
            {where}
            AND pg.estado = 'pagado'
            ORDER BY pg.fecha_pago_real DESC""",
            *params,
        ) if empresa_id else await conn.fetch(
            """SELECT pg.*, c.numero_contrato, l.entidad
            FROM pagos pg
            JOIN contratos c ON pg.contrato_id = c.id
            JOIN licitaciones l ON c.licitacion_id = l.id
            WHERE pg.estado = 'pagado'
            ORDER BY pg.fecha_pago_real DESC"""
        )

    total_pendiente = sum(p["monto"] for p in pendientes if p["monto"])
    total_cobrado = sum(p["monto"] for p in cobrados if p["monto"])

    # Pagos vencidos (esperados pero no recibidos)
    vencidos = [p for p in pendientes
                if p.get("fecha_pago_esperada") and p["fecha_pago_esperada"] < date.today()]

    return {
        "pendientes": [dict(p) for p in pendientes],
        "cobrados": [dict(p) for p in cobrados],
        "total_pendiente": total_pendiente,
        "total_cobrado": total_cobrado,
        "vencidos": len(vencidos),
        "total_vencido": sum(p["monto"] for p in vencidos if p["monto"]),
    }


async def obtener_pagos_por_contrato(contrato_id: int) -> list[dict]:
    """Lista pagos de un contrato específico."""
    async with connection() as conn:
        rows = await conn.fetch(
            "SELECT * FROM pagos WHERE contrato_id=$1 ORDER BY fecha_factura, fecha_pago_real",
            contrato_id,
        )
    return [dict(r) for r in rows]
