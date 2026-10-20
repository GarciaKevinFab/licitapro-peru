"""Correcciones de la auditoria del 20/10/2026 en suscripciones y cobros.

LO QUE SE PROTEGE AQUI

  Cada bloque fija uno de los hallazgos ALTO 12 y 13 (y los MEDIO de checkout)
  para que no vuelva callado. Ninguno de estos fallos daba error: regalaban
  horas o meses de servicio, cobraban dos veces o suspendian a quien pagaba.

    1. `vence` escrito en UTC y leido en hora de Lima: cinco horas de mas.
    2. Cancelar cortaba al instante aunque prometia "hasta fin de periodo".
    3. Subir de plan sin pagar, por /suscripcion/elegir y por /comprar.
    4. Doble clic en Culqi = dos suscripciones; fallo tras crearla = cobro vivo.
    5. El mismo cargo rechazado reenviado N veces sumaba N intentos.
    6. `suscripcion_por_culqi` con `OR ... LIMIT 1` sin orden.
    7. El retorno simulado confirmaba pagos fuera de pruebas y de otros.
    8. El importe de Izipay pasaba por float.
    9. El checkout confirmaba si un correo tenia cuenta y no frenaba el
       card-testing por correo.
"""
import asyncio
import re
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote_plus

import pytest

from shared import fechas
from tests.conftest import sin_base
from tests.test_suscripcion_culqi import (
    _con_plan_sincronizado,
    _CulqiCheckout,
    _entrar,
    _sin_plan_sincronizado,
)
from web import comprar
from web import suscripcion as vista

RAIZ = Path(__file__).resolve().parent.parent


@pytest.fixture
def izipay_simulado(monkeypatch):
    """Izipay en simulado aunque el .env de la maquina traiga credenciales.

    Sin esto, una maquina con IZIPAY_MODO=sandbox mandaria peticiones de verdad
    al sandbox de la pasarela durante las pruebas.
    """
    monkeypatch.setattr(vista.izipay, "modo", lambda: "simulado")
    monkeypatch.setenv("LICITAPRO_ENTORNO", "dev")


async def _otro_usuario():
    from shared.db import crear_usuario
    from shared.seguridad import hashear_password
    email = f"prueba-otro-{uuid.uuid4().hex[:10]}@ejemplo.pe"
    fila = await crear_usuario(email, hashear_password("ClaveDePrueba123!"), "Otra")
    return {"id": fila["id"], "email": email, "password": "ClaveDePrueba123!"}


async def _borrar(uid):
    from shared.db import borrar_cuenta
    try:
        await borrar_cuenta(uid)
    except Exception as e:  # noqa: BLE001
        print(f"aviso: no se pudo borrar la cuenta de prueba {uid}: {e}")


async def _fila(usuario_id):
    from shared.db import connection
    async with connection() as c:
        return await c.fetchrow(
            """SELECT plan_codigo, periodo, estado, vence, intentos_fallidos,
                      culqi_subscription_id
                 FROM suscripciones WHERE usuario_id=$1""", usuario_id)


async def _poner(usuario_id, plan="basico", estado="activa", dias=20):
    from shared.suscripciones import activar_manual
    assert await activar_manual(usuario_id, plan, estado, "mensual",
                                fechas.ahora() + timedelta(days=dias),
                                "prueba", por="pruebas")


# ─── 1. Una sola hora: la de Lima ────────────────────────

def test_ninguna_consulta_usa_now_a_secas():
    """Cualquier `NOW()` suelto vuelve a escribir en el reloj de la sesion (UTC).

    La unica excepcion admitida es la ventana de `created_at`, que lo rellena
    el DEFAULT con ese mismo reloj.
    """
    fuente = (RAIZ / "shared" / "suscripciones.py").read_text(encoding="utf-8")
    codigo = [linea for linea in fuente.splitlines()
              if not linea.strip().startswith("#")]
    sueltos = [linea for linea in codigo
               if re.search(r"NOW\(\)(?! AT TIME ZONE)", linea)
               and "created_at" not in linea]
    assert sueltos == [], sueltos


@sin_base
async def test_la_expresion_da_hora_de_lima_aunque_la_sesion_este_en_utc():
    """Produccion corre la sesion en UTC: es justo el caso que hay que cubrir."""
    from shared.db import connection
    from shared.suscripciones import AHORA_SQL
    async with connection() as c, c.transaction():
        await c.execute("SET LOCAL TIME ZONE 'UTC'")
        en_base = await c.fetchval(f"SELECT {AHORA_SQL}")
    assert abs((en_base - fechas.ahora()).total_seconds()) < 120


