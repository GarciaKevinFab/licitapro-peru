"""Auditoria de cuentas: correo verificado, sesiones del checkout y del admin,
TLS hacia la base y cambio de contrasena.

LO QUE SE FIJA AQUI

  1. Una cuenta nace sin verificar, recibe un enlace de un solo uso (solo su
     hash en la base, 48 h, hora de Lima) y el rol de administrador exige el
     correo verificado. Antes, el primero que registrara LICITAPRO_ADMIN_EMAIL
     era administrador.
  2. El alta desde /comprar abre la sesion por `iniciar_sesion` (sin heredar
     la cookie anterior) y un correo ya registrado no se confirma a quien
     pregunta: texto generico y aviso al dueno del correo.
  3. La suplantacion del admin guarda y restaura la huella del dueno: cambiar
     su contrasena cierra tambien la sesion metida en la cuenta de un cliente.
  4. `_es_gestionado` decide el TLS por lista blanca EXACTA de hosts locales:
     `db.<ref>.supabase.co` ya no cuenta como local.
  5. Cambiar la contrasena desde /configuracion usa `password_debil`.
"""
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote_plus

import pytest

from tests.conftest import sin_base

RAIZ = Path(__file__).resolve().parent.parent


# ─── 4. TLS por lista blanca exacta ──────────────────────

@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "postgres", "db",
                                  "LOCALHOST", " postgres ", ""])
def test_hosts_locales_van_sin_tls(host, monkeypatch):
    from shared.db import _es_gestionado
    monkeypatch.delenv("POSTGRES_SSLMODE", raising=False)
    assert not _es_gestionado(host)


@pytest.mark.parametrize("host", ["db.abcdefgh.supabase.co", "postgres.ejemplo.com",
                                  "localhost.ejemplo.com", "127.0.0.1.nip.io",
                                  "aws-0-sa-east-1.pooler.supabase.com", "dbserver"])
def test_todo_lo_demas_exige_tls(host, monkeypatch):
    """`db.<ref>.supabase.co` empieza por "db": con prefijos contaba como local."""
    from shared.db import _es_gestionado
    monkeypatch.delenv("POSTGRES_SSLMODE", raising=False)
    assert _es_gestionado(host)


def test_sslmode_disable_explicito_apaga_el_tls(monkeypatch):
    from shared.db import _es_gestionado
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
    assert not _es_gestionado("db.abcdefgh.supabase.co")


def test_el_puerto_por_defecto_no_es_el_5433():
    """El 5433 lo ocupa otro proyecto en la maquina de desarrollo."""
    from shared import db
    assert db.PUERTO_POR_DEFECTO == "5432"
    for ruta in ("shared/db.py", "migrations/env.py", "tools/crear_esquema.py",
                 "tools/auditar_sql.py"):
        codigo = (RAIZ / ruta).read_text(encoding="utf-8")
        assert "'POSTGRES_PORT', '5433'" not in codigo, ruta
        assert '"POSTGRES_PORT", "5433"' not in codigo, ruta


# ─── 1. La migracion ─────────────────────────────────────

def test_la_migracion_0022_cuelga_de_la_0021_y_es_idempotente():
    texto = (RAIZ / "migrations" / "versions" / "0022_correo_verificado.py").read_text(
        encoding="utf-8")
    assert "revision = '0022'" in texto and "down_revision = '0021'" in texto
    assert "CREATE TABLE IF NOT EXISTS tokens_verificacion" in texto
    # El relleno de las cuentas viejas solo corre si la columna no existia.
    assert 'if not _hay_columna("usuarios", "email_verificado_en")' in texto
    assert "America/Lima" in texto


# ─── Utilidades ──────────────────────────────────────────

async def _entrar(cliente, usuario, password=None):
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": password or usuario["password"]})
    assert r.status_code == 303, r.text[:300]


async def _verificado(usuario_id) -> bool:
    from shared.db import connection
    async with connection() as c:
        return bool(await c.fetchval(
            "SELECT email_verificado_en FROM usuarios WHERE id = $1", usuario_id))


