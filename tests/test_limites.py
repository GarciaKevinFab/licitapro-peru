"""El limite de peticiones por IP del panel, sin servidor ni base de datos.

    python -m pytest tests/test_limites.py -q

web/limites.py es un middleware ASGI puro, asi que se le puede pasar un `scope`
a mano y recoger lo que manda por `send`. Se usa asyncio.run() y no
pytest-asyncio para no anadir un plugin por diez lineas de prueba.

LA QUE IMPORTA

  test_los_webhooks_nunca_se_limitan. Culqi avisa desde un punado de IPs suyas
  que un cobro salio bien. Si ese aviso se descarta con un 429, queda una
  suscripcion cobrada que el panel no registra -- dinero cobrado a un cliente
  que se queda sin su plan. Es el peor desenlace posible de este modulo, y por
  eso la exencion esta escrita en una prueba y no solo en un comentario.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web import limites  # noqa: E402

# Rango reservado para documentacion (RFC 5737): nunca es de nadie.
IP_A = b"203.0.113.7"
IP_B = b"203.0.113.8"


async def _app_ok(scope, receive, send):
    """La aplicacion que hay detras. Siempre responde 200."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def pedir(middleware, ruta, metodo="GET", ip=IP_A):
    """Una peticion. Devuelve (codigo, cabeceras como dict)."""

    async def correr():
        recogido = {}

        async def send(mensaje):
            if mensaje["type"] == "http.response.start":
                recogido["status"] = mensaje["status"]
                recogido["headers"] = dict(mensaje.get("headers") or [])

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "method": metodo,
            "path": ruta,
            "headers": [(b"cf-connecting-ip", ip)],
            "client": ("172.21.0.1", 54321),
        }
        await middleware(scope, receive, send)
        return recogido["status"], recogido.get("headers", {})

    return asyncio.run(correr())


def _middleware(reglas, exentas=limites.EXENTAS):
    # `activo=True` explicito: el conftest apaga el limite para el resto de la
    # suite (ver LICITAPRO_LIMITE_PETICIONES alli), y sin esto estas pruebas
    # pasarian en verde sin comprobar absolutamente nada.
    return limites.LimitePeticiones(_app_ok, reglas=reglas, exentas=exentas, activo=True)


def test_deja_pasar_hasta_el_limite_y_luego_corta():
    m = _middleware([(None, "/", 3, 60)])
    assert [pedir(m, "/propuestas")[0] for _ in range(5)] == [200, 200, 200, 429, 429]


def test_cada_ip_tiene_su_propio_cubo():
    """Si esto se rompe, un atacante bloquea a todos los clientes a la vez."""
    m = _middleware([(None, "/", 2, 60)])
    for _ in range(3):
        pedir(m, "/propuestas", ip=IP_A)
    assert pedir(m, "/propuestas", ip=IP_B)[0] == 200


def test_los_webhooks_nunca_se_limitan():
    """Descartar un aviso de Culqi deja un cobro sin registrar."""
    m = _middleware([(None, "/", 1, 60)])
    for _ in range(100):
        assert pedir(m, "/webhooks/culqi", "POST")[0] == 200
    # Y no han gastado el cubo del resto del panel.
    assert pedir(m, "/propuestas")[0] == 200


def test_los_estaticos_y_la_salud_estan_exentos():
    """Una pagina arrastra varios ficheros; el healthcheck golpea sin parar."""
    m = _middleware([(None, "/", 1, 60)])
    for _ in range(30):
        assert pedir(m, "/static/estilo.css")[0] == 200
        assert pedir(m, "/salud")[0] == 200
    assert pedir(m, "/propuestas")[0] == 200


def test_el_login_se_agota_antes_que_el_techo():
    """Frena la fuerza bruta ANTES de escribir en intentos_acceso."""
    m = _middleware([("POST", "/entrar", 2, 60), (None, "/", 100, 60)])
    assert [pedir(m, "/entrar", "POST")[0] for _ in range(3)] == [200, 200, 429]
    # Agotar el login no cierra el resto del panel.
    assert pedir(m, "/propuestas")[0] == 200


