"""Correcciones de la auditoria del 20/10/2026: autenticacion, cabeceras y despliegue.

Cada bloque fija un agujero concreto que se encontro abierto. Si una de estas
pruebas falla, no es un detalle de estilo: es la puerta que se volvio a abrir.
"""
import base64
import hashlib
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tests.conftest import sin_base
from web import auth, limites
from web.app import _csp, app, peticion_ajena

RAIZ = Path(__file__).resolve().parent.parent


# ─── IP del cliente: X-Forwarded-For no manda ────────────

def test_x_forwarded_for_inventado_no_cambia_la_ip():
    """El primer elemento de XFF lo escribe el cliente: con el se saltaba el freno."""
    scope = {"headers": [(b"x-forwarded-for", b"1.2.3.4")],
             "client": ("172.18.0.1", 5555)}
    assert limites.ip_del_cliente(scope) == "172.18.0.1"

    # La de Cloudflare si manda, aunque venga XFF al lado.
    scope["headers"].append((b"cf-connecting-ip", b"203.0.113.9"))
    assert limites.ip_del_cliente(scope) == "203.0.113.9"


def test_el_dockerfile_no_se_fia_de_cualquier_proxy():
    lineas = [linea.strip() for linea in
              (RAIZ / "Dockerfile").read_text(encoding="utf-8").splitlines()
              if not linea.strip().startswith("#")]
    codigo = "\n".join(lineas)
    assert "forwarded-allow-ips" not in codigo
    assert "ENV FORWARDED_ALLOW_IPS=127.0.0.1" in lineas


@sin_base
async def test_el_freno_de_acceso_cuenta_la_ip_de_cloudflare(cliente, usuario):
    from shared.db import connection

    falsa, real = "198.51.100.66", "203.0.113.77"
    try:
        r = await cliente.post(
            "/entrar",
            data={"email": usuario["email"], "password": "no-es-la-clave"},
            headers={"x-forwarded-for": falsa, "cf-connecting-ip": real})
        assert r.status_code == 401
        async with connection() as c:
            ips = {f["identificador"] for f in await c.fetch(
                "SELECT identificador FROM intentos_acceso WHERE tipo='ip' "
                "AND identificador = ANY($1::text[])", [falsa, real])}
        assert ips == {real}
    finally:
        async with connection() as c:
            await c.execute(
                "DELETE FROM intentos_acceso WHERE identificador = ANY($1::text[])",
                [falsa, real, usuario["email"].lower()])


# ─── Redireccion abierta ─────────────────────────────────

def test_destino_seguro_rechaza_lo_que_el_navegador_lee_como_otro_sitio():
    for malo in ("/\\evil.com", "/\\/evil.com", "//evil.com", "https://evil.com",
                 "javascript:alert(1)", "/\t/evil.com", "/\n/evil.com",
                 "evil.com", "", "\\\\evil.com"):
        assert auth._destino_seguro(malo) == "/", repr(malo)

    for bueno in ("/panel", "/propuestas/3?pestana=docs", "/licitacion/9#precios"):
        assert auth._destino_seguro(bueno) == bueno


# ─── CSRF ────────────────────────────────────────────────

def test_csrf_decision():
    propio = {"host": "licitapro.sisac.pe"}
    assert peticion_ajena("POST", "/salir", {**propio, "origin": "https://evil.com"})
    # Un subdominio hermano es "el mismo sitio" para SameSite, pero no este origen.
    assert peticion_ajena("POST", "/salir",
                          {**propio, "origin": "https://otro.sisac.pe"})
    assert peticion_ajena("POST", "/salir", {**propio, "origin": "null"})
    assert peticion_ajena("POST", "/salir",
                          {**propio, "referer": "https://evil.com/form"})
    assert peticion_ajena("POST", "/salir", {**propio, "sec-fetch-site": "same-site"})

    assert not peticion_ajena("POST", "/salir",
                              {**propio, "origin": "https://licitapro.sisac.pe"})
    assert not peticion_ajena("POST", "/salir",
                              {**propio, "referer": "https://licitapro.sisac.pe/panel"})
    assert not peticion_ajena("POST", "/salir", {**propio, "sec-fetch-site": "same-origin"})
    # Sin ninguna cabecera de navegador: curl, la suite. No arrastran cookie ajena.
    assert not peticion_ajena("POST", "/salir", propio)
    # Lo que no escribe, y los webhooks, no se miran.
    assert not peticion_ajena("GET", "/panel", {**propio, "origin": "https://evil.com"})
    assert not peticion_ajena("POST", "/webhooks/culqi",
                              {**propio, "origin": "https://evil.com"})


