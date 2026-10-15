"""Una pregunta no se queda abierta pidiendo un dato que ya se sabe.

La propuesta 1 del Minimarket (SC 5884) pedia el cargo del representante,
que estaba en la ficha de la empresa, y el CCI, que la DJ ya imprimia porque
se aprendio en otra propuesta. Las preguntas se creaban al abrir la propuesta
y nadie las volvia a mirar.
"""
import pytest

from tests.conftest import sin_base


@sin_base
@pytest.mark.asyncio
async def test_al_abrir_la_propuesta_se_cierran_las_que_ya_tienen_respuesta(
        usuario, empresa, cliente, marca):
    from prep_bot.questioner import generar_preguntas_propuesta
    from shared.db import connection, kb_get, kb_set

    async with connection() as c:
        lic = await c.fetchval(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto)
               VALUES ($1, 'gore_portals', 'cotizacion', $2, 'E', 'O') RETURNING id""",
            f"prueba_{marca}", f"COT-{marca}")
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'iniciado') RETURNING id", lic, empresa)
        await c.execute("UPDATE empresas SET cargo_representante=NULL WHERE id=$1", empresa)
    try:
        await generar_preguntas_propuesta(pid, empresa)
        async with connection() as c:
            abiertas = {r["kb_clave"] for r in await c.fetch(
                "SELECT kb_clave FROM preguntas WHERE propuesta_id=$1 AND NOT respondida", pid)}
        assert {"cargo_representante", "cci", "entidad_bancaria"} <= abiertas

        # Despues llegan los datos: el cargo por la ficha, el banco por otra via.
        async with connection() as c:
            await c.execute("UPDATE empresas SET cargo_representante='GERENTE GENERAL' "
                            "WHERE id=$1", empresa)
        await kb_set(empresa, "financiero", "cci", "00235500199892905762")
        await kb_set(empresa, "financiero", "entidad_bancaria", "Banco de Crédito del Perú")

        async with connection() as c:
            await c.execute("DELETE FROM intentos_acceso")
        r = await cliente.post("/entrar", data={"email": usuario["email"],
                                                "password": usuario["password"]})
        assert r.status_code == 303
        pagina = (await cliente.get(f"/propuestas/{pid}")).text
        # Siguen a la vista, pero respondidas con el dato que ya se sabia.
        assert "Respondida:</span> 00235500199892905762" in pagina
        assert "Respondida:</span> GERENTE GENERAL" in pagina

        async with connection() as c:
            fila = await c.fetchrow(
                "SELECT respuesta FROM preguntas WHERE propuesta_id=$1 AND kb_clave='cci'", pid)
            sigue = await c.fetchval(
                "SELECT count(*) FROM preguntas WHERE propuesta_id=$1 AND NOT respondida "
                "AND kb_clave='cuenta_corriente'", pid)
            pendientes = await c.fetchval(
                "SELECT preguntas_pendientes FROM propuestas WHERE id=$1", pid)
        assert fila["respuesta"] == "00235500199892905762"
        assert sigue == 1                 # lo que de verdad falta se sigue pidiendo
        assert pendientes == len(abiertas) - 3
        assert await kb_get(empresa, "legal", "cargo_representante") == "GERENTE GENERAL"
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE id=$1", pid)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


@sin_base
@pytest.mark.asyncio
async def test_en_una_cotizacion_menor_se_retiran_las_que_no_se_exigen(empresa, marca):
    """La partida registral y la vigencia de poder no se piden en una
    cotizacion menor; las preguntas creadas antes de esa regla se retiran."""
    from prep_bot.questioner import cerrar_las_ya_resueltas
    from shared.db import connection

    async with connection() as c:
        lic = await c.fetchval(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto)
               VALUES ($1, 'gore_portals', 'cotizacion', $2, 'E', 'O') RETURNING id""",
            f"prueba_{marca}", f"COT-{marca}")
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'preguntas_pendientes') RETURNING id", lic, empresa)
        for clave in ("partida_registral", "vigencia_poder"):
            await c.execute(
                """INSERT INTO preguntas (propuesta_id, empresa_id, campo_requerido, pregunta,
                       kb_categoria, kb_clave) VALUES ($1, $2, $3, $3, 'legal', $3)""",
                pid, empresa, clave)
    try:
        assert await cerrar_las_ya_resueltas(pid) == 2
        async with connection() as c:
            assert await c.fetchval("SELECT count(*) FROM preguntas WHERE propuesta_id=$1",
                                    pid) == 0
            fila = await c.fetchrow(
                "SELECT preguntas_pendientes, estado FROM propuestas WHERE id=$1", pid)
        assert (fila["preguntas_pendientes"], fila["estado"]) == (0, "listo")
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE id=$1", pid)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)
