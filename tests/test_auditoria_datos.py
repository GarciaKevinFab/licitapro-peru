"""Hallazgos de la auditoria de octubre de 2026 sobre datos y esquema.

    python -m pytest tests/test_auditoria_datos.py -q

Cada prueba fija un fallo que paso de verdad y que no hacia ruido: un monto
inventado a partir de un correlativo, un precio que subia en una "rebaja", un
total que sobrevivia a los items de los que salia, dos propuestas por un doble
clic, y un blindaje de pruebas que daba por local a Supabase.
"""
import ast
import asyncio
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import pytest

from shared import cotizacion
from shared.config import parse_monto
from tests.conftest import motivo_para_abortar, sin_base

RAIZ = Path(__file__).resolve().parent.parent
D = Decimal


# ─── parse_monto: "S/" solo cuando es la moneda ──────────

@pytest.mark.parametrize("texto", [
    "COT-123-2026-MDS/GM",          # el caso de la auditoria: salia S/ 123
    "Bases/ 500 hojas",
    "ANEXOS/ 2026 4500",
    "OFICIO 145-2026-GRMDD/GRI",
    "COT-123-2026",
    "21/08/2026",
    "3000",
])
def test_parse_monto_no_inventa_montos(texto):
    assert parse_monto(texto) is None


@pytest.mark.parametrize("texto, esperado", [
    ("S/ 5000", 5000.0),
    ("S/5000", 5000.0),
    ("S/.5000", 5000.0),
    ("S/. 12,115.20", 12115.20),
    ("s/ 1,234.56", 1234.56),
    ("Monto: S/ 45,000.00", 45000.0),
    ("(S/ 2500)", 2500.0),
    ("1,234.56", 1234.56),
    ("12.345,67", 12345.67),
    ("S/ 1.234.567,89", 1234567.89),
    ("US$ 2500", 2500.0),
    ("PEN 3000", 3000.0),
    ("3000 soles", 3000.0),
    (15000, 15000.0),
])
def test_parse_monto_sigue_leyendo_los_formatos_buenos(texto, esperado):
    assert parse_monto(texto) == pytest.approx(esperado)


# ─── cotizacion: rango, rebaja y total ───────────────────

@pytest.mark.parametrize("crudo", [
    "1E+30", "1e10", "10000000000", "9999999999.99999", "nan", "-inf", "Infinity",
    "1" * 40,
])
def test_a_decimal_rechaza_lo_que_no_cabe_en_la_columna(crudo):
    """Antes "1E+30" pasaba y Postgres respondia con un 500 (numeric overflow)."""
    assert cotizacion.a_decimal(crudo) is None


def test_a_decimal_acepta_el_borde_que_si_cabe():
    assert cotizacion.a_decimal("9999999999.9999") == D("9999999999.9999")
    assert cotizacion.a_decimal("0.125") == D("0.125")


@pytest.mark.parametrize("valor", [D("1E+30"), D("NaN"), D("Infinity"), "abc"])
def test_validar_rango_lanza_un_valueerror_controlado(valor):
    with pytest.raises(cotizacion.FueraDeRango) as e:
        cotizacion.validar_rango(valor, "precio")
    assert isinstance(e.value, ValueError)


def test_validar_rango_deja_pasar_lo_normal_y_el_vacio():
    cotizacion.validar_rango(None)
    cotizacion.validar_rango(D("12115.20"))


def test_una_rebaja_nunca_sube_un_precio():
    """Un item de S/ 0.30 tiene paso de 0.50: el piso lo subia a 0.50."""
    items = [
        {"id": 1, "cantidad": D(10), "precio_unitario": D("0.30")},
        {"id": 2, "cantidad": D(3), "precio_unitario": D("0.45")},
        {"id": 3, "cantidad": D(100), "precio_unitario": D("25.00")},
    ]
    nuevos = cotizacion.precios_rebajados(items, D(2))
    for it in items:
        assert nuevos[it["id"]] <= it["precio_unitario"], it
    assert cotizacion.total([{**it, "precio_unitario": nuevos[it["id"]]} for it in items]) \
        <= cotizacion.total(items)


