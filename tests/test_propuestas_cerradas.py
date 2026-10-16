"""Lo que ya cerro no se puede enviar: sale de la lista de propuestas y queda
en "Cerradas". La lista mezclaba cotizaciones vencidas del 12/10 con las que
cerraban ese mismo dia."""
from datetime import timedelta

import pytest

from tests.conftest import sin_base


@sin_base
@pytest.mark.asyncio
async def test_las_propuestas_vencidas_se_archivan(usuario, empresa, cliente, marca):
    from shared import fechas
    from shared.db import connection

    ahora = fechas.ahora()
    ids = {}
    async with connection() as c:
        for clave, cierre in (("vieja", ahora - timedelta(days=2)),
                              ("viva", ahora + timedelta(hours=3))):
            lic = await c.fetchval(
                """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto,
                                             fecha_cierre)
                   VALUES ($1, 'gore_portals', 'cotizacion', $1, 'E', $2, $3) RETURNING id""",
                f"prueba_{clave}_{marca}", f"OBJETO {clave.upper()} {marca}", cierre)
            ids[clave] = (lic, await c.fetchval(
                "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
                "VALUES ($1, $2, 'listo') RETURNING id", lic, empresa))
        await c.execute("DELETE FROM intentos_acceso")
    try:
        r = await cliente.post("/entrar", data={"email": usuario["email"],
                                                "password": usuario["password"]})
        assert r.status_code == 303
        vigentes = (await cliente.get("/propuestas")).text
        assert f"OBJETO VIVA {marca}" in vigentes
        assert f"OBJETO VIEJA {marca}" not in vigentes
        assert "Cerradas (1)" in vigentes

        cerradas = (await cliente.get("/propuestas?cerradas=1")).text
        assert f"OBJETO VIEJA {marca}" in cerradas
        assert f"OBJETO VIVA {marca}" not in cerradas
        # Archivada no es borrada: se sigue pudiendo abrir.
        assert (await cliente.get(f"/propuestas/{ids['vieja'][1]}")).status_code == 200
    finally:
        async with connection() as c:
            for lic, pid in ids.values():
                await c.execute("DELETE FROM propuestas WHERE id=$1", pid)
                await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


@sin_base
@pytest.mark.asyncio
async def test_con_envio_armado_el_expediente_entrega_el_zip_firmado(
        usuario, empresa, cliente, marca, tmp_path, monkeypatch):
    """En la SC 6023 se bajo el expediente de SEACE (carta, pacto, cotizacion
    generica) en vez del ZIP firmado a mano. Con envio armado, todo lleva a el."""
    import zipfile

    from shared.db import connection
    from web import propuestas

    monkeypatch.setattr(propuestas, "PAQUETES", tmp_path)
    async with connection() as c:
        lic = await c.fetchval(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto, url)
               VALUES ($1, 'gore_portals', 'cotizacion', 'COT-1-2026-GOREMAD', 'E', 'O',
                       'http://cotizaciones.regionmadrededios.gob.pe/solicitud/1') RETURNING id""",
            f"prueba_armado_{marca}")
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado, expediente_zip_path) "
            "VALUES ($1, $2, 'listo', '/no/existe.zip') RETURNING id", lic, empresa)
        await c.execute(
            "INSERT INTO propuesta_items (propuesta_id, orden, descripcion, cantidad, unidad, "
            "precio_unitario) VALUES ($1, 1, 'GOMA', 2, 'UNIDAD', 4.5)", pid)
        await c.execute("UPDATE propuestas SET cotizacion_condiciones='{}'::jsonb WHERE id=$1", pid)
        await c.execute("DELETE FROM intentos_acceso")
    with zipfile.ZipFile(tmp_path / f"propuesta_{pid}.zip", "w") as z:
        z.writestr("01_Cotizacion_firmada.pdf", b"x")
    try:
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
        r = await cliente.get(f"/propuestas/{pid}/descargar")
        assert r.status_code == 303 and r.headers["location"] == f"/propuestas/{pid}/paquete"
        r = await cliente.get(f"/propuestas/{pid}/paquete")
        assert r.status_code == 200 and r.content.startswith(b"PK")
        pagina = (await cliente.get(f"/propuestas/{pid}")).text
        assert "Descargar ZIP para el portal" in pagina
        assert "01_Cotizacion_firmada.pdf" in pagina
        assert "Armar expediente ZIP" not in pagina
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE id=$1", pid)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


def test_el_envio_al_goremad_lleva_solo_lo_que_pide_el_portal():
    """El portal pide cotizacion, DJ de postor, RNP y ficha tecnica. La
    consulta RUC va en el mismo PDF que el RNP, detras de la constancia."""
    import io

    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    from web.propuestas import _ANEXOS_GOREMAD, _unir_pdfs
    assert [t for t, _ in _ANEXOS_GOREMAD] == [("rnp", "ficha"), ("eett",)]

    def pdf(texto, hojas):
        b = io.BytesIO()
        c = canvas.Canvas(b)
        for _ in range(hojas):
            c.drawString(72, 720, texto)
            c.showPage()
        c.save()
        return b.getvalue()
    unido = PdfReader(io.BytesIO(_unir_pdfs([pdf("RNP", 1), pdf("RUC", 2)])))
    assert [p.extract_text().strip() for p in unido.pages] == ["RNP", "RUC", "RUC"]
