"""Pruebas del receptor de avisos de Culqi.

LO QUE SE PROTEGE AQUI, EN UNA FRASE

  Que el cuerpo del aviso no decida nada.

  De Culqi no se ha podido verificar ni la forma del cuerpo del webhook ni si
  trae firma. El receptor esta hecho para no necesitarlo: saca un identificador
  y pregunta a la API con la llave secreta. Estas pruebas comprueban justamente
  eso -- que un aviso que dice "pagado" sin respaldo en la API no active nada --
  y la idempotencia, que es lo que separa "un mes de servicio" de "tres".
"""
import pytest

from tests.conftest import sin_base
from web import webhooks_culqi as wh


# ─── Formas documentadas de Culqi ────────────────────────

CARGO_OK = {
    "object": "charge", "id": "chr_test_hVgMx1fUOnMbEbmU",
    "amount": 9900, "currency_code": "PEN",
    "email": "cliente@ejemplo.pe",
    "outcome": {"type": "venta_exitosa", "code": "AUT0000",
                "user_message": "Su compra ha sido exitosa."},
}

CARGO_DENEGADO = {
    "object": "charge", "id": "chr_test_denegado00000",
    "amount": 9900, "currency_code": "PEN",
    "email": "cliente@ejemplo.pe",
    "outcome": {"type": "venta_denegada", "code": "insufficient_funds",
                "user_message": "Tu tarjeta no tiene saldo suficiente."},
}


# ─── El extractor de identificadores ─────────────────────

def test_se_encuentra_el_cargo_este_donde_este():
    """El campo que lo lleva es POR_CONFIRMAR; la forma del id no lo es.

    Buscar en un campo concreto significa que el dia que no acertemos -- o que
    Culqi lo cambie -- los cobros dejarian de aplicarse EN SILENCIO: las
    cuentas irian venciendo una a una con el dinero ya cobrado.
    """
    for cuerpo in (
        {"data": {"id": "chr_test_hVgMx1fUOnMbEbmU"}},
        {"object": "event", "type": "charge.creation",
         "data": {"object": "charge", "id": "chr_test_hVgMx1fUOnMbEbmU"}},
        {"charge_id": "chr_test_hVgMx1fUOnMbEbmU"},
        [{"cosas": [{"x": "chr_test_hVgMx1fUOnMbEbmU"}]}],
    ):
        assert wh.identificadores(cuerpo)["cargo"] == "chr_test_hVgMx1fUOnMbEbmU"


def test_se_distinguen_las_cuatro_clases_de_identificador():
    ids = wh.identificadores({
        "id": "evt_test_aaaaaaaaaa",
        "data": {"id": "chr_test_bbbbbbbbbb",
                 "subscription": "sxn_test_cccccccccc",
                 "customer_id": "cus_test_dddddddddd"}})
    assert ids == {"evento": "evt_test_aaaaaaaaaa",
                   "cargo": "chr_test_bbbbbbbbbb",
                   "suscripcion": "sxn_test_cccccccccc",
                   "cliente": "cus_test_dddddddddd"}


@pytest.mark.parametrize("cuerpo", [
    {}, {"id": "chr_"}, {"id": "chr_test_"}, {"id": "algo chr_test_abcdefgh"},
    {"id": "pln_test_abcdefgh"},          # un plan no es un cargo
    {"texto": "chr_prod_abcdefgh"},       # entorno que Culqi no usa
])
def test_lo_que_no_tiene_forma_de_identificador_no_cuela(cuerpo):
    assert "cargo" not in wh.identificadores(cuerpo)


def test_un_cuerpo_enorme_no_nos_tiene_recorriendo_para_siempre():
    """Un aviso hecho a proposito para agotarnos se corta por el tope."""
    hondo = actual = {}
    for _ in range(20000):
        actual["mas"] = {}
        actual = actual["mas"]
    actual["id"] = "chr_test_nuncaSeVera"
    assert wh.identificadores(hondo) == {}


def test_el_correo_se_saca_de_donde_este():
    assert wh._correo_de(CARGO_OK) == "cliente@ejemplo.pe"
    assert wh._correo_de({"a": {"b": {"email": "otro@ejemplo.pe"}}}) == "otro@ejemplo.pe"
    assert wh._correo_de({"email": "no-es-un-correo"}) is None


# ─── El aviso no decide: decide la API ───────────────────

class _Suscripcion(dict):
    pass


