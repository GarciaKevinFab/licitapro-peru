"""Pruebas del adaptador de Culqi.

POR QUE NINGUNA LLAMA A CULQI DE VERDAD

  Atar la suite a un tercero la vuelve roja los dias que ellos tengan
  mantenimiento, y eso es como se ensena a la gente a ignorar el rojo. Se
  monta un `httpx.MockTransport` que devuelve las formas DOCUMENTADAS de
  Culqi -- objetos con id pln_/cus_/crd_/sxn_/chr_ y el objeto de error
  {"object":"error", "type":..., "user_message":...} -- y se comprueba lo
  nuestro: la traduccion del error, la puerta de configuracion, los centimos y
  que interval_unit_time no se adivine.

  Las respuestas no se inventan de cero: son las que documentan Culqi y su SDK
  oficial. Una prueba contra una forma imaginada pasa en verde y no protege de
  nada.
"""
import httpx
import pytest

from shared import culqi

# La clase de verdad, guardada antes de que ninguna prueba la sustituya. Sin
# esto, la segunda llamada a `responder` dentro de una misma prueba heredaba de
# la clase ya parcheada y se quedaba con el transporte de la primera: el test
# pedia una tarjeta y recibia el cliente anterior.
_AsyncClientReal = httpx.AsyncClient


# ─── Arnes ───────────────────────────────────────────────

def _entorno(monkeypatch, modo="prueba", publica="pk_test_abc", secreta="sk_test_abc"):
    monkeypatch.setenv("CULQI_MODO", modo)
    monkeypatch.setenv("CULQI_LLAVE_PUBLICA", publica)
    monkeypatch.setenv("CULQI_LLAVE_SECRETA", secreta)


def _falso(respuestas):
    """Un AsyncClient de mentira que devuelve lo que se le diga.

    `respuestas` es una lista de (status, json) que se van consumiendo en
    orden, o un solo par si solo hay una llamada. Se guarda cada peticion en
    `visto` para poder comprobar cabeceras y cuerpo, que es donde estan los dos
    errores caros: mandar la llave publica donde va la secreta, y mandar soles
    donde van centimos.
    """
    if isinstance(respuestas, tuple):
        respuestas = [respuestas]
    visto = []

    def manejar(peticion: httpx.Request) -> httpx.Response:
        visto.append(peticion)
        estado, cuerpo = respuestas[min(len(visto) - 1, len(respuestas) - 1)]
        return httpx.Response(estado, json=cuerpo)

    transporte = httpx.MockTransport(manejar)

    class Cliente(_AsyncClientReal):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    return Cliente, visto


@pytest.fixture
def culqi_falso(monkeypatch):
    """Devuelve una funcion `responder(status, json)` y la lista de peticiones."""
    caja = {"visto": []}

    def responder(*respuestas):
        Cliente, visto = _falso(list(respuestas) if len(respuestas) > 1
                                else respuestas[0])
        monkeypatch.setattr(httpx, "AsyncClient", Cliente)
        caja["visto"] = visto
        return visto

    return responder, caja


# ─── Formas documentadas de Culqi ────────────────────────

PLAN_OK = {"object": "plan", "id": "pln_test_gGjMEBcs2h7BOB3E",
           "short_name": "plan-pro-mensual", "amount": 9900, "currency": "PEN",
           "interval_unit_time": 1, "interval_count": 1, "status": 1}

CLIENTE_OK = {"object": "customer", "id": "cus_test_ZBQMFtCQXGWWSDcq",
              "email": "cliente@ejemplo.pe", "country_code": "PE"}

TARJETA_OK = {"object": "card", "id": "crd_test_lPpdrmLarypcSNuG",
              "customer_id": "cus_test_ZBQMFtCQXGWWSDcq",
              "source": {"object": "token", "id": "tkn_test_4Zbe0mUxDXfHbCwB",
                         "last_four": "1111",
                         "iin": {"card_brand": "Visa", "card_type": "credito"}}}