async def test_post_desde_otro_origen_se_rechaza(cliente):
    r = await cliente.post("/salir", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    # La respuesta de rechazo lleva las cabeceras de seguridad como cualquier otra.
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_post_desde_el_mismo_origen_pasa(cliente):
    r = await cliente.post("/salir", headers={"origin": "http://pruebas"})
    assert r.status_code == 303
    assert r.headers["location"] == "/entrar"


# ─── /docs y compania, solo en desarrollo ────────────────

def test_documentacion_de_la_api_apagada_fuera_de_desarrollo():
    entorno = {**os.environ, "LICITAPRO_ENTORNO": "produccion",
               "LICITAPRO_SECRET_KEY": "clave_de_pruebas_de_al_menos_32_caracteres_x",
               "PYTHONPATH": str(RAIZ)}
    codigo = ("from web.app import app;"
              "print(app.docs_url, app.redoc_url, app.openapi_url)")
    salida = subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, env=entorno,
                            capture_output=True, text=True, timeout=120)
    assert salida.returncode == 0, salida.stderr[-2000:]
    assert salida.stdout.strip().splitlines()[-1] == "None None None"


# ─── CSP sin unpkg; htmx propio y con SRI ────────────────

def test_csp_no_admite_unpkg():
    assert "unpkg.com" not in _csp("x")


def test_htmx_se_sirve_desde_static_con_integrity_que_casa():
    plantilla = (RAIZ / "web/templates/dashboard.html").read_text(encoding="utf-8")
    assert "unpkg.com" not in plantilla
    assert "estatico('htmx.min.js')" in plantilla

    contenido = (RAIZ / "web/static/htmx.min.js").read_bytes()
    sri = "sha384-" + base64.b64encode(hashlib.sha384(contenido).digest()).decode()
    # Si el archivo cambia sin cambiar el hash, el navegador no ejecuta htmx y
    # el panel deja de filtrar sin ningun error visible.
    assert f'integrity="{sri}"' in plantilla


async def test_htmx_responde_en_static(cliente):
    r = await cliente.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert b"htmx" in r.content[:2000]


# ─── .dockerignore ───────────────────────────────────────