async def _propuesta_con_items(marca, empresa, items):
    from shared.db import connection
    lic = f"prueba_audit_{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, tipo, entidad, objeto)
               VALUES ($1, 'gore_portals', 'cotizacion', 'ENTIDAD', 'Prueba auditoria')""",
            lic)
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'iniciado') RETURNING id", lic, empresa)
        for n, (cant, precio) in enumerate(items, 1):
            await c.execute(
                "INSERT INTO propuesta_items (propuesta_id, orden, descripcion, cantidad, "
                "precio_unitario) VALUES ($1, $2, $3, $4, $5)",
                pid, n, f"ITEM {n}", cant, precio)
    return lic, pid


async def _limpiar(lic):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lic)
        await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


@sin_base
async def test_quitar_el_ultimo_item_con_precio_deja_el_total_en_null(empresa, marca):
    from shared.db import connection
    lic, pid = await _propuesta_con_items(marca, empresa, [(D(2), D("4.50")), (D(1), None)])
    try:
        async with connection() as c:
            await cotizacion._recalcular(c, pid)
            assert await c.fetchval(
                "SELECT precio_ofertado FROM propuestas WHERE id=$1", pid) == pytest.approx(9.0)
            con_precio = await c.fetchval(
                "SELECT id FROM propuesta_items WHERE propuesta_id=$1 AND orden=1", pid)
        await cotizacion.quitar(pid, con_precio)
        async with connection() as c:
            assert await c.fetchval(
                "SELECT precio_ofertado FROM propuestas WHERE id=$1", pid) is None
    finally:
        await _limpiar(lic)


@sin_base
async def test_quitar_un_item_sin_precio_respeta_el_monto_puesto_a_mano(empresa, marca):
    """Una propuesta sin precios por item conserva el precio que fijo el usuario."""
    from shared.db import connection
    lic, pid = await _propuesta_con_items(marca, empresa, [(D(1), None), (D(1), None)])
    try:
        async with connection() as c:
            await c.execute("UPDATE propuestas SET precio_ofertado=1500 WHERE id=$1", pid)
            uno = await c.fetchval(
                "SELECT id FROM propuesta_items WHERE propuesta_id=$1 AND orden=1", pid)
        await cotizacion.quitar(pid, uno)
        async with connection() as c:
            assert await c.fetchval(
                "SELECT precio_ofertado FROM propuestas WHERE id=$1", pid) == pytest.approx(1500)
    finally:
        await _limpiar(lic)


@sin_base
async def test_agregar_a_la_vez_no_choca_con_el_orden(empresa, marca):
    """MAX(orden)+1 en dos sentencias: dos altas simultaneas daban un 500."""
    from shared.db import connection
    lic, pid = await _propuesta_con_items(marca, empresa, [(D(1), D(1))])
    try:
        await asyncio.gather(*(cotizacion.agregar(pid, f"NUEVO {n}", D(n), "UND")
                               for n in range(1, 7)))
        async with connection() as c:
            ordenes = [r["orden"] for r in await c.fetch(
                "SELECT orden FROM propuesta_items WHERE propuesta_id=$1 ORDER BY orden", pid)]
        assert ordenes == list(range(1, 8))
    finally:
        await _limpiar(lic)


@sin_base
async def test_agregar_rechaza_una_cantidad_que_no_cabe(empresa, marca):
    lic, pid = await _propuesta_con_items(marca, empresa, [])
    try:
        with pytest.raises(cotizacion.FueraDeRango):
            await cotizacion.agregar(pid, "ENORME", D("1E+30"), None)
        with pytest.raises(cotizacion.FueraDeRango):
            await cotizacion.guardar(pid, {1: ("", D("1E+30"))}, {})
    finally:
        await _limpiar(lic)


# ─── Esquema: una propuesta por licitacion y empresa ─────

@sin_base
async def test_no_puede_haber_dos_propuestas_de_la_misma_empresa(empresa, marca):
    """Migracion 0020. Un doble clic en "Postular" creaba dos propuestas."""
    import asyncpg

    from shared.db import connection
    lic, _ = await _propuesta_con_items(marca, empresa, [])
    try:
        async with connection() as c:
            with pytest.raises(asyncpg.UniqueViolationError):
                await c.execute(
                    "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
                    "VALUES ($1, $2, 'iniciado')", lic, empresa)
    finally:
        await _limpiar(lic)


def test_la_migracion_0020_cuelga_de_la_0019_y_esta_en_schema_sql():
    texto = (RAIZ / "migrations" / "versions" / "0020_propuesta_unica.py").read_text(
        encoding="utf-8")
    assert "revision = '0020'" in texto and "down_revision = '0019'" in texto
    esquema = (RAIZ / "shared" / "schema.sql").read_text(encoding="utf-8")
    assert "uq_propuestas_licitacion_empresa" in esquema
    assert "uq_propuestas_licitacion_empresa" in texto


# ─── Alembic: TLS solo donde hace falta, sin contrasena por defecto ──────────

def _url_bd_de_env_py():
    """`url_bd` de migrations/env.py, sin ejecutar el modulo (que lanza Alembic).

    Se compilan solo las funciones que la componen, con el `_es_gestionado`
    verdadero de shared.db: lo que se prueba es exactamente el codigo del
    archivo, no una copia.
    """
    from shared.db import _es_gestionado
    arbol = ast.parse((RAIZ / "migrations" / "env.py").read_text(encoding="utf-8"))
    funciones = [n for n in arbol.body if isinstance(n, ast.FunctionDef)
                 and n.name in {"url_bd", "_password_obligatoria"}]
    assert len(funciones) == 2
    espacio = {"os": os, "urlparse": urlparse, "_es_gestionado": _es_gestionado}
    exec(compile(ast.Module(body=funciones, type_ignores=[]), "env.py", "exec"), espacio)  # noqa: S102 - codigo del propio repo (migrations/env.py), no entrada externa
    return espacio["url_bd"]


@pytest.fixture
def entorno_bd(monkeypatch):
    for clave in ("DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
                  "POSTGRES_USER", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(clave, raising=False)
    return monkeypatch


def test_alembic_no_fuerza_tls_contra_el_postgres_del_vps(entorno_bd):
    entorno_bd.setenv("DATABASE_URL", "postgresql://licitapro:x@postgres:5432/licitapro")
    url = _url_bd_de_env_py()()
    assert url.startswith("postgresql+psycopg2://")
    assert "sslmode" not in url


def test_alembic_exige_tls_contra_una_base_gestionada(entorno_bd):
    entorno_bd.setenv("DATABASE_URL",
                      "postgresql://postgres.abc:x@aws-0-sa-east-1.pooler.supabase.com:6543/postgres")
    assert _url_bd_de_env_py()().endswith("?sslmode=require")


def test_alembic_respeta_el_sslmode_que_trae_la_url(entorno_bd):
    entorno_bd.setenv("DATABASE_URL", "postgresql://u:x@remoto.example.com/db?sslmode=disable")
    assert _url_bd_de_env_py()().count("sslmode") == 1


def test_alembic_no_inventa_la_contrasena(entorno_bd):
    url_bd = _url_bd_de_env_py()
    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD"):
        url_bd()
    entorno_bd.setenv("POSTGRES_PASSWORD", "secreta")
    entorno_bd.setenv("POSTGRES_PORT", "5434")
    url = url_bd()
    assert ":secreta@localhost:5434/" in url and "sslmode" not in url


# ─── Blindaje de la suite ────────────────────────────────

@pytest.mark.parametrize("host, puerto", [
    ("db.abcdefghijkl.supabase.co", 5432),    # empieza por "db": antes era "local"
    ("postgres.miempresa.com", 5432),         # empieza por "postgres"
    ("localhost.evil.com", 5432),
    ("127.0.0.2", 5434),
    ("203.0.113.10", 5432),
    ("", 5434),
    ("localhost", 5442),                      # tunel SSH a produccion
    ("127.0.0.1", 15432),                     # el mismo tunel en la PC puente
])
def test_el_blindaje_rechaza_lo_que_no_es_local(host, puerto):
    assert motivo_para_abortar(host, puerto)


@pytest.mark.parametrize("host, puerto", [
    ("localhost", 5434), ("127.0.0.1", 5432), ("::1", 5434), ("postgres", 5432),
    ("db", 5432), ("LOCALHOST", 5434),
])
def test_el_blindaje_deja_pasar_lo_local(host, puerto):
    assert motivo_para_abortar(host, puerto) is None