async def _marcar_verificado(usuario_id) -> None:
    from shared.db import connection
    async with connection() as c:
        await c.execute("UPDATE usuarios SET email_verificado_en = NOW() WHERE id = $1",
                        usuario_id)


async def _tokens(usuario_id) -> int:
    from shared.db import connection
    async with connection() as c:
        return await c.fetchval(
            "SELECT COUNT(*) FROM tokens_verificacion WHERE usuario_id = $1", usuario_id)


async def _token_para(usuario_id, email, desfase=timedelta(0)) -> str:
    from shared.db import crear_token_verificacion
    from web.auth import nuevo_token_verificacion
    token, token_hash, expira = nuevo_token_verificacion()
    await crear_token_verificacion(usuario_id, email, token_hash, expira + desfase)
    return token


@pytest.fixture
def sin_smtp(monkeypatch):
    """Recoge los correos en vez de mandarlos. [(destinatario, asunto, texto)]"""
    import shared.email_sender as es
    enviados = []

    async def falso(destinatario, asunto, html, texto=""):
        enviados.append((destinatario, asunto, texto))
        return True

    monkeypatch.setattr(es, "enviar_email", falso)
    return enviados


@pytest.fixture
async def otro_usuario(marca):
    from shared.db import borrar_cuenta, crear_usuario
    from shared.seguridad import hashear_password
    email = f"prueba-otro-{marca}@ejemplo.pe"
    fila = await crear_usuario(email, hashear_password("ClaveDePrueba123!"), "Otro")
    try:
        yield {"id": fila["id"], "email": email, "password": "ClaveDePrueba123!"}
    finally:
        await borrar_cuenta(fila["id"])


# ─── 1. Verificacion del correo ──────────────────────────

@sin_base
async def test_el_registro_nace_sin_verificar_y_manda_el_enlace(cliente, marca, sin_smtp):
    from shared.db import borrar_cuenta, get_usuario_por_email
    correo = f"prueba-alta-{marca}@ejemplo.pe"
    r = await cliente.post("/registro", data={"email": correo,
                                              "password": "ClaveDePrueba123!"})
    fila = await get_usuario_por_email(correo)
    try:
        assert r.status_code == 303
        assert fila and not fila["email_verificado_en"]
        assert await _tokens(fila["id"]) == 1
        assert [d for d, _, _ in sin_smtp] == [correo]
        texto = sin_smtp[0][2]
        assert "/verificar/" in texto
        # El panel avisa mientras tanto.
        r = await cliente.get("/configuracion")
        assert "Confirma tu correo" in r.text

        # El enlace del correo, abierto (sin sesion: otro dispositivo).
        enlace = next(p for p in texto.split() if "/verificar/" in p)
        ruta = enlace[enlace.index("/verificar/"):]
        cliente.cookies.clear()
        r = await cliente.get(ruta)
        assert r.status_code == 200 and "Correo confirmado" in r.text
        assert await _verificado(fila["id"])
        # De un solo uso.
        assert (await cliente.get(ruta)).status_code == 400
    finally:
        if fila:
            await borrar_cuenta(fila["id"])


@sin_base
async def test_en_la_base_solo_queda_el_hash_del_enlace(usuario):
    from shared.db import connection
    token = await _token_para(usuario["id"], usuario["email"])
    async with connection() as c:
        guardado = await c.fetchval(
            "SELECT token_hash FROM tokens_verificacion WHERE usuario_id = $1",
            usuario["id"])
    assert guardado and guardado != token and len(guardado) == 64


@sin_base
async def test_un_enlace_vencido_no_verifica(cliente, usuario):
    token = await _token_para(usuario["id"], usuario["email"], -timedelta(hours=49))
    assert (await cliente.get(f"/verificar/{token}")).status_code == 400
    assert not await _verificado(usuario["id"])


@sin_base
async def test_el_enlace_vale_48_horas_en_hora_de_lima(cliente, usuario):
    """Recien creado tiene que valer, con la sesion de la base en UTC o en Lima.

    Es el fallo que ya tuvieron la recuperacion y Telegram: comparar contra
    NOW() a secas hacia nacer vencidos los enlaces en el VPS (sesion en UTC).
    """
    from web.auth import HORAS_TOKEN_VERIFICACION
    assert HORAS_TOKEN_VERIFICACION == 48
    token = await _token_para(usuario["id"], usuario["email"], -timedelta(hours=47))
    assert (await cliente.get(f"/verificar/{token}")).status_code == 200
    assert await _verificado(usuario["id"])


