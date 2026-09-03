"""Pruebas del checkout publico: que el producto se pueda comprar desde fuera.

QUE FALLO, Y POR QUE NO SE VEIA DESDE DENTRO

  El cobro estaba entero detras del login. Desde una cuenta abierta el flujo
  funcionaba perfectamente -- /suscripcion, elegir plan, pagar --, asi que
  probandolo como cliente todo estaba bien.

  Desde fuera no habia nada. La portada anunciaba tres precios y los tres
  botones llevaban a un formulario de registro; en ninguna pagina publica
  aparecia un importe a pagar ni un boton de pagar. Izipay lo miro asi, sin
  cuenta, y rechazo la integracion: "no cuentas con un carrito de compras,
  proceso de checkout o boton de pago".

  Es el peor tipo de fallo: no da error, no aparece en ningun log, y quien lo
  padece -- el que todavia no es cliente -- no tiene forma de reportarlo.

LO QUE SE FIJA AQUI, EN UNA FRASE

  Que un visitante SIN SESION llegue a ver un total y un boton de pagar.

  Por eso casi todas usan el cliente HTTP sin cookie: la comprobacion no es
  "el checkout responde", es "el checkout responde a quien no ha entrado". Con
  sesion ya funcionaba antes del arreglo.
"""
import pytest

from shared.suscripciones import ruta_libre
from tests.conftest import sin_base
from web.comprar import PERIODOS, _comercio, _desglose
from web.suscripcion import _con_error


# ─── El desglose que se ensena en el resumen del pedido ──

@pytest.mark.parametrize("total", ["49.00", "99.00", "199.00", "490.00",
                                   "990.00", "1990.00", "0.01", "33.33"])
def test_base_mas_igv_suman_exactamente_el_total(total):
    """La cuenta que el cliente compara con el cargo de su tarjeta.

    Es el fallo silencioso clasico de un checkout: base e IGV redondeados por
    separado suman un centimo mas o menos que el total. Nadie devuelve el
    producto por eso, pero es lo primero que mira quien valida un comercio, y
    convierte el resumen del pedido en un papel que no cuadra.
    """
    d = _desglose(total)
    assert d["base"] + d["igv"] == d["total"]


def test_el_igv_es_el_18_por_ciento_de_la_base():
    """Los precios se publican CON IGV, asi que el desglose va hacia atras.

    S/99 con IGV incluido son S/83.90 de valor de venta y S/15.10 de impuesto.
    Si alguien "arregla" esto multiplicando el precio por 1.18, el total
    mostrado dejaria de ser el cobrado y esta prueba cae.
    """
    d = _desglose("99.00")
    assert (str(d["base"]), str(d["igv"]), str(d["total"])) == ("83.90", "15.10", "99.00")


def test_el_total_es_intocable():
    """El desglose informa; no cambia ni un centimo de lo que se cobra."""
    assert str(_desglose("49.00")["total"]) == "49.00"


# ─── El portero deja pasar al escaparate ─────────────────

@pytest.mark.parametrize("camino", [
    "/precios", "/comprar", "/comprar/pro", "/comprar/basico",
])
def test_comprar_no_pide_estar_al_dia_de_pago(camino):
    """Cerrar el checkout a quien debe dinero es dispararse en el pie.

    Quien llega desde /suscripcion con la cuenta suspendida viene justamente a
    pagar. Si el portero le redirige a "tu suscripcion esta suspendida", vuelve
    al sitio del que acaba de salir y no hay forma de cobrarle.
    """
    assert ruta_libre(camino) is True


@pytest.mark.parametrize("camino", ["/comprarlo", "/precioso"])
def test_una_ruta_que_solo_comparte_prefijo_no_queda_gratis(camino):
    """Mismo limite de segmento que el resto: compartir prefijo no basta."""
    assert ruta_libre(camino) is False


# ─── El error vuelve al checkout correcto ────────────────

def test_el_error_respeta_el_periodo_que_traia_la_url():
    """Pegar siempre "?" le comeria el periodo al checkout anual.

    Quien falla la contrasena comprando el plan anual tiene que volver al
    anual: si no, corrige el dato y acaba pagando otro importe que no eligio.
    """
    assert _con_error("/comprar/pro?periodo=anual", "Ese correo no parece válido.") == (
        "/comprar/pro?periodo=anual&error=Ese+correo+no+parece+v%C3%A1lido.")


