"""La lista de licitaciones dice a que hora cierra cada una (hora de Lima) y
lleva el numero del proceso, que tambien se puede buscar. El 16/10 dos
cotizaciones del GOREMAD con el mismo objeto se veian iguales, decian "0 d"
sin hora, y el contador de arriba marcaba "0 vigentes" encima de siete."""
import re
from datetime import timedelta

import pytest

from tests.conftest import sin_base


@sin_base
@pytest.mark.asyncio
async def test_la_lista_muestra_hora_numero_y_cuenta_las_vigentes(usuario, cliente, marca):
    from shared import fechas
    from shared.db import connection

    cierre = (fechas.ahora() + timedelta(hours=5, minutes=30)).replace(second=0, microsecond=0)
    num = f"COT-{marca[-6:]}-2026-GOREMAD"
    async with connection() as c:
        lic = await c.fetchval(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto,
                                         fecha_cierre, score_viabilidad)
               VALUES ($1, 'gore_portals', 'cotizacion', $2, 'GORE PRUEBA', $3, $4, 99)
               RETURNING id""",
            f"prueba_hora_{marca}", num, f"OBJETO HORA {marca}", cierre)
        await c.execute("DELETE FROM intentos_acceso")
    try:
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
        pagina = (await cliente.get("/panel")).text
        assert num in pagina
        assert cierre.strftime("%H:%M") in pagina
        assert "en 5 h" in pagina
        vigentes = int(re.search(r'<div class="n">(\d+)</div><div class="l">vigentes ahora',
                                 pagina).group(1))
        assert vigentes >= 1

        # Se encuentra por el numero, aunque se escriba solo una parte.
        encontrada = (await cliente.get(f"/panel?q={marca[-6:]}")).text
        assert f"OBJETO HORA {marca}" in encontrada
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)
