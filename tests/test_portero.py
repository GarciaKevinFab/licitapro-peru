"""Pruebas del portero de suscripcion: que rutas escapan del cobro.

QUE PROTEGEN, Y POR QUE SE ROMPE SOLO

  `exigir_suscripcion` es un middleware: cubre TODAS las rutas de golpe, que
  es justo lo que se queria -- olvidarse de decorar una sola seria dejar la
  puerta abierta. El unico agujero posible es la lista de excepciones.

  Y esa lista se comprobaba con un `startswith` a secas. Con esa regla,
  `/registro-empresa` quedaba libre por empezar por `/registro`, y
  `/entrar-como` por empezar por `/entrar`. Hoy no cuela ninguna ruta de las
  69 declaradas -- se comprobo una por una --, pero eso es suerte, no diseno:
  bastaba con que alguien anadiera una ruta con ese prefijo.

  Un fallo asi NO da error, NO abre un ticket y NO aparece en ningun log:
  simplemente se deja de facturar. Por eso se fija aqui.

POR QUE HAY PRUEBAS DE LAS DOS DIRECCIONES

  Cerrar de mas es tan malo como abrir de menos. Si `/webhooks/izipay` deja de
  ser libre, Izipay recibe un 303 en vez de un 200, da el webhook por
  fallido, y los pagos confirmados dejan de aplicarse. Se cobra al cliente y
  no se le activa la cuenta, que es la peor version de este fallo.
"""
import pytest

from shared.suscripciones import RUTAS_LIBRES, ruta_libre

# ─── Lo que TIENE que estar abierto ──────────────────────

@pytest.mark.parametrize("camino", [
    "/entrar", "/registro", "/salir", "/recuperar", "/recuperar/un-token",
    "/suscripcion", "/suscripcion/pagar", "/suscripcion/retorno",
    "/salud", "/static/estilo.css", "/privacidad", "/terminos",
])
def test_las_rutas_de_servicio_no_piden_suscripcion(camino):
    """Sin estas, un usuario con la cuenta suspendida no podria ni pagar ni salir."""
    assert ruta_libre(camino) is True


@pytest.mark.parametrize("camino", ["/webhooks/izipay", "/webhooks/whatsapp"])
def test_los_webhooks_siguen_abiertos(camino):
    """Los avisos de la pasarela no traen cookie: su autenticidad es la firma.

    Si el portero los redirigiera, Izipay veria un 303, daria el aviso por
    fallido y el pago cobrado no activaria la cuenta.
    """
    assert ruta_libre(camino) is True


def test_las_legales_se_leen_con_la_suscripcion_caida():
    """Saber que se hace con tus datos no depende de estar al dia con el pago."""
    assert ruta_libre("/privacidad") is True
    assert ruta_libre("/terminos") is True


# ─── Lo que NO puede colarse ─────────────────────────────

@pytest.mark.parametrize("camino", [
    "/registro-empresa",   # empieza por /registro
    "/entrar-como",        # empieza por /entrar
    "/saludos",            # empieza por /salud
    "/administrar",        # empieza por /admin
    "/terminosyoferta",    # empieza por /terminos
    "/staticos",           # empieza por /static
])
def test_una_ruta_que_solo_comparte_prefijo_no_queda_gratis(camino):
    """El fallo que esto congela: cobrar depende de un limite de segmento.

    Ninguna de estas existe hoy. La prueba esta precisamente para el dia que
    alguien cree una y no se entere de que acaba de regalarla.
    """
    assert ruta_libre(camino) is False


@pytest.mark.parametrize("camino", [
    "/panel", "/empresas", "/licitaciones", "/propuestas", "/contratos",
    "/configuracion",
])
def test_el_producto_de_pago_sigue_detras_del_portero(camino):
    assert ruta_libre(camino) is False


def test_los_prefijos_de_arbol_se_escriben_con_barra():
    """La regla solo abre un arbol entero si el prefijo acaba en "/".

    Se comprueba sobre la constante y no sobre un caso: asi, quien anada
    manana un prefijo ancho sin la barra ve fallar esto y no descubre el
    agujero seis meses despues.
    """
    anchos = [r for r in RUTAS_LIBRES if r.endswith("/")]
    assert anchos == ["/webhooks/"] or anchos == ("/webhooks/",) or set(anchos) == {"/webhooks/"}