@sin_base
async def test_un_enlace_del_correo_viejo_no_verifica_el_nuevo(cliente, usuario, marca):
    from shared.admin_cuentas import editar_cuenta
    token = await _token_para(usuario["id"], usuario["email"])
    assert await editar_cuenta(usuario["id"], "X", f"prueba-nuevo-{marca}@ejemplo.pe",
                               True) is None
    assert (await cliente.get(f"/verificar/{token}")).status_code == 400
    assert not await _verificado(usuario["id"])


@sin_base
async def test_cambiar_el_correo_a_mano_quita_la_verificacion(usuario, marca):
    from shared.admin_cuentas import editar_cuenta
    await _marcar_verificado(usuario["id"])
    # Mismo correo (otra caja): se conserva.
    assert await editar_cuenta(usuario["id"], "X", usuario["email"].upper(), True) is None
    assert await _verificado(usuario["id"])
    # Correo distinto: se pierde.
    assert await editar_cuenta(usuario["id"], "X", f"prueba-cambio-{marca}@ejemplo.pe",
                               True) is None
    assert not await _verificado(usuario["id"])


@sin_base
async def test_reenviar_tiene_tope_por_hora(cliente, usuario, sin_smtp):
    from web.auth import MAX_VERIFICACIONES_POR_HORA
    await _entrar(cliente, usuario)
    for _ in range(MAX_VERIFICACIONES_POR_HORA):
        r = await cliente.post("/verificar/reenviar")
        assert "aviso=" in r.headers["location"]
    r = await cliente.post("/verificar/reenviar")
    assert "error=" in r.headers["location"]
    assert await _tokens(usuario["id"]) == MAX_VERIFICACIONES_POR_HORA
    assert len(sin_smtp) == MAX_VERIFICACIONES_POR_HORA


@sin_base
async def test_verificado_no_ve_la_franja_ni_reenvia(cliente, usuario, sin_smtp):
    await _marcar_verificado(usuario["id"])
    await _entrar(cliente, usuario)
    assert "Confirma tu correo" not in (await cliente.get("/configuracion")).text
    await cliente.post("/verificar/reenviar")
    assert sin_smtp == []


@sin_base
async def test_el_admin_exige_el_correo_verificado(cliente, usuario, monkeypatch):
    """El primero que registrara el correo del dueno ya no se queda con /admin."""
    from web.app import app
    monkeypatch.setenv("LICITAPRO_ADMIN_EMAIL", usuario["email"])
    # El enlace del menu lee el correo del dueno de app.state (se fija al arrancar).
    monkeypatch.setattr(app.state, "admin_email", usuario["email"], raising=False)
    await _entrar(cliente, usuario)
    assert (await cliente.get("/admin/clientes")).status_code == 404
    assert "/admin/clientes" not in (await cliente.get("/configuracion")).text
    await _marcar_verificado(usuario["id"])
    assert (await cliente.get("/admin/clientes")).status_code == 200
    assert "/admin/clientes" in (await cliente.get("/configuracion")).text


def test_correo_verificado_falla_cerrado():
    from web.auth import correo_verificado
    assert not correo_verificado(None)
    assert not correo_verificado({"email": "a@b.pe"})  # base anterior a la 0022
    assert not correo_verificado({"email_verificado_en": None})
    assert correo_verificado({"email_verificado_en": "2026-10-20"})


# ─── 3. Suplantacion del admin y huella ──────────────────

