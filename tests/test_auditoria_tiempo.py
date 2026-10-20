"""Relojes y secretos: lo que la auditoria del 20/10/2026 encontro roto.

POR QUE ESTAS PRUEBAS FIJAN LA ZONA DE LA SESION

  El fallo mas caro no lo vio ninguna prueba: la suite corre con la sesion de
  Postgres en America/Lima y el VPS la tiene en UTC (POSTGRES_ZONA_SESION). Con
  Lima, `expira > NOW()` funcionaba; con UTC, NOW() va cinco horas por delante
  de la hora de pared que escribe Python, y todo enlace de recuperacion y todo
  codigo de Telegram nacian vencidos. Aqui cada prueba de base corre DOS veces,
  con la sesion en UTC y en Lima, para que la condicion valga en las dos.

LO DEMAS

  - clave_sesion() ya no cae a la clave literal del repo fuera de dev/test.
  - bcrypt 5 lanza ValueError con mas de 72 bytes: se rechaza con un mensaje.
  - Celdas CSV que Excel ejecutaria como formula.
  - Montos de contratos: nan, inf, negativos y 1e30 ya no llegan a la base.
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from shared import fechas
from tests.conftest import sin_base

# ─── Sesion de Postgres en una zona concreta ─────────────────────────────────

ZONAS = ["UTC", "America/Lima"]


@pytest_asyncio.fixture(params=ZONAS)
async def sesion_en(request, monkeypatch):
    """Cambia el pool del proyecto por uno cuya sesion corre en `request.param`.

    Se crea con el mismo get_pool() de produccion y POSTGRES_ZONA_SESION, que
    es exactamente la palanca que usa el VPS: asi se prueba el camino real y
    no una conexion armada a mano que podria comportarse distinto.
    """
    import shared.db as db

    monkeypatch.setenv("POSTGRES_ZONA_SESION", request.param)
    original = db._pool
    db._pool = None
    pool = await db.get_pool()
    try:
        async with db.connection() as c:
            assert await c.fetchval("SHOW timezone") == request.param
        yield request.param
    finally:
        await pool.close()
        db._pool = original


# ─── Token de Telegram ───────────────────────────────────────────────────────

@sin_base
async def test_codigo_de_telegram_recien_creado_vale(usuario, sesion_en, marca):
    from shared.db import set_token_telegram, token_telegram_vigente, vincular_telegram
    from shared.seguridad import nuevo_token_telegram

    token, expira = nuevo_token_telegram()
    await set_token_telegram(usuario["id"], token, expira)

    assert await token_telegram_vigente(usuario["id"]) == token
    chat_id = int(marca, 16) % 10**12 + 10**12
    fila = await vincular_telegram(token, chat_id)
    assert fila is not None and fila["telegram_chat_id"] == chat_id


@sin_base
async def test_codigo_de_telegram_que_vence_en_un_minuto_aun_vale(usuario, sesion_en):
    """El borde que delataba el desfase: con UTC, cualquier vencimiento a menos
    de cinco horas vista ya parecia pasado."""
    from shared.db import set_token_telegram, token_telegram_vigente

    await set_token_telegram(usuario["id"], "casi-vencido-" + usuario["email"],
                             fechas.ahora() + timedelta(minutes=1))
    assert await token_telegram_vigente(usuario["id"]) is not None


@sin_base
async def test_codigo_de_telegram_vencido_no_vale(usuario, sesion_en, marca):
    from shared.db import set_token_telegram, token_telegram_vigente, vincular_telegram

    token = "vencido-" + marca
    await set_token_telegram(usuario["id"], token, fechas.ahora() - timedelta(minutes=1))

    assert await token_telegram_vigente(usuario["id"]) is None
    assert await vincular_telegram(token, int(marca, 16) % 10**12 + 2 * 10**12) is None


# ─── Recuperacion de contrasena ──────────────────────────────────────────────

@sin_base
async def test_enlace_de_recuperacion_recien_creado_vale(usuario, sesion_en):
    from shared.db import (
        consumir_token_y_cambiar_password,
        crear_token_recuperacion,
        usuario_por_token_recuperacion,
    )
    from shared.seguridad import hashear_password, nuevo_token_recuperacion

    _, token_hash, expira = nuevo_token_recuperacion()
    await crear_token_recuperacion(usuario["id"], token_hash, expira)

    fila = await usuario_por_token_recuperacion(token_hash)
    assert fila is not None and fila["id"] == usuario["id"]
    assert await consumir_token_y_cambiar_password(
        token_hash, hashear_password("OtraClaveDePrueba456"))
    # Un solo uso.
    assert await usuario_por_token_recuperacion(token_hash) is None


@sin_base
async def test_enlace_de_recuperacion_vencido_no_vale(usuario, sesion_en):
    from shared.db import (
        consumir_token_y_cambiar_password,
        crear_token_recuperacion,
        usuario_por_token_recuperacion,
    )
    from shared.seguridad import hash_token, hashear_password

    token_hash = hash_token("vencido-" + usuario["email"])
    await crear_token_recuperacion(usuario["id"], token_hash,
                                   fechas.ahora() - timedelta(minutes=1))

    assert await usuario_por_token_recuperacion(token_hash) is None
    assert not await consumir_token_y_cambiar_password(
        token_hash, hashear_password("OtraClaveDePrueba456"))


# ─── Fechas de Lima en consultas ─────────────────────────────────────────────

@sin_base
async def test_el_mes_de_la_cuota_de_ia_empieza_el_dia_1_de_lima(sesion_en):
    from shared.db import connection
    from shared.ia import _INICIO_MES_LIMA

    async with connection() as c:
        inicio = await c.fetchval(f"SELECT {_INICIO_MES_LIMA}")
    hoy = fechas.hoy()
    assert inicio == datetime(hoy.year, hoy.month, 1)


@sin_base
async def test_vencimientos_cuentan_desde_la_fecha_de_lima(usuario, empresa, sesion_en):
    """La ventana de aviso (60 dias) se cuenta desde el hoy de Lima: lo que
    vence justo en el limite entra, y lo del dia siguiente no."""
    from shared.db import connection
    from web.informes import DIAS_AVISO_VENCIMIENTO, proximos_vencimientos

    limite = fechas.hoy() + timedelta(days=DIAS_AVISO_VENCIMIENTO)
    async with connection() as c:
        await c.execute("UPDATE empresas SET rnp_vigencia=$2 WHERE id=$1", empresa, limite)
        await c.execute(
            """INSERT INTO vencimientos (empresa_id, tipo, descripcion, fecha_vencimiento)
               VALUES ($1, 'poliza', 'fuera de la ventana', $2)""",
            empresa, limite + timedelta(days=1))

    filas = await proximos_vencimientos(usuario["id"])
    assert [f["tipo"] for f in filas] == ["RNP"]
    assert filas[0]["dias"] == DIAS_AVISO_VENCIMIENTO


# ─── clave_sesion ────────────────────────────────────────────────────────────

def test_clave_sesion_exige_la_variable_en_produccion(monkeypatch):
    from shared.seguridad import clave_sesion

    monkeypatch.delenv("LICITAPRO_SECRET_KEY", raising=False)
    monkeypatch.setenv("LICITAPRO_ENTORNO", "produccion")
    with pytest.raises(RuntimeError):
        clave_sesion()


@pytest.mark.parametrize("entorno", ["dev", "test", ""])
def test_clave_sesion_tolera_la_falta_en_desarrollo(monkeypatch, entorno):
    from shared.seguridad import clave_sesion

    monkeypatch.delenv("LICITAPRO_SECRET_KEY", raising=False)
    monkeypatch.setenv("LICITAPRO_ENTORNO", entorno)
    assert len(clave_sesion()) == 64


def test_clave_sesion_se_deriva_y_no_es_la_variable_en_crudo(monkeypatch):
    """La misma variable alimenta Fernet; la cookie firma con otra clave."""
    from shared.seguridad import clave_sesion

    monkeypatch.setenv("LICITAPRO_SECRET_KEY", "un-secreto-cualquiera")
    monkeypatch.setenv("LICITAPRO_ENTORNO", "produccion")
    clave = clave_sesion()
    assert clave != "un-secreto-cualquiera"
    assert clave == clave_sesion()  # estable: no cierra sesiones en cada arranque
    monkeypatch.setenv("LICITAPRO_SECRET_KEY", "otro-secreto")
    assert clave_sesion() != clave


def test_cifrar_no_cambia_de_clave(monkeypatch):
    """Lo cifrado antes de este cambio se tiene que seguir leyendo."""
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    from shared.seguridad import descifrar

    monkeypatch.setenv("LICITAPRO_SECRET_KEY", "secreto-de-siempre")
    clave_vieja = base64.urlsafe_b64encode(hashlib.sha256(b"secreto-de-siempre").digest())
    assert descifrar(Fernet(clave_vieja).encrypt(b"clave-sol")) == "clave-sol"


# ─── Contrasenas de mas de 72 bytes ──────────────────────────────────────────

def test_contrasena_de_mas_de_72_bytes_se_rechaza_con_mensaje():
    from shared.seguridad import MOTIVO_PASSWORD_LARGA, hashear_password, password_debil

    larga = "UnaFraseMuyLarga-" * 5  # 85 bytes
    assert password_debil(larga) == MOTIVO_PASSWORD_LARGA
    with pytest.raises(ValueError, match="mas corta"):
        hashear_password(larga)


def test_el_limite_es_de_bytes_y_no_de_caracteres():
    """70 caracteres con tildes pasan de 72 bytes en UTF-8."""
    from shared.seguridad import MOTIVO_PASSWORD_LARGA, password_debil

    con_tildes = "contraseña" + "á" * 60
    assert len(con_tildes) < 72 < len(con_tildes.encode("utf-8"))
    assert password_debil(con_tildes) == MOTIVO_PASSWORD_LARGA
    assert password_debil("x" * 72 + "") is None


def test_verificar_una_contrasena_larga_da_false_y_no_revienta():
    from shared.seguridad import hashear_password, verificar_password

    guardado = hashear_password("x" * 72)
    assert verificar_password("x" * 72, guardado)
    # Con bcrypt 4 esto casaba (truncaba en silencio); con el 5 era ValueError.
    assert not verificar_password("x" * 72 + "y", guardado)


def test_los_hashes_existentes_siguen_valiendo():
    """Sin prehash: un hash bcrypt de los de siempre se verifica igual."""
    import bcrypt

    from shared.seguridad import verificar_password

    viejo = bcrypt.hashpw(b"ClaveDePrueba123!", bcrypt.gensalt()).decode()
    assert verificar_password("ClaveDePrueba123!", viejo)
    assert not verificar_password("ClaveDePrueba124!", viejo)


# ─── CSV con formulas ────────────────────────────────────────────────────────

@pytest.mark.parametrize("peligrosa", ["=1+1", "+51 999", "-2+3", "@SUMA(A1)",
                                       "\t=1", "\r=1",
                                       '=HYPERLINK("http://x","clic")'])
def test_celda_que_empieza_como_formula_se_neutraliza(peligrosa):
    from web.informes import _celda_segura

    assert _celda_segura(peligrosa) == "'" + peligrosa


@pytest.mark.parametrize("inocua", ["Municipalidad", "", "15000.50", 15000.5, -3, None])
def test_celda_normal_no_se_toca(inocua):
    from web.informes import _celda_segura

    assert _celda_segura(inocua) == inocua


async def test_el_csv_sale_con_las_celdas_neutralizadas():
    from web.informes import _csv

    respuesta = _csv([["=cmd|' /C calc'!A0", -250.0, "Obra"]],
                     ["Concepto", "Monto", "Objeto"], "x.csv")
    cuerpo = "".join([trozo async for trozo in respuesta.body_iterator])
    linea = cuerpo.splitlines()[1]
    assert linea.startswith("\"'=cmd") or linea.startswith("'=cmd")
    assert ";-250.0;" in linea


# ─── Montos de contratos ─────────────────────────────────────────────────────

@pytest.mark.parametrize("texto,esperado", [
    ("15000.50", 15000.5), ("1500,50", 1500.5), ("15,000.50", 15000.5),
    ("1.500,50", 1500.5), (" 0 ", 0.0), ("1000000000", 1_000_000_000.0),
])
def test_montos_aceptados(texto, esperado):
    from web.contratos import _leer_monto

    assert _leer_monto(texto) == esperado


@pytest.mark.parametrize("texto", ["nan", "NaN", "inf", "-inf", "-5", "1e30",
                                   "1000000000.01", "abc", "", "1.2.3"])
def test_montos_rechazados(texto):
    from web.contratos import _leer_monto

    assert _leer_monto(texto) is None


async def _entrar(cliente, usuario):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303


@sin_base
@pytest.mark.parametrize("monto,plazo", [("nan", "30"), ("-5", "30"), ("1e30", "30"),
                                         ("abc", "30"), ("1000", "99999999"),
                                         ("1000", "-1"), ("1000", "x")])
async def test_buena_pro_con_datos_raros_responde_con_mensaje(usuario, cliente, monto, plazo):
    await _entrar(cliente, usuario)
    r = await cliente.post("/contratos/ganar",
                           data={"propuesta_id": "1", "monto": monto, "plazo_dias": plazo})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/contratos?error=")
    assert "no+valid" in r.headers["location"]


@sin_base
async def test_cobro_con_monto_nan_responde_con_mensaje(usuario, empresa, cliente):
    from shared.db import connection

    async with connection() as c:
        contrato = await c.fetchval(
            """INSERT INTO contratos (empresa_id, monto_adjudicado, estado)
               VALUES ($1, 1000, 'adjudicado') RETURNING id""", empresa)
    await _entrar(cliente, usuario)
    r = await cliente.post(f"/contratos/{contrato}/pago",
                           data={"concepto": "Adelanto", "monto": "nan"})
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/contratos/{contrato}?error=Monto")
    async with connection() as c:
        assert await c.fetchval("SELECT COUNT(*) FROM pagos WHERE contrato_id=$1",
                                contrato) == 0