@pytest.fixture
def culqi_apagado(monkeypatch):
    monkeypatch.setattr(wh.culqi, "activo", lambda: False)


@pytest.fixture
def culqi_encendido(monkeypatch):
    """Culqi 'activo' con la API sustituida por lo que cada prueba diga."""
    monkeypatch.setattr(wh.culqi, "activo", lambda: True)
    caja = {"cargos": {}, "eventos": {}, "aplicados": [], "fallidos": [],
            "susc": _Suscripcion({"id": 1, "usuario_id": 7, "periodo": "mensual",
                                  "email": "cliente@ejemplo.pe"})}

    async def leer_cargo(charge_id):
        if charge_id not in caja["cargos"]:
            raise wh.culqi.ErrorCulqi("No existe", "charge not found", http=404)
        return caja["cargos"][charge_id]

    async def leer_evento(event_id):
        if event_id not in caja["eventos"]:
            raise wh.culqi.ErrorCulqi("No existe", "event not found", http=404)
        return caja["eventos"][event_id]

    async def suscripcion_por_culqi(**kw):
        return caja["susc"]

    async def aplicar(**kw):
        caja["aplicados"].append(kw)
        return "aplicado"

    async def fallido(usuario_id):
        caja["fallidos"].append(usuario_id)

    monkeypatch.setattr(wh.culqi, "leer_cargo", leer_cargo)
    monkeypatch.setattr(wh.culqi, "leer_evento", leer_evento)
    monkeypatch.setattr(wh, "suscripcion_por_culqi", suscripcion_por_culqi)
    monkeypatch.setattr(wh, "aplicar_cargo_culqi", aplicar)
    monkeypatch.setattr(wh, "registrar_intento_fallido", fallido)
    return caja


async def test_un_aviso_que_dice_pagado_sin_respaldo_no_activa_nada(
        cliente, culqi_encendido):
    """El nucleo del asunto.

    Cualquiera que descubra la URL puede mandar esto. Sin la comprobacion
    contra la API, seria una forma de regalarse meses de servicio: el aviso
    afirma que el cargo existe y esta pagado, y la API no lo conoce.
    """
    r = await cliente.post("/webhooks/culqi", json={
        "object": "event", "type": "charge.succeeded",
        "data": {"id": "chr_test_inventado00", "amount": 999900,
                 "outcome": {"type": "venta_exitosa"}, "paid": True}})
    assert r.status_code == 200
    assert r.json()["aplicado"] is False
    assert culqi_encendido["aplicados"] == []


async def test_un_cargo_que_la_api_confirma_si_se_aplica(cliente, culqi_encendido):
    culqi_encendido["cargos"][CARGO_OK["id"]] = CARGO_OK
    r = await cliente.post("/webhooks/culqi",
                           json={"data": {"id": CARGO_OK["id"]}})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "aplicado": True, "resultado": "aplicado"}
    aplicado = culqi_encendido["aplicados"][0]
    assert aplicado["charge_id"] == CARGO_OK["id"]
    # El importe sale del cargo COMPROBADO, no del cuerpo del aviso.
    assert str(aplicado["monto"]) == "99.00"


def test_el_importe_no_sale_nunca_del_cuerpo_del_aviso():
    """Documenta la regla que la prueba de arriba comprueba de refilon.

    Si el monto saliera del aviso, quien lo mande elige cuanto "pago".
    """
    fuente = (__import__("pathlib").Path(wh.__file__)).read_text(encoding="utf-8")
    assert "cargo.get(\"amount\")" in fuente
    assert "cuerpo.get(\"amount\")" not in fuente


async def test_un_cobro_denegado_se_procesa_y_devuelve_200(cliente, culqi_encendido):
    """200, no 4xx: un cobro rechazado no es un fallo NUESTRO.

    Culqi reintenta lo que no recibe 200 y acaba desactivando el webhook. Un
    rechazo de tarjeta no puede costarnos el canal entero.
    """
    culqi_encendido["cargos"][CARGO_DENEGADO["id"]] = CARGO_DENEGADO
    r = await cliente.post("/webhooks/culqi",
                           json={"data": {"id": CARGO_DENEGADO["id"]}})
    assert r.status_code == 200
    assert r.json()["cobrado"] is False
    assert culqi_encendido["fallidos"] == [7]
    assert culqi_encendido["aplicados"] == []