@sin_base
async def test_la_prueba_vence_en_hora_de_lima(usuario):
    """La fila nace con `crear_suscripcion_prueba`: 14 dias desde AHORA en Lima."""
    fila = await _fila(usuario["id"])
    esperado = fechas.ahora() + timedelta(days=14)
    assert abs((fila["vence"] - esperado).total_seconds()) < 120


@sin_base
async def test_confirmar_un_pago_extiende_en_hora_de_lima(usuario, izipay_simulado):
    from shared.db import connection
    from shared.suscripciones import confirmar_pago, registrar_intento
    async with connection() as c:
        await c.execute("UPDATE suscripciones SET vence=NULL WHERE usuario_id=$1",
                        usuario["id"])
    await registrar_intento(usuario["id"], Decimal("99.00"), "LPHORA1" + usuario["email"][7:12])
    assert await confirmar_pago("LPHORA1" + usuario["email"][7:12], "T1")
    fila = await _fila(usuario["id"])
    esperado = fechas.ahora() + timedelta(days=30)
    assert abs((fila["vence"] - esperado).total_seconds()) < 120


# ─── 2. Cancelar no corta el periodo pagado ─────────────

def test_cancelada_con_periodo_por_delante_conserva_el_acceso():
    from shared.suscripciones import tiene_acceso
    ahora = fechas.fija(2026, 10, 20, 12)
    assert tiene_acceso("cancelada", ahora + timedelta(days=3), ahora) is True
    assert tiene_acceso("cancelada", ahora - timedelta(minutes=1), ahora) is False
    assert tiene_acceso("cancelada", None, ahora) is False
    assert tiene_acceso("suspendida", ahora + timedelta(days=3), ahora) is False


@sin_base
async def test_cancelar_deja_entrar_hasta_vence_y_no_despues(usuario):
    from shared.db import connection
    from shared.suscripciones import cancelar, estado_suscripcion

    await _poner(usuario["id"], plan="pro", dias=20)
    await cancelar(usuario["id"])
    s = await estado_suscripcion(usuario["id"])
    assert s["estado_efectivo"] == "cancelada"
    assert s["acceso"] is True, "cancelar corto el periodo ya pagado"

    async with connection() as c:
        await c.execute(
            "UPDATE suscripciones SET vence=$2 WHERE usuario_id=$1",
            usuario["id"], fechas.ahora() - timedelta(hours=1))
    assert (await estado_suscripcion(usuario["id"]))["acceso"] is False


@sin_base
async def test_la_pantalla_dice_hasta_cuando_se_conserva(cliente, usuario):
    from shared.suscripciones import cancelar
    await _poner(usuario["id"], plan="pro", dias=20)
    await cancelar(usuario["id"])
    await _entrar(cliente, usuario)
    r = await cliente.get("/suscripcion")
    assert r.status_code == 200
    assert "Conservas el acceso hasta el" in r.text


# ─── 3. Subir de plan exige cobro ───────────────────────

@sin_base
async def test_cambiar_plan_no_aplica_una_subida(usuario):
    from shared.suscripciones import (
        PLAN_APLICADO,
        PLAN_INVALIDO,
        PLAN_PENDIENTE_DE_PAGO,
        cambiar_plan,
    )
    await _poner(usuario["id"], plan="basico")
    assert await cambiar_plan(usuario["id"], "empresa", "mensual") == PLAN_PENDIENTE_DE_PAGO
    assert (await _fila(usuario["id"]))["plan_codigo"] == "basico"
    assert await cambiar_plan(usuario["id"], "inventado", "mensual") == PLAN_INVALIDO
    assert await cambiar_plan(usuario["id"], "basico", "semanal") == PLAN_INVALIDO
    # Mismo plan con otro periodo: no da nada por adelantado, se aplica.
    assert await cambiar_plan(usuario["id"], "basico", "anual") == PLAN_APLICADO