@sin_base
async def test_entrar_como_y_salir_conservan_la_sesion_del_dueno(
        cliente, usuario, otro_usuario, monkeypatch):
    monkeypatch.setenv("LICITAPRO_ADMIN_EMAIL", usuario["email"])
    await _marcar_verificado(usuario["id"])
    await _entrar(cliente, usuario)

    r = await cliente.post(f"/admin/clientes/{otro_usuario['id']}/entrar")
    assert r.status_code == 303 and r.headers["location"] == "/panel"
    r = await cliente.get("/configuracion")
    assert r.status_code == 200 and otro_usuario["email"] in r.text
    # Desde dentro el panel de admin sigue respondiendo.
    assert (await cliente.get("/admin/clientes")).status_code == 200

    r = await cliente.post("/admin/salir")
    assert r.headers["location"] == f"/admin/clientes/{otro_usuario['id']}"
    assert (await cliente.get("/admin/clientes")).status_code == 200


@sin_base
async def test_si_el_dueno_cambia_su_contrasena_cae_tambien_la_suplantacion(
        cliente, usuario, otro_usuario, monkeypatch):
    from shared.admin_cuentas import cambiar_password
    from shared.seguridad import hashear_password
    monkeypatch.setenv("LICITAPRO_ADMIN_EMAIL", usuario["email"])
    await _marcar_verificado(usuario["id"])
    await _entrar(cliente, usuario)
    await cliente.post(f"/admin/clientes/{otro_usuario['id']}/entrar")

    assert await cambiar_password(usuario["id"], hashear_password("OtraClave456!x"))
    assert (await cliente.get("/admin/clientes")).status_code == 404
    # Y la sesion entera se cerro: ya no esta ni dentro del cliente.
    r = await cliente.get("/configuracion")
    assert r.status_code == 303 and r.headers["location"].startswith("/entrar")


@sin_base
async def test_salir_tras_cambio_de_contrasena_del_dueno_no_devuelve_el_admin(
        cliente, usuario, otro_usuario, monkeypatch):
    from shared.admin_cuentas import cambiar_password
    from shared.seguridad import hashear_password
    monkeypatch.setenv("LICITAPRO_ADMIN_EMAIL", usuario["email"])
    await _marcar_verificado(usuario["id"])
    await _entrar(cliente, usuario)
    await cliente.post(f"/admin/clientes/{otro_usuario['id']}/entrar")
    assert await cambiar_password(usuario["id"], hashear_password("OtraClave456!x"))

    await cliente.post("/admin/salir")
    r = await cliente.get("/configuracion")
    assert r.status_code == 303 and r.headers["location"].startswith("/entrar")


@sin_base
async def test_si_el_cliente_cambia_su_contrasena_cae_la_suplantacion_en_el(
        cliente, usuario, otro_usuario, monkeypatch):
    from shared.admin_cuentas import cambiar_password
    from shared.seguridad import hashear_password
    monkeypatch.setenv("LICITAPRO_ADMIN_EMAIL", usuario["email"])
    await _marcar_verificado(usuario["id"])
    await _entrar(cliente, usuario)
    await cliente.post(f"/admin/clientes/{otro_usuario['id']}/entrar")
    assert await cambiar_password(otro_usuario["id"], hashear_password("OtraClave456!x"))
    r = await cliente.get("/configuracion")
    assert r.status_code == 303


# ─── 2. /comprar: sesion nueva y correo existente sin confirmarlo ────────────

@sin_base
async def test_el_alta_en_el_checkout_pasa_por_iniciar_sesion(
        cliente, marca, monkeypatch, sin_smtp):
    from fastapi.responses import RedirectResponse

    from shared.db import borrar_cuenta, get_usuario_por_email
    from web import comprar

    llamadas = []
    original = comprar.iniciar_sesion

    def espia(request, fila):
        llamadas.append(fila["id"])
        original(request, fila)

    async def cobro_falso(request, usuario, **kw):
        return RedirectResponse("/pagar-simulado", status_code=303)

    monkeypatch.setattr(comprar, "iniciar_sesion", espia)
    monkeypatch.setattr(comprar, "iniciar_cobro", cobro_falso)
    correo = f"prueba-checkout-{marca}@ejemplo.pe"
    r = await cliente.post("/comprar", data={
        "plan": "pro", "periodo": "mensual", "email": correo,
        "password": "ClaveDePrueba123!"})
    fila = await get_usuario_por_email(correo)
    try:
        assert r.status_code == 303 and r.headers["location"] == "/pagar-simulado"
        assert fila and llamadas == [fila["id"]]
        # La sesion quedo abierta y sellada: el panel responde.
        assert (await cliente.get("/configuracion")).status_code == 200
        # Y la verificacion salio, sin frenar la compra.
        assert [d for d, _, _ in sin_smtp] == [correo]
        assert not fila["email_verificado_en"]
    finally:
        if fila:
            await borrar_cuenta(fila["id"])