SUSCRIPCION_OK = {"object": "subscription", "id": "sxn_test_2LmNsgvCUZ0Tcbcp",
                  "plan_id": "pln_test_gGjMEBcs2h7BOB3E",
                  "card_id": "crd_test_lPpdrmLarypcSNuG", "status": 1}

CARGO_OK = {"object": "charge", "id": "chr_test_hVgMx1fUOnMbEbmU",
            "amount": 9900, "currency_code": "PEN", "paid": True,
            "outcome": {"type": "venta_exitosa", "code": "AUT0000",
                        "merchant_message": "La operación de venta ha sido autorizada exitosamente.",
                        "user_message": "Su compra ha sido exitosa."}}

CARGO_DENEGADO = {"object": "charge", "id": "chr_test_denegado000000",
                  "amount": 9900, "currency_code": "PEN",
                  "outcome": {"type": "venta_denegada", "code": "insufficient_funds",
                              "merchant_message": "The card was declined for insufficient funds.",
                              "user_message": "Tu tarjeta no tiene saldo suficiente."}}

ERROR_TARJETA = {"object": "error", "type": "card_error",
                 "charge_id": "chr_test_XXXX", "code": "insufficient_funds",
                 "decline_code": "insufficient_funds",
                 "merchant_message": "The card was declined for insufficient funds.",
                 "user_message": "Tu tarjeta no tiene saldo suficiente."}

ERROR_PARAMETRO = {"object": "error", "type": "parameter_error",
                   "code": "parameter_invalid", "param": "amount",
                   "merchant_message": "The amount parameter is invalid.",
                   "user_message": "El monto no es válido."}


# ─── El modo falla hacia "no cobra nada" ─────────────────

@pytest.mark.parametrize("valor", ["", "  ", "PRODUCCION-ya", "test", "live"])
def test_un_modo_que_no_se_reconoce_no_intenta_cobrar(monkeypatch, valor):
    _entorno(monkeypatch, valor)
    assert culqi.modo() == "simulado"


def test_sin_llaves_no_se_cobra_aunque_el_modo_lo_pida(monkeypatch):
    _entorno(monkeypatch, "prueba")
    monkeypatch.delenv("CULQI_LLAVE_SECRETA")
    assert culqi.modo() == "simulado"


def test_produccion_con_llaves_de_prueba_se_niega(monkeypatch):
    """El fallo silencioso de Culqi: sk_test_ en produccion NO da error.

    Culqi acepta las peticiones -- son validas, solo que contra el entorno de
    pruebas -- y crea suscripciones que no mueven dinero. El dueno cree que
    factura y no entra un sol.
    """
    _entorno(monkeypatch, "produccion", "pk_test_abc", "sk_test_abc")
    with pytest.raises(RuntimeError, match="PRUEBAS"):
        culqi.modo()


def test_el_error_de_llaves_dice_que_hay_que_cambiar_las_dos(monkeypatch):
    _entorno(monkeypatch, "produccion", "pk_test_abc", "sk_test_abc")
    with pytest.raises(RuntimeError) as e:
        culqi.modo()
    texto = str(e.value)
    assert "CULQI_MODO" in texto
    assert "pk_live_" in texto and "sk_live_" in texto


def test_produccion_con_llaves_de_verdad_cobra(monkeypatch):
    _entorno(monkeypatch, "produccion", "pk_live_abc", "sk_live_abc")
    assert culqi.modo() == "produccion"
    assert culqi.activo() is True
    assert culqi.cobro_recurrente() is True


def test_sin_configurar_no_se_promete_renovacion_automatica(monkeypatch):
    """El texto del sitio cuelga de esto y no de una frase escrita a mano."""
    monkeypatch.delenv("CULQI_MODO", raising=False)
    monkeypatch.delenv("CULQI_LLAVE_PUBLICA", raising=False)
    monkeypatch.delenv("CULQI_LLAVE_SECRETA", raising=False)
    assert culqi.cobro_recurrente() is False


def test_mal_configurado_tampoco_promete_renovacion(monkeypatch):
    """modo() levanta RuntimeError; el texto no puede reventar la pagina."""
    _entorno(monkeypatch, "produccion", "pk_test_abc", "sk_test_abc")
    assert culqi.cobro_recurrente() is False


