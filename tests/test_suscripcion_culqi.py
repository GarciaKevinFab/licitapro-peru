"""Cancelar y cambiar de plan cuando el cobro lo hace Culqi sola.

LO QUE SE PROTEGE

  Las dos formas de quedarse desincronizado con la pasarela, que son las dos
  que el cliente paga:

    - Cancelado aqui y vivo alli: Culqi le sigue cobrando cada periodo a
      alguien que ve "cancelada" en su cuenta.
    - Dos suscripciones vivas alli tras un cambio de plan: dos cobros
      recurrentes que el cliente descubre en su extracto, un mes despues.
"""
import pytest

from tests.conftest import sin_base
from web import suscripcion as vista


class _CulqiFalso:
    """Doble de la pasarela: apunta lo que se le pide y falla cuando se le dice."""

    def __init__(self, falla_cancelar=False, falla_crear=False):
        self.canceladas, self.creadas = [], []
        self.falla_cancelar, self.falla_crear = falla_cancelar, falla_crear

    def cobro_recurrente(self):
        return True

    async def cancelar_suscripcion(self, sxn):
        if self.falla_cancelar:
            raise vista.culqi.ErrorCulqi("No se pudo cancelar", "api down", http=500)
        self.canceladas.append(sxn)
        return {"id": sxn, "deleted": True}

    async def crear_suscripcion(self, card_id, plan_id, metadata=None):
        if self.falla_crear:
            raise vista.culqi.ErrorCulqi("Tu tarjeta fue rechazada.",
                                         "card declined", http=400)
        nuevo = f"sxn_test_nueva{len(self.creadas)}"
        self.creadas.append({"card_id": card_id, "plan_id": plan_id,
                             "metadata": metadata, "id": nuevo})
        return {"id": nuevo, "object": "subscription"}

    # Lo que la vista usa tal cual del modulo real.
    ErrorCulqi = vista.culqi.ErrorCulqi


def _instalar(monkeypatch, falso):
    monkeypatch.setattr(vista, "culqi", falso)


async def _entrar(cliente, usuario):
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303, r.text
    return r


@sin_base
async def test_cancelar_cancela_tambien_en_culqi(cliente, usuario, monkeypatch):
    """Marcarlo solo aqui dejaria a Culqi cobrando a alguien que ve 'cancelada'."""
    from shared.db import connection
    from shared.suscripciones import activar_por_culqi

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_c", card_id="crd_test_c",
        subscription_id="sxn_test_cancelame")

    falso = _CulqiFalso()
    _instalar(monkeypatch, falso)
    await _entrar(cliente, usuario)
    r = await cliente.post("/suscripcion/cancelar")

    assert r.status_code == 303
    assert falso.canceladas == ["sxn_test_cancelame"]
    async with connection() as c:
        fila = await c.fetchrow(
            """SELECT estado, culqi_subscription_id, culqi_card_id
                 FROM suscripciones WHERE usuario_id=$1""", usuario["id"])
    assert fila["estado"] == "cancelada"
    assert fila["culqi_subscription_id"] is None
    # La tarjeta se conserva: es lo que permite volver sin pedirsela otra vez.
    assert fila["culqi_card_id"] == "crd_test_c"


@sin_base
async def test_si_culqi_no_cancela_aqui_tampoco_se_cancela(cliente, usuario,
                                                           monkeypatch):
    """La peor equivocacion posible: pantalla que dice 'cancelada' y cobro vivo.

    Se prefiere no cancelar nada y decirlo, que dejar al cliente creyendo que
    dejo de pagar mientras le siguen cargando cada periodo.
    """
    from shared.db import connection
    from shared.suscripciones import activar_por_culqi

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_c", card_id="crd_test_c",
        subscription_id="sxn_test_nocancela")

    _instalar(monkeypatch, _CulqiFalso(falla_cancelar=True))
    await _entrar(cliente, usuario)
    r = await cliente.post("/suscripcion/cancelar")

    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    async with connection() as c:
        fila = await c.fetchrow(
            "SELECT estado, culqi_subscription_id FROM suscripciones WHERE usuario_id=$1",
            usuario["id"])
    assert fila["estado"] == "activa", "se marco cancelada con el cobro vivo en Culqi"
    assert fila["culqi_subscription_id"] == "sxn_test_nocancela"


@sin_base
async def test_cancelar_sin_suscripcion_de_culqi_sigue_funcionando(cliente, usuario,
                                                                   monkeypatch):
    """Los clientes de Izipay y los pagos manuales no se pueden romper."""
    from shared.db import connection

    falso = _CulqiFalso()
    _instalar(monkeypatch, falso)
    await _entrar(cliente, usuario)
    r = await cliente.post("/suscripcion/cancelar")

    assert r.status_code == 303
    assert falso.canceladas == [], "se llamo a Culqi sin tener nada alli"
    async with connection() as c:
        estado = await c.fetchval(
            "SELECT estado FROM suscripciones WHERE usuario_id=$1", usuario["id"])
    assert estado == "cancelada"


