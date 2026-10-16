"""La misma cotizacion con otra empresa: precios redondos, el total un 2% por
debajo (lo mas cerca sin pasarse) y ninguno por encima del original."""
from decimal import Decimal as D

import pytest

from shared.cotizacion import precios_rebajados, total
from tests.conftest import sin_base

# Los 14 items de la E/M 4202-2026-MPT, tal como los cotizo Soluciones.
E_M_4202 = [(60, "7.00"), (30, "3.00"), (15, "55.00"), (5, "42.00"), (20, "4.00"),
            (20, "7.50"), (50, "5.50"), (6, "65.00"), (12, "58.00"), (5, "6.00"),
            (10, "6.00"), (20, "17.50"), (50, "19.00"), (6, "38.00")]


def _items(datos):
    return [{"id": i, "cantidad": D(c), "precio_unitario": D(p)}
            for i, (c, p) in enumerate(datos, 1)]


def test_rebaja_dos_por_ciento_con_precios_redondos():
    items = _items(E_M_4202)
    nuevos = precios_rebajados(items, D(2))
    antes = total(items)
    despues = total([{**it, "precio_unitario": nuevos[it["id"]]} for it in items])
    assert despues <= antes * D("0.98")
    assert despues >= antes * D("0.975")          # cerca del 2%, no un 4%
    for it in items:
        p = nuevos[it["id"]]
        assert p <= it["precio_unitario"]
        paso = D("0.50") if p < 10 else D(1)
        assert p % paso == 0, p                     # nada de 2.32


def test_items_sin_precio_no_se_tocan():
    items = _items([(1, "10.00")]) + [{"id": 9, "cantidad": D(1), "precio_unitario": None}]
    assert set(precios_rebajados(items, D(2))) == {1}


@sin_base
@pytest.mark.asyncio
async def test_copia_la_cotizacion_a_otra_empresa(usuario, empresa, cliente, marca):
    from shared.db import connection

    async with connection() as c:
        otra = await c.fetchval(
            "INSERT INTO empresas (usuario_id, razon_social, ruc) "
            "SELECT usuario_id, 'OTRA EMPRESA SAC', '20999999999' FROM empresas WHERE id=$1 "
            "RETURNING id", empresa)
        lic = await c.fetchval(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto)
               VALUES ($1, 'mpt_tambopata', 'cotizacion', 'E/M-1-2026-MPT', 'MPT', 'O')
               RETURNING id""", f"prueba_otra_{marca}")
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado, cotizacion_condiciones) "
            "VALUES ($1, $2, 'enviado', '{\"plazo_entrega\": \"07 días\"}'::jsonb) RETURNING id",
            lic, empresa)
        for n, (cant, precio) in enumerate(E_M_4202, 1):
            await c.execute(
                "INSERT INTO propuesta_items (propuesta_id, orden, descripcion, cantidad, unidad, "
                "marca, precio_unitario) VALUES ($1, $2, $3, $4, 'UNIDAD', 'OVE', $5)",
                pid, n, f"ITEM {n}", cant, D(precio))
        await c.execute("DELETE FROM intentos_acceso")
    try:
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
        r = await cliente.post(f"/propuestas/{pid}/otra-empresa",
                               data={"empresa_id": str(otra), "rebaja": "2"})
        assert r.status_code == 303, r.text
        async with connection() as c:
            nueva = await c.fetchrow(
                "SELECT id, estado, precio_ofertado, cotizacion_condiciones FROM propuestas "
                "WHERE licitacion_id=$1 AND empresa_id=$2", lic, otra)
            filas = await c.fetch(
                "SELECT marca, precio_unitario FROM propuesta_items WHERE propuesta_id=$1",
                nueva["id"])
        # "Faltan datos" si a la empresa nueva le falta algo (aqui, de prueba, si).
        assert nueva["estado"] in ("listo", "preguntas_pendientes")
        assert len(filas) == 14 and all(f["marca"] == "OVE" for f in filas)
        assert D(str(nueva["precio_ofertado"])) == D("4657.00")
        assert f"/propuestas/{nueva['id']}" in r.headers["location"]

        # Una segunda vez no duplica: lleva a la que ya existe.
        r = await cliente.post(f"/propuestas/{pid}/otra-empresa",
                               data={"empresa_id": str(otra), "rebaja": "2"})
        assert f"/propuestas/{nueva['id']}?error=" in r.headers["location"]
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lic)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)
            await c.execute("DELETE FROM empresas WHERE id=$1", otra)


@sin_base
@pytest.mark.asyncio
async def test_la_licitacion_deja_postular_con_otra_empresa(usuario, empresa, cliente, marca):
    """Que una empresa ya haya postulado no cierra la puerta a las demas."""
    from shared.db import connection

    async with connection() as c:
        otra = await c.fetchval(
            "INSERT INTO empresas (usuario_id, razon_social, ruc) "
            "SELECT usuario_id, 'SEGUNDA EMPRESA SAC', '20888888888' FROM empresas WHERE id=$1 "
            "RETURNING id", empresa)
        lic = await c.fetchval(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto)
               VALUES ($1, 'mpt_tambopata', 'cotizacion', 'E/M-2-2026-MPT', 'MPT', 'O')
               RETURNING id""", f"prueba_lic_otra_{marca}")
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'enviado') RETURNING id", lic, empresa)
        await c.execute(
            "INSERT INTO propuesta_items (propuesta_id, orden, descripcion, cantidad, unidad, "
            "precio_unitario) VALUES ($1, 1, 'PAPEL', 50, 'EMP', 19)", pid)
        await c.execute("DELETE FROM intentos_acceso")
    try:
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
        pagina = (await cliente.get(f"/licitacion/{lic}")).text
        assert "Enviada</span>" in pagina
        assert f'formaction="/propuestas/{pid}/otra-empresa"' in pagina
        assert "SEGUNDA EMPRESA SAC" in pagina
        assert "Empezar en blanco" in pagina
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lic)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)
            await c.execute("DELETE FROM empresas WHERE id=$1", otra)