def test_dockerignore_deja_fuera_secretos_y_datos():
    lineas = {linea.strip() for linea in
              (RAIZ / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    for patron in (".env", ".env.*", "data/", ".git", "tests/"):
        assert patron in lineas, patron
    # Lo que el contenedor si usa no puede quedar fuera.
    for necesario in ("tools/", "shared/", "migrations/", "web/"):
        assert necesario not in lineas, necesario


def test_caddy_corta_cuerpos_grandes():
    texto = (RAIZ / "Caddyfile").read_text(encoding="utf-8")
    assert "max_size 15MB" in texto


# ─── Sesion: rotacion y revocacion ───────────────────────

async def _entrar(cliente, usuario):
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303, r.text[:300]


@sin_base
async def test_cambiar_la_contrasena_cierra_las_sesiones_abiertas(cliente, usuario):
    from shared.admin_cuentas import cambiar_password
    from shared.seguridad import hashear_password

    await _entrar(cliente, usuario)
    assert (await cliente.get("/configuracion")).status_code == 200

    # La contrasena cambia por otro camino (recuperacion, el admin...).
    assert await cambiar_password(usuario["id"], hashear_password("OtraClave456!x"))

    r = await cliente.get("/configuracion")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/entrar")


@sin_base
async def test_el_login_no_hereda_la_sesion_anterior(usuario):
    """Fijacion de sesion: lo que traia la cookie antes de entrar se descarta."""
    peticion = SimpleNamespace(session={"admin_original": 999, "otra": "cosa"})
    auth.iniciar_sesion(peticion, {"id": usuario["id"], "password_hash": "x"})
    assert set(peticion.session) == {"usuario_id", "huella"}
    assert peticion.session["usuario_id"] == usuario["id"]


@sin_base
async def test_usuario_actual_no_entrega_el_hash(usuario):
    peticion = SimpleNamespace(session={"usuario_id": usuario["id"]})
    datos = await auth.usuario_actual(peticion)
    assert datos["id"] == usuario["id"]
    assert "password_hash" not in datos
    # La sesion sin huella (suplantacion del admin, alta en /comprar) se sella.
    assert peticion.session["huella"].startswith(f"{usuario['id']}:")
    assert await auth.verificar_password_de(usuario["id"], usuario["password"])
    assert not await auth.verificar_password_de(usuario["id"], "mal")


@sin_base
async def test_huella_de_otro_usuario_se_resella_y_no_revoca(usuario):
    """El admin que suplanta cambia usuario_id: eso no es una revocacion."""
    peticion = SimpleNamespace(session={"usuario_id": usuario["id"],
                                        "huella": "999999:abcdef0123456789"})
    assert await auth.usuario_actual(peticion) is not None
    assert peticion.session["huella"].startswith(f"{usuario['id']}:")


# ─── Registro sin enumerar correos ───────────────────────

@sin_base
async def test_registro_con_correo_existente_no_lo_confirma(cliente, usuario):
    r = await cliente.post("/registro", data={"email": usuario["email"],
                                              "password": "OtraClaveLarga789!"})
    assert r.status_code == 400
    assert "Ya existe" not in r.text
    assert "No pudimos crear la cuenta" in r.text


# ─── Recuperacion ────────────────────────────────────────

def test_el_enlace_no_sale_de_la_cabecera_host(monkeypatch):
    monkeypatch.setenv("LICITAPRO_ENTORNO", "produccion")
    monkeypatch.setenv("LICITAPRO_DOMINIO", "licitapro.sisac.pe")
    peticion = SimpleNamespace(base_url="https://sitio-del-atacante.example/")
    assert auth._origen_para_enlaces(peticion) == "https://licitapro.sisac.pe"


@sin_base
async def test_sin_smtp_el_token_no_va_al_log(monkeypatch, caplog, usuario):
    from shared.db import connection

    monkeypatch.setenv("LICITAPRO_ENTORNO", "produccion")
    enlaces = []

    async def smtp_caido(destinatario, enlace):
        enlaces.append(enlace)
        return False

    monkeypatch.setattr(auth, "_enviar_correo_recuperacion", smtp_caido)
    with caplog.at_level(logging.INFO, logger="web.auth"):
        await auth._procesar_recuperacion(usuario["email"], "203.0.113.5",
                                          "https://licitapro.sisac.pe")
    assert len(enlaces) == 1
    token = enlaces[0].rsplit("/", 1)[-1]
    assert token not in caplog.text
    assert "/recuperar/" not in caplog.text

    async with connection() as c:
        await c.execute("DELETE FROM tokens_recuperacion WHERE usuario_id=$1",
                        usuario["id"])


@sin_base
async def test_recuperar_responde_igual_exista_o_no(cliente, usuario, monkeypatch):
    enviados = []

    async def smtp_falso(destinatario, enlace):
        enviados.append(destinatario)
        return True

    monkeypatch.setattr(auth, "_enviar_correo_recuperacion", smtp_falso)
    con = await cliente.post("/recuperar", data={"email": usuario["email"]})
    sin = await cliente.post("/recuperar",
                             data={"email": "nadie-" + usuario["email"]})
    assert con.status_code == sin.status_code == 200

    def sin_nonce(texto):
        return re.sub(r'nonce="[^"]+"', "", texto)

    assert sin_nonce(con.text) == sin_nonce(sin.text)
    # El trabajo se hizo, en segundo plano, solo para quien tiene cuenta.
    assert enviados == [usuario["email"]]


# ─── Ciclo de vida: el pool se abre y se cierra ──────────

@sin_base
async def test_lifespan_abre_y_cierra_el_pool():
    from shared import db

    async with app.router.lifespan_context(app):
        assert db._pool is not None
        pool = db._pool
    assert db._pool is None
    assert pool._closed
    # Las pruebas siguientes lo recrean al primer uso.
    await db.get_pool()
