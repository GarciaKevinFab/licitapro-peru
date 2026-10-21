"""El dinero en NUMERIC(14,2) y en Decimal de punta a punta (migracion 0021).

    python -m pytest tests/test_auditoria_numeric.py -q

POR QUE

  Los montos vivian en columnas REAL (float de 32 bits). S/ 300 000.10 volvia
  como 300 000.09375 y S/ 25 000 000.55 como 25 000 000: centimos y hasta
  soles enteros perdidos en montos de una licitacion regional cualquiera.

  Con NUMERIC, asyncpg devuelve Decimal, y Decimal no se mezcla con float:
  `precio / 1.18` o `monto_ref * 0.92` pasan a ser un TypeError. Varios de esos
  calculos estaban dentro de un `except Exception` y el fallo no se veia: el
  rango de precios de la ficha desaparecia sin mas. Estas pruebas recorren lo
  que lee montos de la base -- paginas, documentos, filtros -- con dos cifras
  que REAL no sabia guardar.
"""
import io
from decimal import Decimal

import pytest

from shared.config import a_soles, desglose_igv, format_monto
from tests.conftest import sin_base

D = Decimal
CON_CENTIMOS = D("300000.10")      # por encima de 2**18: REAL pierde el centimo
ENORME = D("25000000.55")          # por encima de 2**24: REAL pierde hasta el entero

COLUMNAS_DINERO = {
    ("contratos", "carta_fianza_monto"), ("contratos", "monto_adjudicado"),
    ("experiencia", "monto"),
    ("historico_precios", "monto_adjudicado"), ("historico_precios", "monto_referencial"),
    ("licitaciones", "monto_referencial"), ("pagos", "monto"),
    ("propuestas", "precio_ofertado"), ("propuestas", "precio_sugerido_max"),
    ("propuestas", "precio_sugerido_min"),
    ("user_config", "monto_max"), ("user_config", "monto_min"),
}
# No son dinero: un puntaje y un cociente. Se quedan en float a proposito.
COLUMNAS_FLOAT = {("analisis_ia", "score"), ("licitaciones", "score_viabilidad"),
                  ("historico_precios", "ratio_adj_ref")}


# ─── Sin base: las piezas de calculo ─────────────────────

@pytest.mark.parametrize("valor, esperado", [
    (300000.1, D("300000.10")),            # el float pasa por str: sin cola binaria
    (25000000.55, D("25000000.55")),
    (D("12115.2"), D("12115.20")),
    ("1500.005", D("1500.01")),            # medio centimo hacia arriba, como en caja
    (7, D("7.00")),
])
def test_a_soles_da_decimal_en_centimos(valor, esperado):
    resultado = a_soles(valor)
    assert isinstance(resultado, Decimal)
    assert resultado == esperado and resultado.as_tuple().exponent == -2


@pytest.mark.parametrize("valor", [None, True, float("nan"), float("inf"), "-inf",
                                   "abc", "", "1.2.3", D("NaN")])
def test_a_soles_no_inventa_un_monto(valor):
    assert a_soles(valor) is None


@pytest.mark.parametrize("total", [ENORME, CON_CENTIMOS, D("118.00"), D("0.01"), D("99")])
def test_el_desglose_del_igv_cuadra_al_centimo(total):
    """Antes `precio / 1.18` y `precio - precio / 1.18` en float: con Decimal es
    un TypeError, y con float el subtotal mas el IGV podia no dar el total."""
    subtotal, igv = desglose_igv(total)
    assert subtotal + igv == total
    assert subtotal.as_tuple().exponent == -2 and igv.as_tuple().exponent == -2


def test_desglose_de_118_soles():
    assert desglose_igv(D("118.00")) == (D("100.00"), D("18.00"))


def test_format_monto_con_decimal():
    assert format_monto(ENORME) == "S/ 25,000,000.55"
    assert format_monto(CON_CENTIMOS, "USD") == "US$ 300,000.10"


def test_el_monto_de_un_comando_de_telegram_sale_en_decimal():
    from win_bot.main import _monto
    assert _monto("25000000.55") == ENORME and isinstance(_monto("1"), Decimal)
    for malo in ("nan", "inf", "-5", "0", "1e30", "abc"):
        assert _monto(malo) is None, malo