@sin_base
async def test_cambiar_de_plan_cancela_la_vieja_y_crea_la_nueva(cliente, usuario,
                                                                monkeypatch):
    """En Culqi no hay 'cambiale el plan': se cancela una y se crea otra.

    Y se reaprovecha la tarjeta guardada: el cliente no vuelve a escribirla
    para algo que el vive como un simple cambio de plan.
    """
    from shared.db import connection
    from shared.suscripciones import activar_por_culqi

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="basico", periodo="mensual",
        monto="49.00", customer_id="cus_test_c", card_id="crd_test_c",
        subscription_id="sxn_test_vieja")

    async with connection() as c:
        await c.execute("UPDATE planes SET culqi_plan_id_mensual=$1 WHERE codigo='pro'",
                        "pln_test_promensual")

    falso = _CulqiFalso()
    _instalar(monkeypatch, falso)
    await _entrar(cliente, usuario)
    r = await cliente.post("/suscripcion/elegir",
                           data={"plan": "pro", "periodo": "mensual"})

    assert r.status_code == 303
    assert falso.canceladas == ["sxn_test_vieja"]
    assert len(falso.creadas) == 1
    assert falso.creadas[0]["card_id"] == "crd_test_c"
    assert falso.creadas[0]["plan_id"] == "pln_test_promensual"

    async with connection() as c:
        fila = await c.fetchrow(
            """SELECT plan_codigo, estado, culqi_subscription_id, culqi_card_id
                 FROM suscripciones WHERE usuario_id=$1""", usuario["id"])
        await c.execute("UPDATE planes SET culqi_plan_id_mensual=NULL WHERE codigo='pro'")
    assert fila["plan_codigo"] == "pro"
    assert fila["estado"] == "activa"
    assert fila["culqi_subscription_id"] == "sxn_test_nueva0"
    assert fila["culqi_card_id"] == "crd_test_c"


@sin_base
async def test_si_la_nueva_falla_se_dice_que_el_cobro_quedo_desactivado(
        cliente, usuario, monkeypatch):
    """No se puede callar: la vieja YA esta cancelada.

    Quedarse sin cobro automatico y saberlo es recuperable. Quedarse sin el y
    creer que sigue activo es una cuenta que vence sin que nadie lo espere.
    """
    from shared.db import connection
    from shared.suscripciones import activar_por_culqi

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="basico", periodo="mensual",
        monto="49.00", customer_id="cus_test_c", card_id="crd_test_c",
        subscription_id="sxn_test_vieja2")
    async with connection() as c:
        await c.execute("UPDATE planes SET culqi_plan_id_mensual=$1 WHERE codigo='pro'",
                        "pln_test_promensual")

    falso = _CulqiFalso(falla_crear=True)
    _instalar(monkeypatch, falso)
    await _entrar(cliente, usuario)
    r = await cliente.post("/suscripcion/elegir",
                           data={"plan": "pro", "periodo": "mensual"})

    assert r.status_code == 303
    destino = r.headers["location"]
    assert "error=" in destino
    # El texto que ve el cliente lleva el user_message de Culqi, no el del log.
    assert "rechazada" in destino or "rechaz" in destino

    async with connection() as c:
        fila = await c.fetchrow(
            "SELECT culqi_subscription_id FROM suscripciones WHERE usuario_id=$1",
            usuario["id"])
        await c.execute("UPDATE planes SET culqi_plan_id_mensual=NULL WHERE codigo='pro'")
    assert fila["culqi_subscription_id"] is None, (
        "quedo un sxn_ apuntando a una suscripcion ya cancelada en Culqi")


@sin_base
async def test_cambiar_a_un_plan_sin_sincronizar_no_cancela_nada(cliente, usuario,
                                                                 monkeypatch):
    """Sin `pln_` no hay adonde ir: cancelar antes de comprobarlo dejaria al
    cliente sin cobro automatico por un plan que ni siquiera existe alli."""
    from shared.db import connection
    from shared.suscripciones import activar_por_culqi

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="basico", periodo="mensual",
        monto="49.00", customer_id="cus_test_c", card_id="crd_test_c",
        subscription_id="sxn_test_intacta")

    falso = _CulqiFalso()
    _instalar(monkeypatch, falso)
    await _entrar(cliente, usuario)
    r = await cliente.post("/suscripcion/elegir",
                           data={"plan": "pro", "periodo": "mensual"})

    assert r.status_code == 303
    assert falso.canceladas == [] and falso.creadas == []
    async with connection() as c:
        sxn = await c.fetchval(
            "SELECT culqi_subscription_id FROM suscripciones WHERE usuario_id=$1",
            usuario["id"])
    assert sxn == "sxn_test_intacta"


@sin_base
async def test_con_cobro_automatico_no_se_ensena_el_boton_de_pagar_a_mano(
        cliente, usuario, monkeypatch):
    """Ese boton va al pago unico de Izipay: pulsarlo con una suscripcion viva
    en Culqi seria pagar dos veces el mismo periodo."""
    from shared.suscripciones import activar_por_culqi

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_c", card_id="crd_test_c",
        subscription_id="sxn_test_viva")

    await _entrar(cliente, usuario)
    r = await cliente.get("/suscripcion")
    assert r.status_code == 200
    assert 'action="/suscripcion/pagar"' not in r.text
    assert "se renueva automáticamente" in r.text
    # Y la cancelacion avisa de que no tiene vuelta atras.
    assert "irreversible" in r.text


@sin_base
async def test_sin_culqi_el_boton_de_pagar_sigue_donde_estaba(cliente, usuario):
    """Los clientes de Izipay y los pagos manuales no cambian de pantalla."""
    await _entrar(cliente, usuario)
    r = await cliente.get("/suscripcion")
    assert r.status_code == 200
    assert 'action="/suscripcion/pagar"' in r.text
