"""Conectar Telegram: el codigo se ensena en Configuracion, no se salta a t.me.

Antes el POST redirigia a t.me/<bot>?start=<token>. En el escritorio esa pagina
solo ofrece "START BOT", que intenta abrir la app con tg://: sin Telegram
Desktop, o con el navegador bloqueando el protocolo, no hacia nada. Ahora el
POST vuelve a /configuracion, que da la app, Telegram Web y el mensaje a mano.
"""
import pytest

from tests.conftest import sin_base

pytestmark = [pytest.mark.asyncio, sin_base]


async def _entrar(cliente, usuario):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303


async def test_el_boton_vuelve_a_configuracion_y_no_salta_a_telegram(usuario, cliente):
    await _entrar(cliente, usuario)
    r = await cliente.post("/configuracion/telegram/vincular")
    assert r.status_code == 303
    assert r.headers["location"] == "/configuracion#telegram"


async def test_configuracion_ensena_el_codigo_y_los_tres_caminos(usuario, cliente):
    from shared.db import connection

    await _entrar(cliente, usuario)
    await cliente.post("/configuracion/telegram/vincular")
    async with connection() as c:
        token = await c.fetchval(
            "SELECT telegram_token FROM usuarios WHERE id=$1", usuario["id"])
    assert token

    html = (await cliente.get("/configuracion")).text
    assert f"/start {token}" in html
    assert "tg://resolve?domain=" in html and f"start={token}" in html
    assert "https://web.telegram.org/k/#?tgaddr=tg%3A%2F%2Fresolve" in html
    assert "https://t.me/" in html


async def test_un_codigo_caducado_no_se_ensena(usuario, cliente):
    """Si se ensenara, el usuario lo mandaria y el bot le diria que no vale."""
    from shared.db import connection

    await _entrar(cliente, usuario)
    await cliente.post("/configuracion/telegram/vincular")
    async with connection() as c:
        token = await c.fetchval(
            """UPDATE usuarios SET telegram_token_expira = NOW() - INTERVAL '1 minute'
                WHERE id=$1 RETURNING telegram_token""", usuario["id"])

    html = (await cliente.get("/configuracion")).text
    assert token not in html
    assert "Conectar Telegram" in html