def test_sin_query_previa_el_error_abre_la_query():
    assert _con_error("/suscripcion", "Nada") == "/suscripcion?error=Nada"


# ─── La identidad del comercio no se inventa ─────────────

def test_sin_datos_de_comercio_no_se_pinta_nada(monkeypatch):
    """Un RUC de relleno en un checkout es peor que un hueco.

    Tiene que coincidir letra por letra con el contrato de comercio, y un
    marcador de posicion colado en produccion es justo el detalle que tumba la
    validacion por segunda vez.
    """
    for v in ("LICITAPRO_RAZON_SOCIAL", "LICITAPRO_RUC", "LICITAPRO_CONTACTO_EMAIL",
              "LICITAPRO_CONTACTO_TELEFONO", "LICITAPRO_DIRECCION"):
        monkeypatch.delenv(v, raising=False)
    assert _comercio() == {}


def test_los_datos_de_comercio_se_limpian_y_los_vacios_no_entran(monkeypatch):
    monkeypatch.setenv("LICITAPRO_RAZON_SOCIAL", "  Ejemplo SAC  ")
    monkeypatch.setenv("LICITAPRO_RUC", "20123456789")
    monkeypatch.setenv("LICITAPRO_CONTACTO_EMAIL", "   ")
    monkeypatch.delenv("LICITAPRO_CONTACTO_TELEFONO", raising=False)
    monkeypatch.delenv("LICITAPRO_DIRECCION", raising=False)
    assert _comercio() == {"razon_social": "Ejemplo SAC", "ruc": "20123456789"}


# ─── Lo que ve el visitante sin cuenta ───────────────────

@sin_base
async def test_precios_se_ve_sin_haber_entrado(cliente):
    """La pagina que no existia. Sin ella no hay catalogo publico que ensenar."""
    r = await cliente.get("/precios")
    assert r.status_code == 200
    assert "S/" in r.text


@sin_base
async def test_el_checkout_ensena_el_total_y_el_boton_de_pagar_sin_sesion(cliente):
    """El corazon del asunto, y lo que la pasarela no encontraba.

    Se comprueban las tres cosas que pedia el correo de Izipay y que ninguna
    pagina publica tenia: el articulo, el importe total y un boton que diga
    pagar.
    """
    r = await cliente.get("/comprar/pro?periodo=mensual")
    assert r.status_code == 200
    assert "Resumen del pedido" in r.text
    assert "Total a pagar" in r.text
    assert "Pagar S/ 99.00" in r.text
    # Y los campos para crear la cuenta en el mismo paso: sin ellos esto vuelve
    # a ser un registro con un precio pintado encima.
    assert 'name="email"' in r.text and 'name="password"' in r.text


@sin_base
async def test_el_checkout_anual_cobra_el_precio_anual(cliente):
    """Un periodo mal leido cobra 99 donde tocaban 990, o al reves."""
    r = await cliente.get("/comprar/pro?periodo=anual")
    assert r.status_code == 200
    assert "Pagar S/ 990.00" in r.text


@sin_base
@pytest.mark.parametrize("url", ["/comprar/no-existe", "/comprar/gratis"])
async def test_un_checkout_sin_precio_devuelve_al_catalogo(cliente, url):
    """Nunca una pantalla de pago de S/0.00 ni un 500.

    Un plan inventado y el gratuito acaban igual: no hay nada que cobrar, asi
    que se vuelve al catalogo en vez de abrir una orden vacia contra Izipay.
    """
    r = await cliente.get(url)
    assert r.status_code == 303
    assert r.headers["location"] == "/precios"


@sin_base
async def test_un_periodo_inventado_cae_a_mensual_y_no_rompe(cliente):
    """No redirige: ensena el checkout mensual, que es el valor por defecto.

    Lo que NO puede pasar es que un periodo manipulado en la URL llegue a la
    tabla `suscripciones`, que solo admite 'mensual' y 'anual'.
    """
    r = await cliente.get("/comprar/pro?periodo=trienal")
    assert r.status_code == 200
    assert "Pagar S/ 99.00" in r.text


