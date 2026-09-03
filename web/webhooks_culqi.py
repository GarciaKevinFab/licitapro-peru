"""Webhook de Culqi: enterarse de los cobros que Culqi hace sola cada periodo.

EL CUERPO DEL AVISO NO SE CREE. SE COMPRUEBA.

  De la integracion de Culqi hay dos cosas que no se han podido verificar: la
  forma exacta del cuerpo del webhook y si trae firma para validar el origen.
  Con Izipay eso se resolvia con HMAC y fallando cerrado; aqui no se puede,
  porque no se sabe si hay firma que validar.

  Asi que el receptor esta disenado para NO NECESITARLO: del aviso solo se saca
  un identificador (`chr_…` o `evt_…`) y se pregunta a la API de Culqi, con la
  llave SECRETA, si ese cargo existe y como fue. Un aviso falsificado no
  sobrevive a esa consulta: quien lo mande puede inventarse el cuerpo, pero no
  puede hacer que Culqi confirme un cargo que no existe.

  Eso funciona haya firma o no, y sigue funcionando el dia que Culqi cambie el
  formato del cuerpo. Cuando se confirme que hay firma, verificarla ADEMAS es
  una linea mas y una defensa mas; no sustituye a esta.

  Lo unico que un aviso falso consigue es que hagamos una consulta de mas a
  Culqi. Es el coste correcto a pagar por no depender de un formato que no
  conocemos.

QUE SE RESPONDE Y POR QUE

  200  el aviso se proceso, aunque el cobro fuera fallido o repetido. Culqi
       reintenta lo que no recibe 200 y acaba desactivando el webhook si
       insistimos en fallar; un cobro denegado no es un fallo NUESTRO.
  4xx  el aviso es invalido: no es JSON, o no trae ningun identificador que
       podamos comprobar. Ahi si conviene que Culqi lo sepa.

IDEMPOTENTE POR EL CARGO, NO POR EL AVISO

  El mismo cobro puede llegar tres veces. La llave es `culqi_charge_id`, con
  indice unico parcial en la base: es la base la que impide contar dos veces un
  periodo, no una comprobacion previa que dos avisos simultaneos se saltan.
"""
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from shared import culqi
from shared.suscripciones import (
    aplicar_cargo_culqi, registrar_intento_fallido, suscripcion_por_culqi,
)

log = logging.getLogger("web.webhooks_culqi")
router = APIRouter()

# Los identificadores de Culqi tienen forma fija: prefijo, entorno y sufijo
# alfanumerico. Se buscan por patron y no en un campo concreto del cuerpo
# porque el campo lo desconocemos (POR_CONFIRMAR) y el patron no.
_PATRON = {
    "cargo": re.compile(r"^chr_(?:test|live)_[A-Za-z0-9]{6,}$"),
    "evento": re.compile(r"^evt_(?:test|live)_[A-Za-z0-9]{6,}$"),
    "suscripcion": re.compile(r"^sxn_(?:test|live)_[A-Za-z0-9]{6,}$"),
    "cliente": re.compile(r"^cus_(?:test|live)_[A-Za-z0-9]{6,}$"),
}

# Tope de nodos que se recorren buscando identificadores. Un cuerpo enorme
# mandado a proposito no puede tenernos recorriendo un arbol indefinidamente.
_MAX_NODOS = 5000


def identificadores(cuerpo) -> dict:
    """Recorre el aviso y devuelve los ids de Culqi que encuentre.

    POR QUE SE BUSCA POR TODO EL ARBOL Y NO EN UN CAMPO

      No sabemos como se llama el campo. Culqi documenta que el webhook
      notifica eventos de cargos, suscripciones y tarjetas, pero no publica el
      esquema del cuerpo, y adivinarlo significa que el dia que no acierte
      -- o que ellos lo cambien -- los cobros dejarian de aplicarse EN SILENCIO:
      las cuentas irian venciendo una a una con el dinero ya cobrado.

      Buscar por la forma del identificador es inmune a eso. Y no es laxo: lo
      encontrado no se cree, se comprueba contra la API.
    """
    hallados: dict[str, str] = {}
    pila = [cuerpo]
    vistos = 0
    while pila and vistos < _MAX_NODOS:
        nodo = pila.pop()
        vistos += 1
        if isinstance(nodo, dict):
            pila.extend(nodo.values())
        elif isinstance(nodo, (list, tuple)):
            pila.extend(nodo)
        elif isinstance(nodo, str) and len(nodo) < 120:
            for clase, patron in _PATRON.items():
                if clase not in hallados and patron.match(nodo):
                    hallados[clase] = nodo
    return hallados


def _correo_de(objeto) -> str | None:
    """El correo que traiga un objeto de Culqi, para el ultimo intento de casar."""
    pila = [objeto]
    vistos = 0
    while pila and vistos < _MAX_NODOS:
        nodo = pila.pop()
        vistos += 1
        if isinstance(nodo, dict):
            valor = nodo.get("email")
            if isinstance(valor, str) and "@" in valor:
                return valor
            pila.extend(nodo.values())
        elif isinstance(nodo, (list, tuple)):
            pila.extend(nodo)
    return None