@pytest.mark.parametrize("publica, secreta, prueba", [
    ("pk_test_x", "sk_test_y", True),
    ("pk_live_x", "sk_live_y", False),
    ("", "", False),
])
def test_se_reconoce_una_llave_de_prueba(monkeypatch, publica, secreta, prueba):
    monkeypatch.setenv("CULQI_LLAVE_PUBLICA", publica)
    monkeypatch.setenv("CULQI_LLAVE_SECRETA", secreta)
    assert culqi.llaves_de_prueba() is prueba


# ─── Centimos ────────────────────────────────────────────

@pytest.mark.parametrize("soles, centimos", [
    ("99.00", 9900), ("49.00", 4900), ("990.00", 99000), ("1990.00", 199000),
    ("0.01", 1), ("33.33", 3333),
])
def test_el_importe_viaja_en_centimos(soles, centimos):
    assert culqi.a_centimos(soles) == centimos


def test_los_centimos_no_pasan_por_coma_flotante():
    """int(98.99999999 * 100) da 9899: un centimo perdido en cada cobro.

    Un NUMERIC de Postgres llega a Python como Decimal, pero basta que alguien
    lo pase por float por el camino para que aparezca ese arrastre. No revienta
    nada: solo cobra de menos y descuadra la contabilidad.
    """
    assert culqi.a_centimos(98.99999999999999) == 9900
    assert culqi.a_centimos(0.1 + 0.2) == 30


def test_los_centimos_vuelven_a_soles():
    assert str(culqi.a_soles(9900)) == "99.00"


# ─── interval_unit_time: no se adivina ───────────────────

def test_sin_declarar_el_intervalo_no_se_crea_ningun_plan(monkeypatch):
    """POR_CONFIRMAR de verdad: adivinar aqui no da error, da cobros mal hechos.

    Un plan mensual creado como diario cobra treinta veces antes de que nadie
    lo note. Uno anual creado como mensual no factura durante once meses.
    Ninguno de los dos avisa, asi que se exige declararlo.
    """
    monkeypatch.delenv("CULQI_INTERVALO_MENSUAL", raising=False)
    with pytest.raises(culqi.ConfiguracionCulqi, match="CULQI_INTERVALO_MENSUAL"):
        culqi.intervalo_de("mensual")


def test_el_intervalo_declarado_se_respeta(monkeypatch):
    monkeypatch.setenv("CULQI_INTERVALO_MENSUAL", "2")
    monkeypatch.setenv("CULQI_INTERVALO_ANUAL", "3")
    assert culqi.intervalo_de("mensual") == 2
    assert culqi.intervalo_de("anual") == 3


def test_un_intervalo_que_no_es_numero_no_pasa(monkeypatch):
    monkeypatch.setenv("CULQI_INTERVALO_MENSUAL", "mensual")
    with pytest.raises(culqi.ConfiguracionCulqi):
        culqi.intervalo_de("mensual")


async def test_crear_plan_sin_intervalo_no_llega_a_llamar_a_culqi(monkeypatch, culqi_falso):
    """La puerta se cierra ANTES de la peticion, no despues.

    Si se llamara y se fallara despues, quedaria un plan a medias en Culqi con
    la frecuencia equivocada y habria que ir a borrarlo a mano.
    """
    _entorno(monkeypatch)
    monkeypatch.delenv("CULQI_INTERVALO_MENSUAL", raising=False)
    responder, _caja = culqi_falso
    visto = responder((201, PLAN_OK))
    with pytest.raises(culqi.ConfiguracionCulqi):
        await culqi.crear_plan("Pro", "plan-pro-mensual", "Pro mensual",
                               "99.00", "mensual")
    assert visto == [], "se llamo a Culqi sin saber la frecuencia del plan"


# ─── Camino feliz: lo que se manda y con que llave ───────