@sin_base
async def test_elegir_un_plan_mas_caro_abre_el_cobro_y_solo_el_pago_lo_aplica(
        cliente, usuario, izipay_simulado):
    from shared.db import connection
    await _poner(usuario["id"], plan="basico")
    await _entrar(cliente, usuario)

    r = await cliente.post("/suscripcion/elegir",
                           data={"plan": "empresa", "periodo": "mensual"})
    assert r.status_code == 200, r.headers.get("location")
    assert "Simular pago aprobado" in r.text
    assert (await _fila(usuario["id"]))["plan_codigo"] == "basico", (
        "subio de plan sin pagar")

    async with connection() as c:
        orden = await c.fetchrow(
            """SELECT ps.izipay_order_number, ps.monto FROM pagos_suscripcion ps
                 JOIN suscripciones s ON s.id = ps.suscripcion_id
                WHERE s.usuario_id=$1 AND ps.estado='pendiente'""", usuario["id"])
    assert str(orden["monto"]) == "199.00"

    r = await cliente.post("/suscripcion/retorno", data={
        "numero_orden": orden["izipay_order_number"], "simulado": "1"})
    assert r.status_code == 303
    fila = await _fila(usuario["id"])
    assert (fila["plan_codigo"], fila["estado"]) == ("empresa", "activa")


@sin_base
async def test_bajar_de_plan_se_aplica_al_instante(cliente, usuario):
    await _poner(usuario["id"], plan="empresa")
    await _entrar(cliente, usuario)
    r = await cliente.post("/suscripcion/elegir",
                           data={"plan": "basico", "periodo": "mensual"})
    assert r.status_code == 303
    assert "aviso=" in r.headers["location"]
    assert (await _fila(usuario["id"]))["plan_codigo"] == "basico"


@sin_base
async def test_el_checkout_con_sesion_tampoco_sube_sin_pagar(
        cliente, usuario, izipay_simulado):
    await _poner(usuario["id"], plan="basico")
    await _entrar(cliente, usuario)
    r = await cliente.post("/comprar", data={"plan": "empresa", "periodo": "anual"})
    assert r.status_code == 200
    assert "1990.00" in r.text
    assert (await _fila(usuario["id"]))["plan_codigo"] == "basico"


@sin_base
async def test_si_culqi_falla_al_cambiar_el_plan_se_queda_el_viejo(
        cliente, usuario, monkeypatch):
    """La suscripcion vieja ya se cancelo, pero el plan nuevo no se pago."""
    from tests.test_suscripcion_culqi import _CulqiFalso, _instalar
    from shared.suscripciones import activar_por_culqi

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="basico", periodo="mensual",
        monto="49.00", customer_id="cus_test_c", card_id="crd_test_c",
        subscription_id="sxn_test_viejaaud")
    await _con_plan_sincronizado("empresa", "mensual", "pln_test_empresa")
    _instalar(monkeypatch, _CulqiFalso(falla_crear=True))
    await _entrar(cliente, usuario)
    try:
        r = await cliente.post("/suscripcion/elegir",
                               data={"plan": "empresa", "periodo": "mensual"})
    finally:
        await _sin_plan_sincronizado("empresa")
    assert "error=" in r.headers["location"]
    assert (await _fila(usuario["id"]))["plan_codigo"] == "basico"


# ─── 4. Culqi: una suscripcion por cuenta y compensacion ─

class _CulqiLento(_CulqiCheckout):
    """Tarda en crear la suscripcion: deja hueco para el segundo clic."""

    async def crear_suscripcion(self, card_id, plan_id, metadata=None):
        await asyncio.sleep(0.3)
        return await super().crear_suscripcion(card_id, plan_id, metadata)


@sin_base
async def test_el_doble_clic_no_crea_dos_suscripciones(cliente, usuario, monkeypatch):
    await _con_plan_sincronizado()
    falso = _CulqiLento()
    monkeypatch.setattr(comprar, "culqi", falso)
    await _entrar(cliente, usuario)
    datos = {"plan": "pro", "periodo": "mensual", "token_id": "tkn_test_doble"}
    try:
        r1, r2 = await asyncio.gather(cliente.post("/comprar/culqi", data=datos),
                                      cliente.post("/comprar/culqi", data=datos))
    finally:
        await _sin_plan_sincronizado()
    assert len(falso.creadas) == 1, "dos cobros recurrentes por el mismo plan"
    assert r1.status_code == r2.status_code == 303
    assert (await _fila(usuario["id"]))["culqi_subscription_id"] == "sxn_test_nueva0"


