"""El flujo de despues de ganar: que el aviso llegue a quien tiene que actuar.

Lo que se encontro el 2026-10-07 revisando el flujo con la base real:

  - `detectar_adjudicaciones` filtraba `estado = 'enviado'` y ninguna ruta pone
    ese estado: el detector corria cada 30 minutos sobre cero filas.
  - Los avisos de adjudicacion y de cobros vencidos salian solo por Telegram y
    WhatsApp; el unico cliente real solo tenia correo.
  - Los plazos de contrato se mandaban todos a ADMIN_ID.
"""
from datetime import timedelta

from shared import fechas
from shared.notificaciones import texto_plazo_proximo
from tests.conftest import sin_base


def _plazo(dias: int) -> dict:
    return {"descripcion": "Firma del contrato", "objeto": "ADQUISICION DE TUBERIAS",
            "entidad": "MUNICIPALIDAD PROVINCIAL DE ESPINAR", "numero_contrato": "C-12",
            "fecha_limite": fechas.hoy() + timedelta(days=dias)}


def test_el_titulo_dice_cuantos_dias_quedan():
    assert texto_plazo_proximo(_plazo(0))[0] == "Un plazo de tu contrato vence hoy"
    assert texto_plazo_proximo(_plazo(1))[0] == "Un plazo de tu contrato vence mañana"
    assert texto_plazo_proximo(_plazo(3))[0] == "Un plazo de tu contrato vence en 3 días"
    assert texto_plazo_proximo(_plazo(-2))[0] == "Plazo vencido hace 2 día(s)"


def test_el_cuerpo_lleva_lo_que_hace_falta_para_actuar():
    _, cuerpo = texto_plazo_proximo(_plazo(2))
    assert "Firma del contrato" in cuerpo
    assert "ESPINAR" in cuerpo
    assert "C-12" in cuerpo


@sin_base
async def test_una_propuesta_lista_tambien_cuenta_como_presentada(usuario, marca):
    """El estado real tras generar el expediente es 'listo'; con 'enviado' a
    secas el detector no veia ninguna propuesta de nadie."""
    from shared.db import connection
    from shared.notificaciones import detectar_adjudicaciones

    lic = f"PRUEBA-LISTO-{marca}"
    async with connection() as c:
        eid = await c.fetchval(
            """INSERT INTO empresas (razon_social, ruc, usuario_id, activa)
               VALUES ($1, $2, $3, TRUE) RETURNING id""",
            "Servicios Altiplano E.I.R.L.", "20" + marca[:9], usuario["id"])
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto,
                                         fecha_cierre, proveedor_ganador)
               VALUES ($1, 'prueba', 'ENTIDAD', 'Servicio',
                       NOW() - INTERVAL '5 days', 'SERVICIOS ALTIPLANO EIRL')""", lic)
        await c.execute(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'listo')", lic, eid)
    try:
        parte = await detectar_adjudicaciones()
        assert parte["coincidencias"] >= 1
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM notificaciones_enviadas WHERE licitacion_id=$1", lic)
            await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lic)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)
            await c.execute("DELETE FROM empresas WHERE id=$1", eid)