async def test_el_plan_se_manda_en_centimos_y_con_la_llave_secreta(
        monkeypatch, culqi_falso):
    _entorno(monkeypatch, secreta="sk_test_SECRETA")
    monkeypatch.setenv("CULQI_INTERVALO_MENSUAL", "2")
    responder, _ = culqi_falso
    visto = responder((201, PLAN_OK))

    plan = await culqi.crear_plan("Pro", "plan-pro-mensual", "Pro mensual",
                                  "99.00", "mensual")
    assert plan["id"].startswith("pln_")

    pedido = visto[0]
    assert pedido.url.path == "/v2/recurrent/plans"
    assert pedido.headers["authorization"] == "Bearer sk_test_SECRETA"
    import json
    cuerpo = json.loads(pedido.content)
    assert cuerpo["amount"] == 9900, "el importe tiene que ir en centimos"
    assert cuerpo["currency"] == "PEN"
    assert cuerpo["interval_unit_time"] == 2
    assert cuerpo["initial_cycles"]["has_initial_charge"] is False


async def test_cliente_tarjeta_y_suscripcion_devuelven_sus_identificadores(
        monkeypatch, culqi_falso):
    _entorno(monkeypatch)
    responder, _ = culqi_falso

    responder((201, CLIENTE_OK))
    cliente = await culqi.crear_cliente("cliente@ejemplo.pe", "Kevin")
    assert cliente["id"].startswith("cus_")

    responder((201, TARJETA_OK))
    tarjeta = await culqi.crear_tarjeta(cliente["id"], "tkn_test_4Zbe0mUxDXfHbCwB")
    assert tarjeta["id"].startswith("crd_")
    assert culqi.marca_y_ultimos(tarjeta) == ("Visa", "1111")

    responder((201, SUSCRIPCION_OK))
    susc = await culqi.crear_suscripcion(tarjeta["id"], PLAN_OK["id"])
    assert susc["id"].startswith("sxn_")


async def test_la_suscripcion_declara_la_aceptacion_de_terminos(
        monkeypatch, culqi_falso):
    """`tyc` es la constancia del consentimiento para un cobro recurrente."""
    _entorno(monkeypatch)
    responder, _ = culqi_falso
    visto = responder((201, SUSCRIPCION_OK))
    await culqi.crear_suscripcion("crd_test_x", "pln_test_y",
                                  metadata={"usuario_id": 7})
    import json
    cuerpo = json.loads(visto[0].content)
    assert cuerpo["tyc"] is True
    assert cuerpo["metadata"] == {"usuario_id": 7}


# ─── Traduccion de errores ───────────────────────────────

async def test_un_rechazo_de_tarjeta_llega_con_el_texto_para_el_cliente(
        monkeypatch, culqi_falso):
    """El cliente ve `user_message`, el log ve `merchant_message`.

    Ensenar el segundo en la pantalla de pago es la forma habitual de que
    alguien vea ingles tecnico y abandone la compra.
    """
    _entorno(monkeypatch)
    responder, _ = culqi_falso
    responder((400, ERROR_TARJETA))

    with pytest.raises(culqi.ErrorCulqi) as e:
        await culqi.crear_suscripcion("crd_test_x", "pln_test_y")

    err = e.value
    assert err.user_message == "Tu tarjeta no tiene saldo suficiente."
    assert "declined" in err.merchant_message
    assert err.tipo == "card_error"
    assert err.codigo == "insufficient_funds"
    assert err.http == 400


async def test_un_error_de_parametro_tambien_se_traduce(monkeypatch, culqi_falso):
    _entorno(monkeypatch)
    responder, _ = culqi_falso
    responder((400, ERROR_PARAMETRO))
    with pytest.raises(culqi.ErrorCulqi) as e:
        await culqi.crear_cliente("cliente@ejemplo.pe")
    assert e.value.user_message == "El monto no es válido."
    assert e.value.tipo == "parameter_error"


async def test_un_error_de_culqi_con_200_tambien_es_un_error(monkeypatch, culqi_falso):
    """`object: "error"` manda por encima del codigo HTTP.

    Si solo se mirara el status, un 200 con cuerpo de error pasaria por bueno y
    activariamos una suscripcion que Culqi rechazo.
    """
    _entorno(monkeypatch)
    responder, _ = culqi_falso
    responder((200, ERROR_TARJETA))
    with pytest.raises(culqi.ErrorCulqi):
        await culqi.crear_suscripcion("crd_test_x", "pln_test_y")