@sin_base
async def test_con_cobro_automatico_vivo_no_se_crea_otro(cliente, usuario, monkeypatch):
    from shared.suscripciones import activar_por_culqi
    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_c", card_id="crd_test_c",
        subscription_id="sxn_test_yaviva")
    await _con_plan_sincronizado()
    falso = _CulqiCheckout()
    monkeypatch.setattr(comprar, "culqi", falso)
    await _entrar(cliente, usuario)
    try:
        r = await cliente.post("/comprar/culqi", data={
            "plan": "pro", "periodo": "mensual", "token_id": "tkn_test_otra"})
    finally:
        await _sin_plan_sincronizado()
    assert r.status_code == 303
    assert falso.creadas == [] and falso.tarjetas == []
    assert (await _fila(usuario["id"]))["culqi_subscription_id"] == "sxn_test_yaviva"


@sin_base
async def test_si_falla_algo_tras_crearla_se_cancela_en_culqi(cliente, usuario,
                                                              monkeypatch):
    """Una excepcion al asentar dejaba el cobro recurrente vivo y sin cuenta."""
    await _con_plan_sincronizado()
    falso = _CulqiCheckout()
    monkeypatch.setattr(comprar, "culqi", falso)

    async def revienta(**kw):
        raise RuntimeError("la base se cayo a medias")

    monkeypatch.setattr(comprar, "activar_por_culqi", revienta)
    await _entrar(cliente, usuario)
    try:
        r = await cliente.post("/comprar/culqi", data={
            "plan": "pro", "periodo": "mensual", "token_id": "tkn_test_comp"})
    finally:
        await _sin_plan_sincronizado()
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert falso.canceladas == ["sxn_test_nueva0"]
    assert (await _fila(usuario["id"]))["culqi_subscription_id"] is None


# ─── 5. Un cargo rechazado cuenta una vez ───────────────

@sin_base
async def test_el_mismo_cargo_rechazado_reenviado_cuenta_una_vez(usuario):
    from shared.suscripciones import activar_por_culqi, registrar_intento_fallido
    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_r", card_id="crd_test_r",
        subscription_id="sxn_test_r")
    cargo = f"chr_test_rechazo{uuid.uuid4().hex[:8]}"
    contados = [await registrar_intento_fallido(usuario["id"], charge_id=cargo,
                                                monto=Decimal("99.00"))
                for _ in range(4)]
    assert contados == [True, False, False, False]
    fila = await _fila(usuario["id"])
    assert fila["intentos_fallidos"] == 1
    assert fila["estado"] == "activa", "cuatro reenvios de un rechazo suspendieron"

    # Cargos DISTINTOS si cuentan, y al cuarto se suspende como siempre.
    for i in range(3):
        await registrar_intento_fallido(usuario["id"], charge_id=f"{cargo}x{i}",
                                        monto=Decimal("99.00"))
    assert (await _fila(usuario["id"]))["estado"] == "suspendida"


@sin_base
async def test_el_webhook_reenviado_no_suma_de_mas(cliente, usuario, monkeypatch):
    from shared.suscripciones import activar_por_culqi
    from web import webhooks_culqi as wh
    sxn = f"sxn_test_wh{uuid.uuid4().hex[:8]}"
    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_wh", card_id="crd_test_wh",
        subscription_id=sxn)
    cargo = {"object": "charge", "id": f"chr_test_den{uuid.uuid4().hex[:8]}",
             "amount": 9900, "subscription_id": sxn,
             "outcome": {"type": "venta_denegada"}}

    async def leer_cargo(charge_id):
        return cargo

    monkeypatch.setattr(wh.culqi, "activo", lambda: True)
    monkeypatch.setattr(wh.culqi, "leer_cargo", leer_cargo)
    for _ in range(4):
        r = await cliente.post("/webhooks/culqi", json={"data": {"id": cargo["id"]}})
        assert r.status_code == 200
    fila = await _fila(usuario["id"])
    assert fila["intentos_fallidos"] == 1
    assert fila["estado"] == "activa"


@sin_base
async def test_un_cargo_contado_como_rechazo_que_luego_cobra_se_aplica(usuario):
    from shared.suscripciones import (
        activar_por_culqi,
        aplicar_cargo_culqi,
        registrar_intento_fallido,
    )
    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
        monto="99.00", customer_id="cus_test_v", card_id="crd_test_v",
        subscription_id="sxn_test_v", charge_id=f"chr_test_alta{uuid.uuid4().hex[:8]}")
    susc_id = (await _datos(usuario["id"]))["id"]
    cargo = f"chr_test_vuelta{uuid.uuid4().hex[:8]}"
    await registrar_intento_fallido(usuario["id"], charge_id=cargo, monto=Decimal("99.00"))
    assert await aplicar_cargo_culqi(susc_id, Decimal("99.00"), cargo,
                                     periodo="mensual") == "aplicado"
    assert await aplicar_cargo_culqi(susc_id, Decimal("99.00"), cargo,
                                     periodo="mensual") == "repetido"