async def test_sin_cargo_a_la_vista_se_pide_el_evento(cliente, culqi_encendido):
    """El aviso puede traer solo el evt_; el cargo esta dentro del evento."""
    culqi_encendido["cargos"][CARGO_OK["id"]] = CARGO_OK
    culqi_encendido["eventos"]["evt_test_aaaaaaaaaa"] = {
        "object": "event", "id": "evt_test_aaaaaaaaaa",
        "type": "charge.creation", "data": CARGO_OK}
    r = await cliente.post("/webhooks/culqi", json={"id": "evt_test_aaaaaaaaaa"})
    assert r.status_code == 200
    assert r.json()["aplicado"] is True
    assert culqi_encendido["aplicados"][0]["event_id"] == "evt_test_aaaaaaaaaa"


async def test_un_aviso_sin_cargo_no_toca_el_vencimiento(cliente, culqi_encendido):
    """Hay avisos que no son de cobro: una tarjeta nueva, una suscripcion
    creada. Ni son un error ni cambian nada."""
    r = await cliente.post("/webhooks/culqi",
                           json={"data": {"id": "sxn_test_cccccccccc"}})
    assert r.status_code == 200
    assert r.json()["aplicado"] is False
    assert culqi_encendido["aplicados"] == []


async def test_un_cargo_que_no_es_de_ningun_cliente_nuestro_no_revienta(
        cliente, culqi_encendido, monkeypatch):
    """Puede ser de otro producto del mismo comercio. 200 y un log en ERROR."""
    culqi_encendido["cargos"][CARGO_OK["id"]] = CARGO_OK
    culqi_encendido["susc"] = None
    r = await cliente.post("/webhooks/culqi",
                           json={"data": {"id": CARGO_OK["id"]}})
    assert r.status_code == 200
    assert r.json()["motivo"] == "sin suscripcion asociada"


async def test_con_la_pasarela_apagada_no_se_aplica_nada(cliente, culqi_apagado):
    """Sin llaves no hay con que comprobar, y creerse el cuerpo es justo lo
    que este modulo existe para no hacer."""
    r = await cliente.post("/webhooks/culqi",
                           json={"data": {"id": CARGO_OK["id"]}})
    assert r.status_code == 200
    assert r.json()["aplicado"] is False


# ─── Lo que si merece un 4xx ─────────────────────────────

async def test_un_cuerpo_que_no_es_json_se_rechaza(cliente):
    r = await cliente.post("/webhooks/culqi", content=b"esto no es json",
                           headers={"content-type": "application/json"})
    assert r.status_code == 400


async def test_un_aviso_sin_identificadores_se_rechaza(cliente):
    """Aqui si conviene que Culqi lo sepa: no hay nada que comprobar."""
    r = await cliente.post("/webhooks/culqi", json={"hola": "que tal"})
    assert r.status_code == 400


async def test_el_get_contesta_al_validador_del_panel(cliente):
    """Culqi comprueba la URL antes de aceptarla. No abre nada: es una
    constante que no lee la peticion ni toca la base."""
    r = await cliente.get("/webhooks/culqi")
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_el_webhook_no_pide_estar_al_dia_de_pago():
    """Un webhook que exigiera suscripcion activa no podria confirmar el pago
    que la activa. Circular y silencioso."""
    from shared.suscripciones import ruta_libre
    assert ruta_libre("/webhooks/culqi") is True


# ─── Idempotencia, contra la base de verdad ──────────────

@sin_base
async def test_un_aviso_repetido_no_extiende_dos_veces(usuario):
    """Culqi reintenta lo que no recibe 200: el mismo cobro llega varias veces.

    Cada aplicacion de mas es un mes de servicio regalado. No da error, no
    aparece en ningun log y nadie lo reclama.
    """
    from shared.db import connection
    from shared.suscripciones import aplicar_cargo_culqi

    async with connection() as c:
        susc_id = await c.fetchval(
            """UPDATE suscripciones SET estado='activa', periodo='mensual',
                      vence = NOW() + INTERVAL '3 days'
                WHERE usuario_id=$1 RETURNING id""", usuario["id"])
        antes = await c.fetchval("SELECT vence FROM suscripciones WHERE id=$1",
                                 susc_id)

    cargo = "chr_test_repetido00001"
    assert await aplicar_cargo_culqi(susc_id, "99.00", cargo) == "aplicado"
    async with connection() as c:
        primera = await c.fetchval("SELECT vence FROM suscripciones WHERE id=$1",
                                   susc_id)
    assert (primera - antes).days == 30

    for _ in range(3):
        assert await aplicar_cargo_culqi(susc_id, "99.00", cargo) == "repetido"

    async with connection() as c:
        final = await c.fetchval("SELECT vence FROM suscripciones WHERE id=$1",
                                 susc_id)
        pagos = await c.fetchval(
            "SELECT COUNT(*) FROM pagos_suscripcion WHERE culqi_charge_id=$1",
            cargo)
    assert final == primera, "un aviso repetido volvio a extender el periodo"
    assert pagos == 1