async def test_un_error_sin_user_message_no_ensena_json_al_cliente(
        monkeypatch, culqi_falso):
    """Un 500 de Culqi o un proxy por medio no puede acabar en la pantalla."""
    _entorno(monkeypatch)
    responder, _ = culqi_falso
    responder((502, {"object": "error", "type": "api_error"}))
    with pytest.raises(culqi.ErrorCulqi) as e:
        await culqi.crear_cliente("cliente@ejemplo.pe")
    assert "{" not in e.value.user_message
    assert "Intenta de nuevo" in e.value.user_message


async def test_sin_llave_secreta_no_se_llama_a_culqi(monkeypatch, culqi_falso):
    _entorno(monkeypatch)
    monkeypatch.setenv("CULQI_LLAVE_SECRETA", "")
    responder, _ = culqi_falso
    visto = responder((201, CLIENTE_OK))
    # modo() cae a simulado sin llaves, asi que se fuerza la ruta real.
    monkeypatch.setattr(culqi, "modo", lambda: "prueba")
    with pytest.raises(culqi.ConfiguracionCulqi):
        await culqi.crear_cliente("cliente@ejemplo.pe")
    assert visto == []


# ─── Reintentos: solo donde repetir es inofensivo ────────

async def test_un_post_que_expira_leyendo_no_se_repite(monkeypatch):
    """Repetirlo crearia una segunda suscripcion cobrando dos veces.

    En un ReadTimeout la peticion SI viajo: puede haberse procesado. Es
    exactamente el caso en el que un reintento "por si acaso" cobra doble.
    """
    _entorno(monkeypatch)
    llamadas = []

    def manejar(peticion):
        llamadas.append(peticion)
        raise httpx.ReadTimeout("tardo demasiado", request=peticion)

    _instalar(monkeypatch, httpx.MockTransport(manejar))
    with pytest.raises(culqi.ErrorCulqi):
        await culqi.crear_suscripcion("crd_test_x", "pln_test_y")
    assert len(llamadas) == 1, "un POST que pudo procesarse se repitio"


async def test_un_post_que_no_llego_a_conectar_si_se_repite(monkeypatch):
    """Sin conexion establecida Culqi no vio nada: repetir es seguro."""
    _entorno(monkeypatch)
    llamadas = []

    def manejar(peticion):
        llamadas.append(peticion)
        if len(llamadas) < 3:
            raise httpx.ConnectError("sin ruta", request=peticion)
        return httpx.Response(201, json=SUSCRIPCION_OK)

    _instalar(monkeypatch, httpx.MockTransport(manejar))
    susc = await culqi.crear_suscripcion("crd_test_x", "pln_test_y")
    assert susc["id"] == SUSCRIPCION_OK["id"]
    assert len(llamadas) == 3


async def test_una_lectura_si_se_repite_ante_un_500(monkeypatch):
    """Leer un cargo es idempotente: reintentarlo no cobra nada."""
    _entorno(monkeypatch)
    llamadas = []

    def manejar(peticion):
        llamadas.append(peticion)
        if len(llamadas) == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=CARGO_OK)

    _instalar(monkeypatch, httpx.MockTransport(manejar))
    cargo = await culqi.leer_cargo("chr_test_hVgMx1fUOnMbEbmU")
    assert cargo["id"] == CARGO_OK["id"]
    assert len(llamadas) == 2


def _instalar(monkeypatch, transporte):
    class Cliente(_AsyncClientReal):
        def __init__(self, *a, **kw):
            kw["transport"] = transporte
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Cliente)


# ─── Lectura de un cargo ─────────────────────────────────

def test_solo_venta_exitosa_cuenta_como_cobrado():
    assert culqi.cargo_exitoso(CARGO_OK) is True
    assert culqi.cargo_exitoso(CARGO_DENEGADO) is False