async def _datos(usuario_id):
    from shared.suscripciones import datos_culqi
    return await datos_culqi(usuario_id)


# ─── 6. De Culqi a nuestra fila, de forma determinista ──

@sin_base
async def test_el_sxn_gana_al_correo_de_otra_cuenta(usuario):
    from shared.suscripciones import activar_por_culqi, suscripcion_por_culqi
    otro = await _otro_usuario()
    try:
        await activar_por_culqi(
            usuario_id=usuario["id"], plan_codigo="pro", periodo="mensual",
            monto="99.00", customer_id=f"cus_test_a{uuid.uuid4().hex[:6]}",
            card_id="crd_test_a", subscription_id=f"sxn_test_a{uuid.uuid4().hex[:6]}")
        mia = await _datos(usuario["id"])
        await activar_por_culqi(
            usuario_id=otro["id"], plan_codigo="pro", periodo="mensual",
            monto="99.00", customer_id=f"cus_test_b{uuid.uuid4().hex[:6]}",
            card_id="crd_test_b", subscription_id=f"sxn_test_b{uuid.uuid4().hex[:6]}")
        suya = await _datos(otro["id"])

        # El correo apunta a la OTRA cuenta (la de id mas bajo, si fuera por
        # orden de llegada); el sxn_ manda.
        for _ in range(5):
            r = await suscripcion_por_culqi(
                subscription_id=suya["culqi_subscription_id"], email=usuario["email"])
            assert r["usuario_id"] == otro["id"]
            r = await suscripcion_por_culqi(
                customer_id=mia["culqi_customer_id"], email=otro["email"])
            assert r["usuario_id"] == usuario["id"]
    finally:
        await _borrar(otro["id"])


# ─── 7. El retorno simulado ─────────────────────────────

async def _orden_pendiente(cliente):
    r = await cliente.post("/suscripcion/pagar")
    assert r.status_code == 200, r.headers.get("location")
    return re.search(r'name="numero_orden" value="([^"]+)"', r.text).group(1)


@sin_base
async def test_fuera_de_dev_el_retorno_simulado_no_confirma(
        cliente, usuario, izipay_simulado, monkeypatch):
    from shared.db import connection
    await _entrar(cliente, usuario)
    orden = await _orden_pendiente(cliente)
    monkeypatch.setenv("LICITAPRO_ENTORNO", "produccion")
    r = await cliente.post("/suscripcion/retorno",
                           data={"numero_orden": orden, "simulado": "1"})
    assert "error=" in r.headers["location"]
    async with connection() as c:
        estado = await c.fetchval(
            "SELECT estado FROM pagos_suscripcion WHERE izipay_order_number=$1", orden)
    assert estado == "pendiente"


@sin_base
async def test_sin_entorno_declarado_tampoco(monkeypatch):
    """Una variable que falta no puede abrir la simulacion: sin valor por defecto."""
    monkeypatch.setattr(vista.izipay, "modo", lambda: "simulado")
    monkeypatch.delenv("LICITAPRO_ENTORNO", raising=False)
    assert vista.simulacion_permitida() is False
    monkeypatch.setenv("LICITAPRO_ENTORNO", "test")
    assert vista.simulacion_permitida() is True
    monkeypatch.setattr(vista.izipay, "modo", lambda: "sandbox")
    assert vista.simulacion_permitida() is False


@sin_base
async def test_fuera_de_dev_no_se_abre_el_formulario_simulado(
        cliente, usuario, izipay_simulado, monkeypatch):
    await _entrar(cliente, usuario)
    monkeypatch.setenv("LICITAPRO_ENTORNO", "produccion")
    r = await cliente.post("/suscripcion/pagar")
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