def test_el_monto_de_un_contrato_sale_en_decimal():
    from web.contratos import _leer_monto
    assert _leer_monto("25,000,000.55") == ENORME
    assert isinstance(_leer_monto("15000.50"), Decimal)


def test_monto_para_columna_descarta_lo_que_no_cabe():
    from shared.db import monto_para_columna
    assert monto_para_columna(300000.1) == CON_CENTIMOS
    assert monto_para_columna(None) is None
    assert monto_para_columna(1e13) is None             # NUMERIC(14,2) llega a 10**12
    assert monto_para_columna(float("nan")) is None


def test_el_json_de_la_base_acepta_decimal():
    import json

    from shared.db import _json_extra
    assert json.loads(json.dumps({"m": ENORME}, default=_json_extra)) == {"m": 25000000.55}


# ─── Con base: el esquema y la ida y vuelta ──────────────

@sin_base
async def test_las_columnas_de_dinero_son_numeric_14_2():
    from shared.db import connection
    async with connection() as c:
        filas = await c.fetch(
            """SELECT table_name, column_name, data_type, numeric_precision, numeric_scale
                 FROM information_schema.columns
                WHERE table_schema = 'public' AND data_type IN ('real', 'numeric',
                                                                'double precision')""")
    tipos = {(f["table_name"], f["column_name"]): f for f in filas}
    for clave in COLUMNAS_DINERO:
        f = tipos[clave]
        assert (f["data_type"], f["numeric_precision"], f["numeric_scale"]) == \
            ("numeric", 14, 2), clave
    for clave in COLUMNAS_FLOAT:
        assert tipos[clave]["data_type"] in ("real", "double precision"), clave


async def _licitacion(c, lic, monto, tipo="AS"):
    await c.execute(
        """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto,
                                     monto_referencial, fecha_cierre, departamento)
           VALUES ($1, 'prueba', $2, $3, 'MUNICIPALIDAD DE PRUEBA', $4, $5,
                   (NOW() AT TIME ZONE 'America/Lima') + INTERVAL '5 days', 'Lima')""",
        lic, tipo, f"AS-{lic}", f"OBRA NUMERICA {lic}", monto)


async def _limpiar(lics):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM contratos WHERE licitacion_id = ANY($1::text[])", lics)
        await c.execute("DELETE FROM propuestas WHERE licitacion_id = ANY($1::text[])", lics)
        await c.execute("DELETE FROM licitaciones WHERE id = ANY($1::text[])", lics)


