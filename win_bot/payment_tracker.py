"""Payment Tracker — Seguimiento de pagos pendientes y recibidos."""
import logging
from datetime import date
from shared.db import connection
from shared.config import format_monto, format_fecha

log = logging.getLogger("win.payments")


async def registrar_factura(contrato_id: int, monto: float, concepto: str,
                             factura_numero: str = None) -> int:
    """Registra una factura emitida (pago pendiente)."""
    async with connection() as conn:
        pago_id = await conn.fetchval(
            """INSERT INTO pagos (contrato_id, concepto, monto, fecha_factura,
                                   estado, factura_numero)
            VALUES ($1, $2, $3, $4, 'facturado', $5) RETURNING id""",
            contrato_id, concepto, monto, date.today(), factura_numero,
        )

        # Estimar fecha de pago (30 días después de factura)
        from datetime import timedelta
        await conn.execute(
            "UPDATE pagos SET fecha_pago_esperada=$2 WHERE id=$1",
            pago_id, date.today() + timedelta(days=30),
        )

    log.info(f"Factura registrada: contrato={contrato_id}, monto={format_monto(monto)}")
    return pago_id


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