@sin_base
async def test_el_aviso_del_primer_cobro_se_adopta_y_no_regala_un_periodo(usuario):
    """El caso que regalaria treinta dias a TODO el que contrate.

    El checkout concede el periodo al activar. El aviso de ese mismo primer
    cobro llega despues: si se contara como cobro nuevo, cada cliente recibiria
    dos periodos por un pago.
    """
    from shared.db import connection
    from shared.suscripciones import activar_por_culqi, aplicar_cargo_culqi

    assert await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_x", card_id="crd_test_x",
        subscription_id="sxn_test_x")

    async with connection() as c:
        susc_id = await c.fetchval(
            "SELECT id FROM suscripciones WHERE usuario_id=$1", usuario["id"])
        tras_alta = await c.fetchval(
            "SELECT vence FROM suscripciones WHERE id=$1", susc_id)

    cargo = "chr_test_primercobro1"
    assert await aplicar_cargo_culqi(susc_id, "99.00", cargo) == "adoptado"

    async with connection() as c:
        despues = await c.fetchval(
            "SELECT vence FROM suscripciones WHERE id=$1", susc_id)
        pagos = await c.fetchval(
            "SELECT COUNT(*) FROM pagos_suscripcion WHERE suscripcion_id=$1",
            susc_id)
        con_cargo = await c.fetchval(
            "SELECT culqi_charge_id FROM pagos_suscripcion WHERE suscripcion_id=$1",
            susc_id)
    assert despues == tras_alta, "el primer aviso regalo un periodo de mas"
    assert pagos == 1, "el primer cobro quedo duplicado en el historial"
    assert con_cargo == cargo, "el pago del checkout no adopto su chr_"


@sin_base
async def test_la_renovacion_siguiente_si_extiende(usuario):
    """Adoptar el primero no puede dejar sordo al segundo."""
    from shared.db import connection
    from shared.suscripciones import activar_por_culqi, aplicar_cargo_culqi

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_x", card_id="crd_test_x",
        subscription_id="sxn_test_y")
    async with connection() as c:
        susc_id = await c.fetchval(
            "SELECT id FROM suscripciones WHERE usuario_id=$1", usuario["id"])

    assert await aplicar_cargo_culqi(susc_id, "99.00", "chr_test_alta00000001") == "adoptado"
    async with connection() as c:
        tras_alta = await c.fetchval(
            "SELECT vence FROM suscripciones WHERE id=$1", susc_id)

    assert await aplicar_cargo_culqi(susc_id, "99.00", "chr_test_renov00000001") == "aplicado"
    async with connection() as c:
        tras_renovar = await c.fetchval(
            "SELECT vence FROM suscripciones WHERE id=$1", susc_id)
    assert (tras_renovar - tras_alta).days == 30


@sin_base
async def test_la_suscripcion_se_encuentra_por_sxn_por_cus_y_por_correo(usuario):
    """Tres vias porque la forma del aviso es POR_CONFIRMAR y no se puede
    exigir un campo concreto."""
    from shared.suscripciones import activar_por_culqi, suscripcion_por_culqi

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_busca1", card_id="crd_test_x",
        subscription_id="sxn_test_busca1")

    por_sxn = await suscripcion_por_culqi(subscription_id="sxn_test_busca1")
    por_cus = await suscripcion_por_culqi(customer_id="cus_test_busca1")
    por_mail = await suscripcion_por_culqi(email=usuario["email"].upper())
    assert por_sxn and por_cus and por_mail
    assert por_sxn["usuario_id"] == por_cus["usuario_id"] == por_mail["usuario_id"]
    assert por_sxn["usuario_id"] == usuario["id"]

    assert await suscripcion_por_culqi(subscription_id="sxn_test_de_otro") is None
    assert await suscripcion_por_culqi() is None
