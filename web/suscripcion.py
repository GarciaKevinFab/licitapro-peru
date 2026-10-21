"""Suscripcion: elegir plan, pagar con Izipay y recibir la confirmacion."""
import logging
import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from shared import culqi, izipay
from shared.db import connection
from shared.suscripciones import (
    PLAN_PENDIENTE_DE_PAGO,
    activar_por_culqi,
    cambiar_plan,
    cancelar,
    confirmar_pago,
    datos_culqi,
    estado_suscripcion,
    limpiar_culqi,
    orden_del_usuario,
    plan_valido,
    plan_y_precio,
    registrar_intento,
)
from web.auth import usuario_actual

log = logging.getLogger("web.suscripcion")
router = APIRouter()


def _plantillas(request: Request):
    return request.app.state.templates


async def _planes():
    async with connection() as conn:
        return await conn.fetch(
            "SELECT * FROM planes WHERE activo=TRUE ORDER BY orden")


# QUIEN EMITE NO SE ESCRIBE AQUI
#
#   La razon social, el RUC y el contacto salen de `identidad()`, que es
#   web.comprar._comercio leyendo del entorno y que la plantilla ya tiene como
#   global (ver web/app.py). Escribirlos en este modulo habria duplicado unos
#   datos que tienen que coincidir letra por letra con el contrato de comercio
#   de la pasarela, y habria roto la regla que _comercio() defiende: lo que
#   falta NO se pinta, porque un dato de relleno es peor que un hueco.
#
#   Este ticket NO es una boleta. Es el resumen del cobro que ya se hizo, para
#   que el cliente tenga a mano el numero de orden y el importe. El comprobante
#   electronico de SUNAT se emite aparte.


def simulacion_permitida() -> bool:
    """Si se puede "pagar" sin pasarela: Izipay simulado Y entorno de pruebas.

    POR QUE NO BASTA CON EL MODO SIMULADO

      `izipay.modo()` cae a "simulado" en cuanto falta una credencial, a
      proposito: sin llaves hay que fallar hacia "no cobra nada". Pero el
      retorno simulado CONFIRMA pagos, y con solo esa condicion bastaba con
      borrar (o escribir mal) una variable del `.env` de produccion para que
      cualquiera con cuenta se regalara meses mandando `simulado=1`.

      Se exige ademas que el entorno lo diga en voz alta. Y sin valor por
      defecto: otras partes del proyecto tratan una LICITAPRO_ENTORNO ausente
      como "dev", y aqui eso seria repetir el mismo agujero con otra variable.
    """
    entorno = os.getenv("LICITAPRO_ENTORNO", "").strip().lower()
    return izipay.modo() == "simulado" and entorno in ("dev", "test")


def _comprobante(historial):
    """El ultimo cobro que de verdad se cobro, ya desglosado. None si no hay.

    `historial` llega ordenado por fecha descendente, asi que el primero que
    este en 'pagado' es el mas reciente. Los pendientes y los fallidos se
    saltan a proposito: un comprobante de algo que no se cobro es justo lo que
    hace que alguien crea que ya pago.

    El desglose sale de web/comprar.py y no se recalcula aqui para que el
    checkout y el comprobante den exactamente las mismas tres cifras. Si cada
    pantalla lo hiciera por su cuenta, un cambio en el redondeo las dejaria
    discrepando en un centimo justo donde el cliente compara con su tarjeta.
    """
    # El import va aqui dentro y no arriba porque comprar.py ya importa este
    # modulo (_con_error, iniciar_cobro): a nivel de fichero seria circular.
    #
    # Se llama _desglose, CON guion bajo. Escribirlo sin el reventaba esta
    # funcion con ImportError, y como se la llama en cada carga de
    # /suscripcion, la pagina entera devolvia 500 a cualquiera que fuera a ver
    # su plan. Local no lo cazo -- las pruebas que llegan aqui necesitan base de
    # datos y se saltaban --; lo cazo el CI.
    from web.comprar import _desglose

    for h in historial or []:
        if h["estado"] != "pagado":
            continue
        partes = _desglose(h["monto"])
        return {
            "orden": h["izipay_order_number"] or h["culqi_charge_id"] or "—",
            "fecha": h["created_at"],
            "base": partes["base"],
            "igv": partes["igv"],
            "total": partes["total"],
        }
    return None


