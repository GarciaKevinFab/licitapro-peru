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