@sin_base
async def test_no_se_confirma_la_orden_de_otro(cliente, usuario, izipay_simulado):
    import httpx

    from shared.db import connection
    from web.app import app
    otro = await _otro_usuario()
    try:
        await _entrar(cliente, usuario)
        orden_mia = await _orden_pendiente(cliente)

        transporte = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transporte, base_url="http://pruebas",
                                     follow_redirects=False) as ajeno:
            await _entrar(ajeno, otro)
            r = await ajeno.post("/suscripcion/retorno",
                                 data={"numero_orden": orden_mia, "simulado": "1"})
        assert "error=" in r.headers["location"]
        async with connection() as c:
            estado = await c.fetchval(
                "SELECT estado FROM pagos_suscripcion WHERE izipay_order_number=$1",
                orden_mia)
        assert estado == "pendiente", "otro usuario confirmo mi orden"

        r = await cliente.post("/suscripcion/retorno",
                               data={"numero_orden": orden_mia, "simulado": "1"})
        assert "aviso=" in r.headers["location"]
    finally:
        await _borrar(otro["id"])


# ─── 8. El importe de Izipay en Decimal ─────────────────

@pytest.mark.parametrize("entrada,esperado", [
    (Decimal("99"), "99.00"), (Decimal("0.285"), "0.29"), (0.285, "0.29"),
    ("1990.005", "1990.01"), (Decimal("49.1"), "49.10"),
])
def test_el_importe_se_formatea_sin_float(entrada, esperado):
    from shared.izipay import importe
    # Con float, f"{0.285:.2f}" da "0.28": el centimo que se perdia.
    assert importe(entrada) == esperado


@pytest.mark.parametrize("malo", ["nan", "inf", "-1", "0", "abc", None])
def test_un_importe_imposible_no_se_manda(malo):
    from shared.izipay import importe
    with pytest.raises(ValueError):
        importe(malo)


async def test_el_token_de_izipay_lleva_el_importe_exacto(monkeypatch):
    from shared import izipay
    enviado = {}

    class _Respuesta:
        headers = {"content-type": "application/json"}

        def json(self):
            return {"code": "00", "response": {"token": "tok"}}

    class _Cliente:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            enviado.update(json)
            return _Respuesta()

    monkeypatch.setattr(izipay, "modo", lambda: "sandbox")
    monkeypatch.setattr(izipay.httpx, "AsyncClient", _Cliente)
    r = await izipay.generar_token_sesion("LP1", Decimal("0.285"), "a@b.pe")
    assert r["ok"] and enviado["amount"] == "0.29"
    assert izipay.comision_estimada(Decimal("99.00")) == izipay.comision_estimada(99.0)


# ─── 9. El checkout publico ─────────────────────────────

def test_la_clave_del_freno_ignora_mayusculas_y_etiquetas():
    assert comprar._clave_correo(" Ana+prueba1@Ejemplo.PE ") == "ana@ejemplo.pe"


@sin_base
async def test_el_checkout_no_confirma_que_el_correo_tenga_cuenta(cliente, usuario):
    r = await cliente.post("/comprar", data={
        "plan": "pro", "periodo": "mensual",
        "email": usuario["email"], "password": "OtraClaveDistinta123!"})
    destino = unquote_plus(r.headers["location"])
    assert "Ya tienes una cuenta" not in destino
    assert comprar.AVISO_CUENTA_EXISTENTE in destino


@sin_base
async def test_el_pago_con_tarjeta_se_frena_por_correo(cliente, monkeypatch):
    """Card-testing: muchas tarjetas contra el mismo correo sin sesion."""
    comprar._FRENO_TARJETA.reiniciar()
    await _con_plan_sincronizado()
    falso = _CulqiCheckout(falla_tarjeta=True)
    monkeypatch.setattr(comprar, "culqi", falso)
    correo = f"prueba-freno-{uuid.uuid4().hex[:8]}@ejemplo.pe"
    try:
        respuestas = []
        for i in range(comprar.INTENTOS_TARJETA_POR_CORREO + 1):
            # La etiqueta +N no abre un cubo nuevo.
            local, dominio = correo.split("@")
            r = await cliente.post("/comprar/culqi", data={
                "plan": "pro", "periodo": "mensual", "token_id": f"tkn_test_{i}",
                "email": f"{local}+{i}@{dominio}", "password": "ClaveDePrueba123!"})
            respuestas.append(unquote_plus(r.headers["location"]))
    finally:
        await _sin_plan_sincronizado()
        comprar._FRENO_TARJETA.reiniciar()
    assert len(falso.clientes) == comprar.INTENTOS_TARJETA_POR_CORREO
    assert "Demasiados intentos" in respuestas[-1]