@router.get("/suscripcion", response_class=HTMLResponse)
async def ver(request: Request, aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/suscripcion", status_code=303)

    susc = await estado_suscripcion(usuario["id"])
    async with connection() as conn:
        historial = await conn.fetch(
            """SELECT ps.* FROM pagos_suscripcion ps
                 JOIN suscripciones s ON s.id = ps.suscripcion_id
                WHERE s.usuario_id = $1
                ORDER BY ps.created_at DESC LIMIT 12""",
            usuario["id"])

    # `sxn` es lo que distingue "esta cuenta se cobra sola" de "hay que
    # perseguirla cada periodo", y de eso dependen tres cosas en la pantalla:
    # si se ensena el boton de pagar a mano, que dice la confirmacion de
    # cancelar y que avisa el cambio de plan.
    culqi_datos = await datos_culqi(usuario["id"]) if susc.get("existe") else None
    return _plantillas(request).TemplateResponse(request, "suscripcion.html", {
        "request": request, "usuario": usuario, "s": susc,
        "planes": await _planes(), "historial": historial,
        "modo_pasarela": izipay.modo(),
        # La comision de la pasarela que de verdad cobra. Con Culqi activo,
        # seguir restando la de Izipay daria un neto equivocado en la unica
        # pantalla donde el dueno mira lo que le queda.
        "comision": (culqi.comision_estimada if culqi.cobro_recurrente()
                     else izipay.comision_estimada),
        "culqi_activo": culqi.cobro_recurrente(),
        "culqi_sxn": (culqi_datos or {}).get("culqi_subscription_id"),
        "aviso": aviso, "error": error,
        # El comprobante del ultimo cobro. `None` si todavia no hay ninguno
        # pagado -- una cuenta en prueba, por ejemplo --, y entonces la
        # plantilla no pinta nada. Quien emite lo pone `identidad()`, que ya es
        # global de Jinja y no hace falta pasar aqui.
        "comprobante": _comprobante(historial),
    })


@router.post("/suscripcion/elegir")
async def elegir(request: Request, plan: str = Form(""), periodo: str = Form("mensual")):
    """Cambia de plan. Con Culqi eso es cancelar una suscripcion y crear otra.

    POR QUE NO ES UN UPDATE

      En Culqi el importe y la frecuencia viven DENTRO del plan, y una
      suscripcion apunta a un plan concreto. No existe "cambiale el plan a esta
      suscripcion": hay que cancelar la vieja y crear otra contra el plan nuevo.
      La tarjeta se reaprovecha -- el `crd_` sigue guardado --, asi que el
      cliente no vuelve a escribirla para algo que el vive como un simple
      cambio de plan.

    SE CANCELA ANTES DE CREAR, Y ES LA MENOS MALA DE LAS DOS OPCIONES

      Lo comodo seria crear la nueva y cancelar la vieja solo si sale bien.
      Pero entonces habria un instante con DOS suscripciones vivas sobre la
      misma tarjeta, y un proceso que muera justo ahi deja al cliente con dos
      cobros recurrentes -- que descubre en su extracto, un mes despues.

      Cancelando primero, el hueco malo pasa a ser "un segundo sin cobro
      automatico", que se ve en esta misma pantalla y se arregla volviendo a
      elegir el plan. Si la creacion falla, se dice con todas las letras que el
      cobro automatico quedo desactivado.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    datos = await datos_culqi(usuario["id"])
    sxn = (datos or {}).get("culqi_subscription_id")
    tarjeta = (datos or {}).get("culqi_card_id")

    if not await plan_valido(plan, periodo):
        return RedirectResponse("/suscripcion?error=Plan+o+periodo+no+valido",
                                status_code=303)

    # Sin suscripcion recurrente viva, el cambio pasa por `cambiar_plan`, que
    # NO aplica una subida sin cobro: si el plan es mas caro, se abre el pago
    # de ese plan y es la confirmacion del pago la que lo aplica. Bajar se
    # aplica ya (ver la decision en `cambiar_plan`).
    if not (sxn and tarjeta and culqi.cobro_recurrente()):
        resultado = await cambiar_plan(usuario["id"], plan, periodo)
        if not resultado:
            return RedirectResponse("/suscripcion?error=Plan+o+periodo+no+valido",
                                    status_code=303)
        if resultado == PLAN_PENDIENTE_DE_PAGO:
            return await iniciar_cobro(request, usuario, plan=plan, periodo=periodo)
        return RedirectResponse("/suscripcion?aviso=Plan+actualizado.+Ya+puedes+pagar",
                                status_code=303)

    # Con Culqi el plan NO se toca antes de crear la suscripcion nueva: es
    # `activar_por_culqi` quien lo pone, y solo despues de que Culqi la haya
    # creado (y cobrado). Antes se cambiaba aqui arriba, y si la creacion
    # fallaba el cliente se quedaba con el plan nuevo sin haberlo pagado.

    from web.comprar import plan_culqi_id
    plan_id = await plan_culqi_id(plan, periodo)
    if not plan_id:
        log.error("Cambio de plan a %s/%s sin plan de Culqi sincronizado",
                  plan, periodo)
        return RedirectResponse(_con_error(
            "/suscripcion",
            "Ese plan aún no admite cobro automático. Escríbenos y lo "
            "resolvemos."), status_code=303)

    try:
        await culqi.cancelar_suscripcion(sxn)
    except culqi.ErrorCulqi as e:
        log.error("No se pudo cancelar %s al cambiar de plan: %s", sxn,
                  e.merchant_message)
        return RedirectResponse(_con_error(
            "/suscripcion",
            "No pudimos cambiar el cobro automático. No hemos tocado nada: "
            "vuelve a intentarlo en un momento."), status_code=303)
    await limpiar_culqi(usuario["id"])

    monto = (await plan_y_precio(plan, periodo) or {}).get("monto")
    try:
        nueva = await culqi.crear_suscripcion(
            tarjeta, plan_id,
            metadata={"usuario_id": usuario["id"], "plan_codigo": plan,
                      "periodo": periodo, "producto": "licitapro",
                      "cambio_desde": sxn})
    except culqi.ErrorCulqi as e:
        log.error("Suscripcion vieja %s cancelada y la nueva (%s/%s) fallo para "
                  "%s: %s. La cuenta se queda SIN cobro automatico.",
                  sxn, plan, periodo, usuario["email"], e.merchant_message)
        return RedirectResponse(_con_error(
            "/suscripcion",
            f"No pudimos cambiar tu plan y el cobro automático quedó "
            f"desactivado: {e.user_message} Conservas tu plan actual hasta "
            f"que venza; vuelve a contratarlo desde Precios."),
            status_code=303)

    await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo=plan, periodo=periodo,
        monto=monto, customer_id=(datos or {}).get("culqi_customer_id"),
        card_id=tarjeta, subscription_id=nueva["id"],
        respuesta={"origen": "cambio_de_plan", "anterior": sxn,
                   "suscripcion": nueva})
    log.info("Plan de %s cambiado a %s/%s: %s -> %s", usuario["email"], plan,
             periodo, sxn, nueva["id"])
    return RedirectResponse(
        "/suscripcion?aviso=Plan+cambiado.+Se+renueva+solo+cada+periodo",
        status_code=303)


def _con_error(ruta: str, texto: str) -> str:
    """La ruta de vuelta con el error pegado, respetando la query que ya traiga.

    `/suscripcion` no lleva query y `/comprar/pro?periodo=anual` si. Pegar
    siempre "?" le comeria el periodo a la segunda y devolveria al usuario a un
    checkout distinto del que estaba mirando.
    """
    return f"{ruta}{'&' if '?' in ruta else '?'}error={quote_plus(texto)}"


async def iniciar_cobro(request: Request, usuario, volver_a: str = "/suscripcion",
                        plan: str | None = None, periodo: str | None = None):
    """Crea la orden, pide el token de sesion a Izipay y pinta el formulario.

    El numero de orden se genera y se guarda ANTES de llamar a la pasarela. Si
    se guardara despues, un pago confirmado sin fila local quedaria huerfano: el
    cliente habria pagado sin recibir el servicio.

    POR QUE ESTA AQUI SUELTA Y NO DENTRO DE LA RUTA

      La usan dos entradas: `/suscripcion/pagar`, para el cliente que ya esta
      dentro, y `POST /comprar`, para quien llega desde la pagina publica de
      precios sin haber entrado nunca. Este es el unico sitio del sistema donde
      se genera un numero de orden y se registra el intento.

      La alternativa obvia -- copiar el bloque en la ruta nueva -- es la peor:
      el dia que una de las dos copias dejara de llamar a `registrar_intento`,
      el webhook llegaria con un numero de orden sin fila que actualizar. El
      cliente habria pagado y su cuenta no se activaria, y eso no da error en
      ningun log: solo un cobro cargado y un servicio que no llega.

    `volver_a` es adonde se devuelve al usuario si algo falla ANTES de llegar a
    la pasarela, y no es el mismo sitio en los dos casos.

    `plan` y `periodo` son lo que se compra cuando NO es el plan de la fila:
    una subida de plan, que `cambiar_plan` deja pendiente de este cobro. Viajan
    en la orden y `confirmar_pago` los aplica al confirmarse. Sin ellos se
    cobra el plan y el periodo que la cuenta ya tiene.
    """
    if izipay.modo() == "simulado" and not simulacion_permitida():
        # La pasarela sin credenciales fuera de un entorno de pruebas: abrir el
        # formulario simulado aqui seria ensenar un "pago" que no cobra nada.
        log.error("Cobro pedido con Izipay en modo simulado fuera de dev/test")
        return RedirectResponse(
            _con_error(volver_a, "El pago no está disponible ahora mismo. "
                                 "Escríbenos y lo resolvemos."),
            status_code=303)

    susc = await estado_suscripcion(usuario["id"])
    if not susc.get("existe"):
        return RedirectResponse(_con_error(volver_a, "No tienes plan seleccionado"),
                                status_code=303)

    if not (plan and periodo):
        # El plan de la fila, no el que se ve: una cuenta degradada al gratuito
        # paga para volver al suyo, y `estado_suscripcion` pisa plan_codigo y
        # precios con los del gratuito en ese caso. Por eso se leen de nuevo.
        async with connection() as conn:
            fila = await conn.fetchrow(
                "SELECT plan_codigo, periodo FROM suscripciones WHERE usuario_id=$1",
                usuario["id"])
        plan, periodo = fila["plan_codigo"], fila["periodo"]
    elegido = await plan_y_precio(plan, periodo)
    monto = (elegido or {}).get("monto")
    if not monto:
        return RedirectResponse(
            _con_error(volver_a, "Ese plan no tiene precio configurado"),
            status_code=303)

    numero_orden = izipay.nuevo_numero_orden()
    if not await registrar_intento(usuario["id"], monto, numero_orden,
                                   plan_codigo=plan, periodo=periodo):
        return RedirectResponse(_con_error(volver_a, "No se pudo iniciar el cobro"),
                                status_code=303)

    # El importe viaja como Decimal: `izipay.importe` lo formatea sin pasar por
    # float, para que lo enviado coincida al centimo con lo registrado.
    resultado = await izipay.generar_token_sesion(
        numero_orden, monto, usuario["email"])

    if not resultado["ok"]:
        log.error("Izipay no devolvio token para %s: %s", numero_orden,
                  resultado.get("detalle"))
        return RedirectResponse(
            _con_error(volver_a, "La pasarela no respondio. Intenta de nuevo"),
            status_code=303)

    return _plantillas(request).TemplateResponse(request, "pagar.html", {
        "request": request, "usuario": usuario,
        "token": resultado["token"], "modo": resultado["modo"],
        "numero_orden": numero_orden, "monto": monto,
        "plan": elegido["nombre"], "periodo": periodo,
    })


@router.post("/suscripcion/pagar")
async def pagar(request: Request):
    """Cobro para quien ya tiene cuenta. La mecanica esta en `iniciar_cobro`."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    return await iniciar_cobro(request, usuario)


@router.post("/suscripcion/retorno")
async def retorno(request: Request, numero_orden: str = Form(""),
                  simulado: str = Form("")):
    """Vuelta del navegador tras el formulario de pago.

    El retorno del navegador NO es prueba de pago: lo manda el cliente. La
    confirmacion de verdad llega por webhook, firmada. En modo simulado si se
    acepta, porque existe justamente para probar el flujo sin pasarela -- pero
    solo en un entorno de pruebas declarado (`simulacion_permitida`) y solo
    para una orden de quien la manda: el numero de orden viene del formulario,
    y sin esa comprobacion se podria confirmar la orden pendiente de otro.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    if simulado:
        if not simulacion_permitida():
            log.warning("Retorno simulado de %s rechazado: la simulacion no esta "
                        "permitida en este entorno", usuario["email"])
            return RedirectResponse(_con_error(
                "/suscripcion", "Ese pago no se puede confirmar desde aquí."),
                status_code=303)
        if not await orden_del_usuario(numero_orden, usuario["id"]):
            log.warning("Retorno simulado de %s con una orden ajena o inexistente: %s",
                        usuario["email"], numero_orden)
            return RedirectResponse(_con_error(
                "/suscripcion", "Ese pago no se puede confirmar desde aquí."),
                status_code=303)
        await confirmar_pago(numero_orden, f"SIM-{numero_orden}", {"origen": "simulado"})
        return RedirectResponse("/suscripcion?aviso=Pago+simulado+confirmado",
                                status_code=303)

    return RedirectResponse(
        "/suscripcion?aviso=Pago+enviado.+Se+confirmara+cuando+la+pasarela+avise",
        status_code=303)


@router.get("/webhooks/izipay")
async def comprobacion_webhook():
    """Responde al validador de Izipay, que comprueba la URL antes de aceptarla.

    POR QUE HACE FALTA UNA RUTA GET AQUI

      La regla de notificacion se llama literalmente [CHECKURL]: antes de
      guardarla, Izipay pide la direccion y exige que conteste. Como el webhook
      solo admitia POST -- que es lo correcto para el aviso real --, el
      validador recibia 405 y rechazaba la URL con "dominio desconocido o
      inaccesible", que apunta a un problema de DNS y no lo era.

    ESTO NO ABRE NADA

      Devuelve una constante. No lee la peticion, no toca la base y no cambia
      ningun estado. El cobro sigue entrando SOLO por POST y con firma HMAC
      verificada; un aviso sin firmar se rechaza con 401 pase lo que pase.
      Aceptar GET aqui equivaldria a dejar que cualquiera confirmase pagos.
    """
    return JSONResponse({"ok": True, "servicio": "webhook izipay"})


_CENTIMO = Decimal("0.01")


def _soles(valor) -> Decimal | None:
    """`valor` como Decimal a dos decimales, o None si no es un importe."""
    try:
        d = Decimal(str(valor).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d.quantize(_CENTIMO, rounding=ROUND_HALF_UP)


def importe_pagado(datos: dict) -> tuple[Decimal | None, str | None]:
    """(importe en soles, moneda) que dice el aviso de Izipay; None si no viene.

    POR_CONFIRMAR: el cuerpo exacto del IPN. Se aceptan las formas
    documentadas: `amount` en soles ("99.00") arriba, en `response` o en
    `response.order[0]`; u `orderDetails.orderTotalAmount` en CENTIMOS (9900),
    que es como lo manda el formulario incrustado.
    """
    respuesta = datos.get("response") if isinstance(datos.get("response"), dict) else {}
    ordenes = respuesta.get("order")
    orden = ordenes[0] if isinstance(ordenes, list) and ordenes and isinstance(
        ordenes[0], dict) else {}
    for fuente in (datos, respuesta, orden):
        if fuente.get("amount") not in (None, ""):
            return _soles(fuente["amount"]), (fuente.get("currency") or None)
    detalle = datos.get("orderDetails") if isinstance(datos.get("orderDetails"), dict) else {}
    if detalle.get("orderTotalAmount") not in (None, ""):
        centimos = _soles(detalle["orderTotalAmount"])
        return ((centimos / 100).quantize(_CENTIMO) if centimos is not None else None,
                detalle.get("orderCurrency") or None)
    return None, None


async def _importe_de_la_orden(numero_orden: str):
    """(monto, moneda) registrados al crear la orden, o None si no existe."""
    async with connection() as conn:
        return await conn.fetchrow(
            "SELECT monto, moneda FROM pagos_suscripcion WHERE izipay_order_number=$1",
            numero_orden)


@router.post("/webhooks/izipay")
async def webhook(request: Request):
    """Confirmacion servidor-a-servidor de Izipay.

    Sin sesion: la autenticidad la da la firma, no la cookie. Se rechaza todo lo
    que no venga firmado; un webhook sin verificar es una orden de cobro que
    puede mandar cualquiera.
    """
    crudo = await request.body()
    firma = (request.headers.get("x-izipay-signature")
             or request.headers.get("signature") or "")

    if not izipay.verificar_firma(crudo, firma):
        log.warning("Webhook de Izipay con firma invalida desde %s",
                    request.client.host if request.client else "?")
        return JSONResponse({"ok": False, "motivo": "firma invalida"}, status_code=401)

    try:
        datos = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "motivo": "cuerpo no es json"}, status_code=400)

    numero_orden = (datos.get("orderNumber") or datos.get("order_number")
                    or (datos.get("response") or {}).get("orderNumber"))
    if not numero_orden:
        return JSONResponse({"ok": False, "motivo": "sin numero de orden"},
                            status_code=400)

    aprobado = (str(datos.get("code")) == "00"
                or datos.get("status") in ("PAID", "AUTHORIZED"))
    if not aprobado:
        log.info("Webhook de %s indica pago no aprobado: %s", numero_orden,
                 datos.get("message"))
        return JSONResponse({"ok": True, "aplicado": False})

    # EL IMPORTE TIENE QUE SER EL DE LA ORDEN
    #
    #   La firma prueba que el aviso lo manda Izipay, no que se pago lo que
    #   pedimos. Una orden de S/ 99 pagada con S/ 1 (un formulario manipulado
    #   en el navegador, un error de la pasarela) extendia la suscripcion igual.
    #   Si no coincide, o el aviso no dice cuanto se pago, no se aplica y queda
    #   en el log para conciliarlo a mano. Se responde 200: reintentar el mismo
    #   aviso no lo va a arreglar.
    orden = await _importe_de_la_orden(numero_orden)
    if orden is not None:
        pagado, moneda = importe_pagado(datos)
        esperado = _soles(orden["monto"])
        moneda_orden = (orden["moneda"] or "PEN").upper()
        if (pagado is None or pagado != esperado
                or (moneda and str(moneda).upper() != moneda_orden)):
            log.error("Webhook de Izipay: la orden %s es de %s %s y el aviso dice %s %s. "
                      "No se aplica; conciliar a mano.", numero_orden, moneda_orden,
                      esperado, moneda or "?", pagado if pagado is not None else "sin importe")
            return JSONResponse({"ok": False, "aplicado": False,
                                 "motivo": "importe no coincide con la orden"})

    transaction_id = (datos.get("transactionId")
                      or (datos.get("response") or {}).get("transactionId"))
    aplicado = await confirmar_pago(numero_orden, transaction_id, datos)
    # 200 tambien cuando ya estaba aplicado: devolver error haria que Izipay
    # reintentara eternamente un webhook que ya procesamos bien.
    return JSONResponse({"ok": True, "aplicado": aplicado})


@router.post("/suscripcion/cancelar")
async def cancelar_suscripcion(request: Request):
    """Cancela en Culqi y aqui. El acceso se conserva hasta el fin del periodo.

    EL ORDEN IMPORTA: PRIMERO CULQI

      Marcar la cancelacion aqui y fallar alli dejaria a Culqi cobrando cada
      periodo a alguien que ve "cancelada" en su cuenta. Es la peor de las dos
      equivocaciones posibles: un cargo que el cliente no espera y que ademas
      nuestra propia pantalla desmiente.

      Al reves -- cancelar alli y fallar aqui -- deja una cuenta que dice
      "activa" y ya no se cobra. Se corrige sola en cuanto venza, y mientras
      tanto el cliente tiene de mas, no de menos.

    ES IRREVERSIBLE, Y SE AVISA ANTES

      Culqi no reactiva una suscripcion cancelada: para volver hay que crear
      otra, o sea pedir la tarjeta de nuevo. La confirmacion de la plantilla lo
      dice con esas palabras cuando hay una suscripcion viva en Culqi.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    datos = await datos_culqi(usuario["id"])
    sxn = (datos or {}).get("culqi_subscription_id")
    if sxn:
        try:
            await culqi.cancelar_suscripcion(sxn)
        except culqi.ErrorCulqi as e:
            log.error("No se pudo cancelar la suscripcion %s de %s en Culqi: %s",
                      sxn, usuario["email"], e.merchant_message)
            return RedirectResponse(_con_error(
                "/suscripcion",
                "No pudimos cancelar el cobro automático. No hemos tocado nada: "
                "vuelve a intentarlo o escríbenos y lo cancelamos nosotros."),
                status_code=303)
        await limpiar_culqi(usuario["id"])
        log.info("Suscripcion de Culqi %s cancelada por %s", sxn, usuario["email"])

    await cancelar(usuario["id"])
    return RedirectResponse(
        "/suscripcion?aviso=Suscripcion+cancelada.+Conservas+el+acceso+hasta+el+fin+del+periodo",
        status_code=303)