@sin_base
async def test_comprar_con_correo_invalido_vuelve_al_mismo_checkout(cliente):
    """Y vuelve al SUYO, con el periodo intacto, no al mensual por defecto."""
    r = await cliente.post("/comprar", data={
        "plan": "pro", "periodo": "anual",
        "email": "esto-no-es-un-correo", "password": "ClaveDePrueba123!"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/comprar/pro?periodo=anual&error=")


@sin_base
async def test_comprar_con_contrasena_debil_no_crea_la_cuenta(cliente):
    """El checkout no puede ser la puerta trasera del registro.

    Si aqui se relajaran las reglas, cualquiera crearia por /comprar la cuenta
    con la contrasena que /registro le rechaza.
    """
    from shared.db import get_usuario_por_email
    correo = "checkout-debil@ejemplo.pe"
    r = await cliente.post("/comprar", data={
        "plan": "pro", "periodo": "mensual", "email": correo, "password": "123"})
    assert r.status_code == 303
    assert "/comprar/pro" in r.headers["location"]
    assert await get_usuario_por_email(correo) is None


@sin_base
async def test_un_plan_inventado_no_llega_a_crear_cuenta(cliente):
    """Se valida el plan ANTES de dar de alta a nadie.

    Al reves quedaria una cuenta huerfana cada vez que alguien toquetee el
    campo oculto del formulario o que un plan se desactive con un checkout
    abierto en otra pestana.
    """
    from shared.db import get_usuario_por_email
    correo = "checkout-plan-falso@ejemplo.pe"
    r = await cliente.post("/comprar", data={
        "plan": "plan-que-no-existe", "periodo": "mensual",
        "email": correo, "password": "ClaveDePrueba123!"})
    assert r.status_code == 303
    assert r.headers["location"] == "/precios"
    assert await get_usuario_por_email(correo) is None


@sin_base
async def test_quien_ya_tiene_cuenta_va_a_entrar_y_vuelve_a_su_checkout(cliente, usuario):
    """Un cliente que vuelve no es un error.

    Se le manda a entrar con `siguiente` apuntando a ESTE checkout: dejandole
    en el panel tendria que volver a buscar el plan que ya habia elegido, y ahi
    es donde se abandona la compra.
    """
    r = await cliente.post("/comprar", data={
        "plan": "pro", "periodo": "anual",
        "email": usuario["email"], "password": "OtraClaveDistinta123!"})
    assert r.status_code == 303
    destino = r.headers["location"]
    assert destino.startswith("/entrar?siguiente=")
    assert "comprar" in destino and "anual" in destino


@sin_base
async def test_todos_los_periodos_declarados_tienen_checkout(cliente):
    """PERIODOS es el contrato con la tabla `suscripciones`.

    Si alguien anade "trimestral" a la tupla sin que `planes` tenga esa columna
    de precio, el checkout se queda sin importe. Aqui se ve.
    """
    for periodo in PERIODOS:
        r = await cliente.get(f"/comprar/pro?periodo={periodo}")
        assert r.status_code == 200, f"periodo {periodo} sin checkout"
        assert "Total a pagar" in r.text


# ─── La promesa del cobro sale de la pasarela, no de la plantilla ─

# QUE PASO Y POR QUE HAY PRUEBAS DE ESTO
#
#   /comprar, /precios y /terminos afirmaron durante meses que la suscripcion
#   se renovaba sola y que un cobro fallido se reintentaba, con una pasarela
#   que solo hacia pagos unicos. Nadie mintio a proposito: la promesa estaba
#   escrita a mano en tres plantillas y la realidad cambio en otro sitio.
#
#   Ahora las tres cuelgan de `cobro_recurrente()`. Estas pruebas comprueban
#   las dos direcciones, porque el fallo puede repetirse en cualquiera de las
#   dos: prometer renovacion sin pasarela que la haga, o seguir diciendo "pago
#   unico" cuando ya se cobra solo.

def _con_culqi(monkeypatch):
    monkeypatch.setenv("CULQI_MODO", "prueba")
    monkeypatch.setenv("CULQI_LLAVE_PUBLICA", "pk_test_pruebas")
    monkeypatch.setenv("CULQI_LLAVE_SECRETA", "sk_test_pruebas")


def _sin_culqi(monkeypatch):
    for v in ("CULQI_MODO", "CULQI_LLAVE_PUBLICA", "CULQI_LLAVE_SECRETA"):
        monkeypatch.delenv(v, raising=False)


@sin_base
@pytest.mark.parametrize("pagina", ["/precios", "/terminos"])
async def test_sin_pasarela_recurrente_el_texto_dice_pago_unico(
        cliente, monkeypatch, pagina):
    """Sin llaves no hay cobro automatico, y el texto no puede prometerlo."""
    _sin_culqi(monkeypatch)
    r = await cliente.get(pagina)
    assert r.status_code == 200
    assert "no deja cobros programados" in r.text or "de una sola vez" in r.text
    assert "renovación es automática" not in r.text
    assert "se renuevan automáticamente" not in r.text


@sin_base
@pytest.mark.parametrize("pagina", ["/precios", "/terminos"])
async def test_con_culqi_el_texto_promete_la_renovacion_que_si_ocurre(
        cliente, monkeypatch, pagina):
    _con_culqi(monkeypatch)
    r = await cliente.get(pagina)
    assert r.status_code == 200
    assert "automátic" in r.text
    assert "de una sola vez" not in r.text
    assert "no deja cobros programados" not in r.text


@sin_base
async def test_la_pagina_de_privacidad_nombra_a_la_pasarela_que_cobra(
        cliente, monkeypatch):
    """El encargado del tratamiento se declara por su nombre (Ley 29733).

    Nombrar al que no es tan malo como no nombrar a ninguno.
    """
    _con_culqi(monkeypatch)
    r = await cliente.get("/privacidad")
    assert "<b>Culqi.</b>" in r.text

    _sin_culqi(monkeypatch)
    r = await cliente.get("/privacidad")
    assert "<b>Izipay.</b>" in r.text


@sin_base
async def test_sin_plan_sincronizado_el_checkout_no_abre_culqi(cliente, monkeypatch):
    """Media configuracion tiene que dar el flujo viejo ENTERO.

    Con las llaves puestas pero sin `pln_` en la tabla, un boton que abriera el
    formulario de tarjeta acabaria en un error de la pasarela con la tarjeta ya
    escrita. Se vuelve al pago unico, que si funciona.
    """
    _con_culqi(monkeypatch)
    r = await cliente.get("/comprar/pro?periodo=mensual")
    assert r.status_code == 200
    assert 'action="/comprar"' in r.text
    assert "data-culqi-llave" not in r.text
    assert "pago único" in r.text


@sin_base
async def test_con_plan_sincronizado_el_checkout_si_abre_culqi(cliente, monkeypatch):
    """La otra direccion: con todo puesto, el boton tiene que llevar a Culqi.

    Y el formulario cambia de destino, no de forma: el resumen del pedido, el
    desglose con IGV y el total siguen ahi. Es lo que hace el sitio validable
    desde fuera, y fue lo que costo el primer rechazo de la pasarela.
    """
    from shared.db import connection
    _con_culqi(monkeypatch)
    async with connection() as c:
        await c.execute("UPDATE planes SET culqi_plan_id_mensual=$1 WHERE codigo='pro'",
                        "pln_test_pruebasdelasuite")
    try:
        r = await cliente.get("/comprar/pro?periodo=mensual")
    finally:
        async with connection() as c:
            await c.execute(
                "UPDATE planes SET culqi_plan_id_mensual=NULL WHERE codigo='pro'")

    assert r.status_code == 200
    assert 'action="/comprar/culqi"' in r.text
    assert 'name="token_id"' in r.text
    assert 'data-culqi-centimos="9900"' in r.text, "el importe no va en centimos"
    assert "renueva automáticamente" in r.text
    assert "pago único" not in r.text
    # Lo que no puede desaparecer nunca de esta pagina.
    assert "Resumen del pedido" in r.text
    assert "Total a pagar" in r.text
    assert "Pagar S/ 99.00" in r.text