@pytest.mark.parametrize("cargo", [
    {},                                             # sin outcome
    {"outcome": None},
    {"outcome": "venta_exitosa"},                   # outcome que no es objeto
    {"outcome": {"type": "estado_que_no_conocemos"}},
    {"paid": True},                                 # un campo que suena bien
])
def test_lo_que_no_se_reconoce_no_cuenta_como_cobrado(cargo):
    """Lista blanca, no lista negra.

    Equivocarse hacia "no cobro" retrasa una activacion y lo arregla el
    siguiente aviso. Equivocarse hacia "si cobro" regala un mes a quien no
    pago, y nadie lo reclama.
    """
    assert culqi.cargo_exitoso(cargo) is False


def test_una_tarjeta_sin_la_estructura_esperada_no_revienta():
    """Que falte un nivel deja la tarjeta sin etiqueta, no tumba el pago."""
    assert culqi.marca_y_ultimos({}) == (None, None)
    assert culqi.marca_y_ultimos({"source": {}}) == (None, None)


# ─── Modo simulado: el flujo entero sin llaves ───────────

async def test_en_simulado_no_se_toca_la_red(monkeypatch):
    """Es lo que permite probar checkout, webhook y cancelacion sin llaves."""
    monkeypatch.delenv("CULQI_MODO", raising=False)

    def prohibido(*a, **kw):
        raise AssertionError("el modo simulado llamo a la red")

    _instalar(monkeypatch, httpx.MockTransport(prohibido))

    cliente = await culqi.crear_cliente("cliente@ejemplo.pe")
    tarjeta = await culqi.crear_tarjeta(cliente["id"], "tkn_x")
    susc = await culqi.crear_suscripcion(tarjeta["id"], "pln_x")
    cargo = await culqi.leer_cargo("chr_x")
    assert cliente["id"].startswith("cus_sim_")
    assert tarjeta["id"].startswith("crd_sim_")
    assert susc["id"].startswith("sxn_sim_")
    assert culqi.cargo_exitoso(cargo) is True


async def test_los_identificadores_simulados_se_ven_a_simple_vista(monkeypatch):
    """Si uno se colara en produccion tiene que cantar en el log."""
    monkeypatch.delenv("CULQI_MODO", raising=False)
    cliente = await culqi.crear_cliente("cliente@ejemplo.pe")
    assert "_sim_" in cliente["id"]
    assert cliente["simulado"] is True


# ─── El comando de planes no crea nada al importarse ─────

def test_importar_el_comando_de_planes_no_crea_planes():
    """Mismo guardia que `tools/renovar_suscripciones.py`, y por lo mismo.

    Alli, un `asyncio.run(main())` suelto a nivel de modulo convertia un import
    en una ronda de cobros a todos los clientes. Aqui convertiria un import en
    una tanda de planes creados en Culqi -- y Culqi NO deja borrar un plan con
    suscripciones vivas, asi que la basura se queda en el panel para siempre.

    Se comprueba leyendo el fichero y no importandolo: importarlo es
    precisamente lo que no debe tener efectos, y una prueba que lo hiciera
    crearia los planes de verdad si el guardia desapareciera.
    """
    import pathlib
    fuente = (pathlib.Path(__file__).resolve().parent.parent
              / "tools" / "culqi_planes.py").read_text(encoding="utf-8")

    for linea in fuente.splitlines():
        if "asyncio.run(" in linea and not linea.lstrip().startswith("#"):
            assert linea.startswith("    "), (
                "asyncio.run() esta a nivel de modulo: importar "
                "tools/culqi_planes crearia planes en Culqi")
    assert 'if __name__ == "__main__":' in fuente, "falta el guardia de __main__"


def test_el_short_name_del_plan_sale_del_codigo_y_no_del_nombre():
    """El nombre visible cambia ("Pro" -> "Pro 2026") y el short_name quedaria
    describiendo otra cosa. El codigo de la tabla es la identidad estable."""
    from tools.culqi_planes import short_name
    assert short_name("pro", "mensual") == "plan-pro-mensual"
    assert short_name("empresa", "anual") == "plan-empresa-anual"
    # Sin tildes ni mayusculas: Culqi trata short_name como identificador.
    assert short_name("Básico", "mensual") == "plan-basico-mensual"