@sin_base
async def test_el_checkout_con_correo_registrado_no_lo_confirma_y_avisa_al_dueno(
        cliente, usuario, sin_smtp):
    from web import comprar
    comprar._FRENO_AVISO.reiniciar()
    r = await cliente.post("/comprar", data={
        "plan": "pro", "periodo": "anual",
        "email": usuario["email"], "password": "OtraClaveDistinta123!"})
    assert r.status_code == 303
    destino = unquote_plus(r.headers["location"])
    assert destino.startswith("/entrar?siguiente=")
    # El texto vale igual si la cuenta existe que si no.
    assert "Si ya tienes una cuenta" in destino and "Si no, revisa tu correo" in destino
    assert "Ya tienes una cuenta" not in destino
    # El dueno del correo recibe el aviso con el enlace a su checkout.
    assert [d for d, _, _ in sin_smtp] == [usuario["email"]]
    assert "comprar" in unquote_plus(sin_smtp[0][2])


@sin_base
async def test_el_aviso_al_dueno_tiene_tope(cliente, usuario, sin_smtp):
    from web import comprar
    comprar._FRENO_AVISO.reiniciar()
    for _ in range(comprar.AVISOS_POR_CORREO + 3):
        await cliente.post("/comprar", data={
            "plan": "pro", "periodo": "mensual",
            "email": usuario["email"], "password": "OtraClaveDistinta123!"})
    assert len(sin_smtp) == comprar.AVISOS_POR_CORREO
    comprar._FRENO_AVISO.reiniciar()


@sin_base
async def test_un_correo_sin_cuenta_no_genera_aviso(sin_smtp, marca):
    from web import comprar
    comprar._FRENO_AVISO.reiniciar()
    await comprar._avisar_al_dueno_del_correo(f"nadie-{marca}@ejemplo.pe",
                                              "http://pruebas", "/comprar/pro")
    assert sin_smtp == []


# ─── 5. Contrasenas ──────────────────────────────────────

@sin_base
async def test_cambiar_la_contrasena_desde_configuracion(cliente, usuario):
    import httpx

    from web.app import app

    await _entrar(cliente, usuario)
    # Otra sesion abierta de la misma cuenta.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://pruebas") as otro:
        await _entrar(otro, usuario)

        r = await cliente.post("/configuracion/password",
                               data={"actual": "mal", "nueva": "OtraClave456!x"})
        assert "error=" in r.headers["location"]

        larga = "ñ" * 40  # 80 bytes: bcrypt no la admite
        r = await cliente.post("/configuracion/password",
                               data={"actual": usuario["password"], "nueva": larga})
        assert "72" in unquote_plus(r.headers["location"])

        r = await cliente.post("/configuracion/password",
                               data={"actual": usuario["password"], "nueva": "corta"})
        assert "10 caracteres" in unquote_plus(r.headers["location"])

        r = await cliente.post("/configuracion/password",
                               data={"actual": usuario["password"],
                                     "nueva": "OtraClave456!x"})
        assert "aviso=" in r.headers["location"]
        # Esta sesion sigue; la otra queda revocada.
        assert (await cliente.get("/configuracion")).status_code == 200
        assert (await otro.get("/configuracion")).status_code == 303


@sin_base
async def test_el_restablecimiento_rechaza_mas_de_72_bytes_con_mensaje(cliente, usuario):
    from shared.db import crear_token_recuperacion
    from shared.seguridad import nuevo_token_recuperacion
    token, token_hash, expira = nuevo_token_recuperacion()
    await crear_token_recuperacion(usuario["id"], token_hash, expira)
    r = await cliente.post(f"/recuperar/{token}", data={"password": "ñ" * 40})
    assert r.status_code == 400 and "72" in r.text