@router.get("/webhooks/culqi")
async def comprobacion_webhook():
    """Contesta al validador del panel de Culqi, que comprueba la URL.

    Devuelve una constante: no lee la peticion, no toca la base y no cambia
    ningun estado. El cobro sigue entrando SOLO por POST y solo despues de
    comprobar el cargo contra la API de Culqi con la llave secreta.
    """
    return JSONResponse({"ok": True, "servicio": "webhook culqi"})


@router.post("/webhooks/culqi")
async def recibir(request: Request):
    """Aviso de Culqi. Sin sesion: la autenticidad la da la API, no la cookie."""
    try:
        cuerpo = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "motivo": "cuerpo no es json"},
                            status_code=400)

    ids = identificadores(cuerpo)
    if not ids:
        log.warning("Aviso de Culqi sin ningun identificador reconocible desde %s",
                    request.client.host if request.client else "?")
        return JSONResponse({"ok": False, "motivo": "sin identificadores"},
                            status_code=400)

    if not culqi.activo():
        # Sin llaves no se puede comprobar nada contra Culqi, y aplicar un cobro
        # solo porque el cuerpo lo dice es justo lo que este modulo evita.
        log.warning("Aviso de Culqi con la pasarela en modo simulado: se ignora %s",
                    ids)
        return JSONResponse({"ok": True, "aplicado": False,
                             "motivo": "pasarela en modo simulado"})

    # ─── La comprobacion: aqui se decide si esto es real ──
    cargo_id = ids.get("cargo")
    evento = None
    if not cargo_id and ids.get("evento"):
        # Sin chr_ a la vista se pide el evento, que trae dentro el objeto al
        # que se refiere, y se vuelve a buscar el cargo ahi.
        try:
            evento = await culqi.leer_evento(ids["evento"])
        except culqi.ErrorCulqi as e:
            log.warning("Culqi no reconoce el evento %s: %s",
                        ids["evento"], e.merchant_message)
            return JSONResponse({"ok": True, "aplicado": False,
                                 "motivo": "evento desconocido"})
        cargo_id = identificadores(evento).get("cargo")
        ids.update({k: v for k, v in identificadores(evento).items()
                    if k not in ids})

    if not cargo_id:
        # Hay avisos que no son de cobro: una tarjeta actualizada, una
        # suscripcion creada. No son un error y no cambian el vencimiento.
        log.info("Aviso de Culqi sin cargo asociado (%s): nada que aplicar", ids)
        return JSONResponse({"ok": True, "aplicado": False,
                             "motivo": "aviso sin cargo"})

    try:
        cargo = await culqi.leer_cargo(cargo_id)
    except culqi.ErrorCulqi as e:
        # Culqi no reconoce ese cargo. O el aviso es falso, o Culqi tiene un
        # problema. En los dos casos NO se aplica nada; si fuera lo segundo, el
        # reintento de Culqi lo resolvera.
        log.warning("Culqi no reconoce el cargo %s: %s. No se aplica nada.",
                    cargo_id, e.merchant_message)
        return JSONResponse({"ok": True, "aplicado": False,
                             "motivo": "cargo no confirmado"})

    # ─── ¿De quien es? ───────────────────────────────────
    del_cargo = identificadores(cargo)
    susc = await suscripcion_por_culqi(
        subscription_id=del_cargo.get("suscripcion") or ids.get("suscripcion"),
        customer_id=del_cargo.get("cliente") or ids.get("cliente"),
        email=_correo_de(cargo))

    if not susc:
        # Puede ser de otro producto del mismo comercio. Se contesta 200 -- el
        # aviso SI se proceso -- pero se registra en ERROR: si fuera nuestro,
        # este log es lo unico que separa "no era nuestro" de "un cliente pago
        # y no se le activo".
        log.error("Cargo %s confirmado por Culqi y sin suscripcion nuestra que "
                  "lo reciba (%s). Si es de LicitaPro, hay un pago sin aplicar.",
                  cargo_id, del_cargo)
        return JSONResponse({"ok": True, "aplicado": False,
                             "motivo": "sin suscripcion asociada"})

    # ─── Cobrado o rechazado ─────────────────────────────
    if not culqi.cargo_exitoso(cargo):
        # Culqi reintenta sus propios cobros; nosotros solo contamos los
        # rechazos y aplicamos la maquina de estados que ya existe: gracia
        # primero, suspension despues de MAX_INTENTOS.
        await registrar_intento_fallido(susc["usuario_id"])
        log.info("Cargo %s de la suscripcion %s no fue aprobado (%s): "
                 "sumado como intento fallido", cargo_id, susc["id"],
                 (cargo.get("outcome") or {}).get("type"))
        return JSONResponse({"ok": True, "aplicado": False, "cobrado": False})

    monto = culqi.a_soles(cargo.get("amount") or 0)
    resultado = await aplicar_cargo_culqi(
        suscripcion_id=susc["id"], monto=monto, charge_id=cargo_id,
        event_id=ids.get("evento"), respuesta=cargo, periodo=susc["periodo"])

    log.info("Cargo %s de %s: %s", cargo_id, susc["email"], resultado)
    return JSONResponse({"ok": True, "aplicado": resultado == "aplicado",
                         "resultado": resultado})