def test_el_429_es_una_pagina_y_dice_cuanto_esperar():
    """Quien se topa con esto es una persona con un navegador delante."""
    m = _middleware([(None, "/", 1, 60)])
    pedir(m, "/propuestas")
    codigo, cabeceras = pedir(m, "/propuestas")
    assert codigo == 429
    assert cabeceras[b"content-type"].startswith(b"text/html")
    assert 1 <= int(cabeceras[b"retry-after"]) <= 61


def test_la_ventana_se_desliza():
    m = _middleware([(None, "/", 2, 60)])
    pedir(m, "/propuestas")
    pedir(m, "/propuestas")
    assert pedir(m, "/propuestas")[0] == 429

    # Se envejecen las marcas 61 segundos en lugar de esperar de verdad.
    for marcas in m._marcas.values():
        for i in range(len(marcas)):
            marcas[i] -= 61
    assert pedir(m, "/propuestas")[0] == 200


def test_la_ip_sale_de_la_cabecera_y_no_de_la_conexion():
    """Delante hay Caddy y Cloudflare: la conexion siempre viene de Docker."""
    scope = {
        "headers": [(b"cf-connecting-ip", b"198.51.100.4")],
        "client": ("172.21.0.1", 1234),
    }
    assert limites.ip_del_cliente(scope) == "198.51.100.4"

    # X-Forwarded-For puede traer una cadena; el cliente es el primero.
    assert limites.ip_del_cliente({
        "headers": [(b"x-forwarded-for", b"198.51.100.4, 10.0.0.1")],
    }) == "198.51.100.4"

    # Sin cabeceras se cae a la conexion, que en local si es la buena.
    assert limites.ip_del_cliente({"headers": [], "client": ("127.0.0.1", 1)}) == "127.0.0.1"
    assert limites.ip_del_cliente({}) == "desconocida"


def test_las_reglas_de_produccion_terminan_en_un_techo_general():
    """La ultima regla tiene que cubrir TODO.

    Es lo que hace que una ruta nueva del panel nazca protegida. Si alguien
    anade una regla especifica al final por descuido, el techo deja de
    aplicarse a lo que quede por debajo y el panel vuelve a estar abierto sin
    que nada avise.
    """
    metodo, prefijo, cuantas, ventana = limites.REGLAS[-1]
    assert metodo is None
    assert prefijo is None
    assert cuantas > 0 and ventana > 0


def test_los_webhooks_siguen_exentos_en_la_configuracion_real():
    """La exencion vive en EXENTAS, no en la prueba de arriba."""
    assert "/webhooks" in limites.EXENTAS


def test_apagado_deja_pasar_todo():
    """El interruptor que usa el conftest tiene que apagarlo de verdad."""
    m = limites.LimitePeticiones(_app_ok, reglas=[(None, "/", 1, 60)], activo=False)
    assert [pedir(m, "/propuestas")[0] for _ in range(20)] == [200] * 20


def test_la_variable_de_entorno_manda(monkeypatch):
    """Encendido por omision; solo 'off' lo apaga.

    Si un valor cualquiera lo apagara, una errata en el .env del VPS dejaria
    produccion sin limite y sin que nada avisara.
    """
    monkeypatch.delenv("LICITAPRO_LIMITE_PETICIONES", raising=False)
    assert limites.activo_por_entorno() is True

    for valor in ("off", "OFF", " off "):
        monkeypatch.setenv("LICITAPRO_LIMITE_PETICIONES", valor)
        assert limites.activo_por_entorno() is False, valor

    for valor in ("on", "", "1", "si", "false"):
        monkeypatch.setenv("LICITAPRO_LIMITE_PETICIONES", valor)
        assert limites.activo_por_entorno() is True, valor