@sin_base
async def test_los_montos_vuelven_exactos(empresa, marca):
    """Lo que REAL no sabia guardar, en cada tabla con dinero."""
    from shared.db import connection
    lic = f"num_{marca}"
    try:
        async with connection() as c:
            await _licitacion(c, lic, ENORME)
            pid = await c.fetchval(
                """INSERT INTO propuestas (licitacion_id, empresa_id, precio_ofertado)
                   VALUES ($1, $2, $3) RETURNING id""", lic, empresa, CON_CENTIMOS)
            cid = await c.fetchval(
                """INSERT INTO contratos (propuesta_id, licitacion_id, empresa_id,
                                          monto_adjudicado, carta_fianza_monto)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                pid, lic, empresa, ENORME, CON_CENTIMOS)
            await c.execute("INSERT INTO pagos (contrato_id, concepto, monto) "
                            "VALUES ($1, 'Primer pago', $2)", cid, CON_CENTIMOS)
            await c.execute(
                "INSERT INTO experiencia (empresa_id, entidad_contratante, objeto_contrato, "
                "monto) VALUES ($1, 'ENTIDAD', 'OBRA', $2)", empresa, ENORME)

            assert await c.fetchval("SELECT monto_referencial FROM licitaciones WHERE id=$1",
                                    lic) == ENORME
            assert await c.fetchval("SELECT precio_ofertado FROM propuestas WHERE id=$1",
                                    pid) == CON_CENTIMOS
            fila = await c.fetchrow("SELECT * FROM contratos WHERE id=$1", cid)
            assert (fila["monto_adjudicado"], fila["carta_fianza_monto"]) == \
                (ENORME, CON_CENTIMOS)
            assert await c.fetchval("SELECT monto FROM pagos WHERE contrato_id=$1",
                                    cid) == CON_CENTIMOS
            assert await c.fetchval("SELECT monto FROM experiencia WHERE empresa_id=$1",
                                    empresa) == ENORME
            # Y la suma en SQL, que con REAL acumulaba el error de cada fila.
            assert await c.fetchval(
                "SELECT SUM(monto_adjudicado) + SUM(carta_fianza_monto) FROM contratos "
                "WHERE id=$1", cid) == D("25300000.65")
    finally:
        await _limpiar([lic])


@sin_base
async def test_el_scraper_guarda_su_float_exacto_y_no_tumba_la_fila(marca):
    """El float de un scraper entra exacto; uno que no cabe se queda en NULL
    en vez de llevarse por delante la licitacion entera."""
    from shared.db import connection, refrescar_licitacion, upsert_licitacion
    buena, rara, vieja = f"num_ok_{marca}", f"num_raro_{marca}", f"num_up_{marca}"
    base = {"fuente": "prueba", "entidad": "ENTIDAD", "objeto": "OBRA"}
    try:
        assert await refrescar_licitacion({**base, "id": buena, "monto_referencial": 300000.1,
                                           "monto_adjudicado": 25000000.55})
        assert await refrescar_licitacion({**base, "id": rara, "monto_referencial": 1e15})
        assert await upsert_licitacion({**base, "id": vieja, "monto_referencial": 25000000.55})
        async with connection() as c:
            fila = await c.fetchrow("SELECT * FROM licitaciones WHERE id=$1", buena)
            assert (fila["monto_referencial"], fila["monto_adjudicado"]) == \
                (CON_CENTIMOS, ENORME)
            assert await c.fetchval(
                "SELECT monto_referencial FROM licitaciones WHERE id=$1", rara) is None
            assert await c.fetchval(
                "SELECT monto_referencial FROM licitaciones WHERE id=$1", vieja) == ENORME
    finally:
        await _limpiar([buena, rara, vieja])


@sin_base
async def test_el_filtro_de_monto_incluye_el_borde_exacto(usuario, marca):
    """monto_min = monto de la licitacion: entra. Con REAL, el float del filtro
    y el de la columna no coincidian por un pelo y la dejaban fuera."""
    from shared.db import connection, licitaciones_para_usuario
    lic = f"num_filtro_{marca}"
    try:
        async with connection() as c:
            await _licitacion(c, lic, ENORME)
            await c.execute("UPDATE user_config SET monto_min=$2, monto_max=$3 "
                            "WHERE usuario_id=$1", usuario["id"], ENORME, ENORME)
        ids = {f["id"] for f in await licitaciones_para_usuario(usuario["id"], limite=5000)}
        assert lic in ids
        async with connection() as c:
            await c.execute("UPDATE user_config SET monto_min=$2 WHERE usuario_id=$1",
                            usuario["id"], ENORME + D("0.01"))
        ids = {f["id"] for f in await licitaciones_para_usuario(usuario["id"], limite=5000)}
        assert lic not in ids
    finally:
        await _limpiar([lic])


@sin_base
async def test_el_precio_competitivo_se_calcula_y_guarda_en_decimal(empresa, marca):
    """`monto_ref * 0.92` con un Decimal era un TypeError."""
    from prep_bot.autofill.pricing import calcular_precio_competitivo, estimar_precio_mercado
    from shared.db import connection
    lic = f"num_precio_{marca}"
    try:
        async with connection() as c:
            await _licitacion(c, lic, ENORME, tipo="AS")
            pid = await c.fetchval("INSERT INTO propuestas (licitacion_id, empresa_id) "
                                   "VALUES ($1, $2) RETURNING id", lic, empresa)
            fila = dict(await c.fetchrow("SELECT * FROM licitaciones WHERE id=$1", lic))
        precio = await calcular_precio_competitivo(pid, empresa, fila)
        assert precio == D("23000000.51")             # 92 % de 25,000,000.55, al centimo
        async with connection() as c:
            assert await c.fetchval("SELECT precio_ofertado FROM propuestas WHERE id=$1",
                                    pid) == precio
        # La horquilla, sin historico parecido (objeto unico).
        rango = await estimar_precio_mercado({**fila, "objeto": f"ZZQ{marca}"})
        assert rango["precio_alto"] == ENORME
        assert rango["precio_bajo"] == D("18750000.41")
    finally:
        await _limpiar([lic])


async def _entrar(cliente, usuario):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303


@sin_base
async def test_las_paginas_muestran_los_montos(cliente, usuario, empresa, marca,
                                              tmp_path, monkeypatch):
    """Ficha, propuesta, contratos, informes, CSV y proforma con montos Decimal."""
    import docx

    import win_bot.invoice_gen as ig
    from shared.db import connection
    monkeypatch.setattr(ig, "TEMPLATES_DIR", str(tmp_path))
    lic = f"num_web_{marca}"
    try:
        async with connection() as c:
            await _licitacion(c, lic, ENORME)
            pid = await c.fetchval(
                """INSERT INTO propuestas (licitacion_id, empresa_id, precio_ofertado)
                   VALUES ($1, $2, $3) RETURNING id""", lic, empresa, CON_CENTIMOS)
            cid = await c.fetchval(
                """INSERT INTO contratos (propuesta_id, licitacion_id, empresa_id,
                                          monto_adjudicado, fecha_adjudicacion)
                   VALUES ($1, $2, $3, $4, CURRENT_DATE) RETURNING id""",
                pid, lic, empresa, ENORME)
            pago = await c.fetchval(
                "INSERT INTO pagos (contrato_id, concepto, monto, estado) "
                "VALUES ($1, 'Valorizacion 1', $2, 'pendiente') RETURNING id",
                cid, ENORME)
        await _entrar(cliente, usuario)

        r = await cliente.get(f"/licitacion/{lic}")
        assert r.status_code == 200 and "S/ 25,000,001" in r.text

        r = await cliente.get(f"/propuestas/{pid}")
        assert r.status_code == 200
        assert "S/ 300,000.10" in r.text            # la oferta, al centimo
        # El rango de mercado: antes el TypeError lo borraba de la ficha en silencio.
        assert "Cuartil bajo" in r.text and "S/ 18,750,000" in r.text

        r = await cliente.get("/contratos")
        assert r.status_code == 200 and "S/ 25,000,001" in r.text
        r = await cliente.get(f"/contratos/{cid}")
        assert r.status_code == 200 and "S/ 25,000,000.55" in r.text
        r = await cliente.get("/informes")
        assert r.status_code == 200 and "25,000,001" in r.text
        r = await cliente.get("/informes/cobros.csv")
        assert r.status_code == 200 and "25000000.55" in r.text

        # Un pago registrado desde la web entra exacto.
        r = await cliente.post(f"/contratos/{cid}/pago",
                               data={"concepto": "Valorizacion 2", "monto": "300,000.10"})
        assert r.status_code == 303 and "error=" not in r.headers["location"]
        async with connection() as c:
            assert await c.fetchval(
                "SELECT monto FROM pagos WHERE contrato_id=$1 AND concepto='Valorizacion 2'",
                cid) == CON_CENTIMOS

        # La proforma: antes float(pago["monto"]); ahora el Decimal y su desglose.
        r = await cliente.get(f"/contratos/{cid}/pagos/{pago}/proforma")
        assert r.status_code == 200, r.headers.get("location")
        texto = "\n".join(celda.text for t in docx.Document(io.BytesIO(r.content)).tables
                          for fila in t.rows for celda in fila.cells)
        subtotal, igv = desglose_igv(ENORME)
        assert format_monto(ENORME) in texto
        assert format_monto(subtotal) in texto and format_monto(igv) in texto
    finally:
        await _limpiar([lic])


@sin_base
async def test_el_filtro_de_montos_de_configuracion(cliente, usuario):
    """Antes `float` de FastAPI: "inf" no cabe en NUMERIC (un 500) y "nan" se
    guardaba y vaciaba el radar. Ahora texto, validado y en Decimal."""
    from shared.db import connection, get_config_usuario
    await _entrar(cliente, usuario)
    for malo in ("inf", "nan", "-5", "1e15", "abc"):
        r = await cliente.post("/configuracion/filtros",
                               data={"monto_min": malo, "monto_max": "1000"})
        assert r.status_code == 303 and "error=" in r.headers["location"], malo
    r = await cliente.post("/configuracion/filtros",
                           data={"monto_min": "300000.10", "monto_max": "25000000.55"})
    assert r.status_code == 303 and "aviso=" in r.headers["location"]
    config = await get_config_usuario(usuario["id"])
    assert (config["monto_min"], config["monto_max"]) == (CON_CENTIMOS, ENORME)
    r = await cliente.get("/configuracion")
    assert r.status_code == 200 and 'value="25000001"' in r.text
    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
