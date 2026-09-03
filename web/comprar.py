"""Compra publica: precios, checkout y pago sin haber entrado todavia.

POR QUE EXISTE ESTE MODULO

  El cobro estaba entero detras del login: /suscripcion -> /suscripcion/pagar
  -> pagar.html. Sirve para quien ya es cliente y es INVISIBLE para el resto,
  incluido el validador de la pasarela, que mira el sitio desde fuera y sin
  cuenta.

  Izipay rechazo la integracion con estas palabras: "no cuentas con un carrito
  de compras, proceso de checkout o boton de pago". Mirando lo que se ve desde
  fuera, tenian razon: la portada anunciaba tres precios y los tres botones
  llevaban a un formulario de REGISTRO. En ninguna pagina publica habia un
  importe, un desglose ni un boton de pagar.

  Y el agujero no solo bloqueaba la validacion: obligar a crear una cuenta
  antes de ensenar el total es justo donde se cae la gente que ya habia
  decidido comprar.

QUE ANADE

  GET  /precios          los planes con precio y un boton de contratar por plan
  GET  /comprar/{plan}   resumen del pedido, desglose con IGV y boton de pagar
  POST /comprar          crea la cuenta si hace falta, fija el plan y cobra

EL COBRO NO SE DUPLICA

  `iniciar_cobro` vive en web/suscripcion.py y se reutiliza tal cual. Es el
  unico sitio donde se genera un numero de orden y se registra el intento;
  copiarlo aqui seria la forma callada de que un pago confirmado por webhook no
  encuentre su fila.

LA CUENTA SE CREA EN EL MISMO PASO QUE EL PAGO

  Es lo que hace que esta pagina sea un checkout de verdad y no un registro
  disfrazado. Quien llega sin sesion rellena correo y contrasena en el propio
  resumen del pedido y pulsa "Pagar": una sola pantalla desde el precio hasta
  la pasarela.
"""
import logging
import os
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import asyncpg

from shared import culqi
from shared.db import connection, crear_usuario
from shared.seguridad import hashear_password, password_debil
from shared.suscripciones import activar_por_culqi, cambiar_plan
from web.auth import usuario_actual
from web.suscripcion import _con_error, iniciar_cobro

log = logging.getLogger("web.comprar")
router = APIRouter()

PERIODOS = ("mensual", "anual")

# POR_CONFIRMAR: la URL vigente del script de Culqi Checkout v4. Se conocen dos
# formas publicadas -- https://js.culqi.com/checkout-js y
# https://checkout.culqi.com/js/v4 -- y no se ha podido comprobar cual sirve
# hoy: desde esta maquina no hay salida a Internet. Se deja por entorno
# (CULQI_CHECKOUT_JS) y la CSP admite los DOS hosts, para que confirmarlo sea
# cambiar una variable y no tocar codigo ni cabeceras.
CHECKOUT_JS = "https://checkout.culqi.com/js/v4"

# Los precios publicados YA incluyen IGV -- lo dice la portada y lo dicen los
# terminos --, asi que el desglose se calcula hacia atras. Se muestra porque un
# checkout peruano sin base imponible ni IGV a la vista no es un resumen de
# pedido, y porque es lo primero que mira quien valida un comercio.
IGV = Decimal("0.18")
CENTIMO = Decimal("0.01")


def _plantillas(request: Request):
    return request.app.state.templates


def _desglose(total) -> dict:
    """Base imponible e IGV a partir de un precio que ya lo incluye.

    En Decimal y no en float: estos tres numeros acaban en una pantalla que el
    cliente compara con el cargo de su tarjeta, y 99/1.18 en coma flotante da
    83.89830508474576, que segun por donde se redondee deja un centimo de
    descuadre entre base + IGV y el total.

    El IGV se saca RESTANDO y no multiplicando, por lo mismo: asi las tres
    cifras suman exactamente lo que se cobra, pase lo que pase con el redondeo
    de la base.
    """
    total = Decimal(str(total)).quantize(CENTIMO, rounding=ROUND_HALF_UP)
    base = (total / (1 + IGV)).quantize(CENTIMO, rounding=ROUND_HALF_UP)
    return {"base": base, "igv": total - base, "total": total}


# Prefijo de Peru. El producto es peruano de arriba abajo -- SEACE, IGV, soles,
# Indecopi --, asi que no hay nada que parametrizar aqui.
_PREFIJO_PAIS = "+51"


def _tel_uri(telefono: str) -> str:
    """El numero mostrado, convertido en algo que un movil pueda marcar.

    Un `tel:` con parentesis y espacios funciona en la mayoria de telefonos,
    pero no en todos, y el que falla no avisa: simplemente no pasa nada al
    tocarlo. Se normaliza a la forma internacional, que marca en cualquiera.

    EL CERO DE DELANTE HAY QUE QUITARLO, NO ARRASTRARLO

      "(082) 573844" lleva el 0 del prefijo interurbano peruano, que solo vale
      marcando DENTRO del pais. Pegado a +51 da +510 82..., un numero que no
      existe. Se descarta el cero y queda +5182573844.

    Un numero que ya venga en internacional se respeta tal cual: quien lo
    escriba asi sabe lo que hace.
    """
    limpio = "".join(c for c in telefono if c.isdigit() or c == "+")
    if limpio.startswith("+"):
        return limpio
    if limpio.startswith("0"):
        limpio = limpio[1:]
    return _PREFIJO_PAIS + limpio if limpio else ""


def _comercio() -> dict:
    """Identidad del titular del cobro, para el pie del checkout.

    La pasarela exige poder identificar en la propia pagina de pago a quien
    cobra: razon social, RUC y una via de contacto. Va por entorno y no escrito
    en la plantilla porque son datos del negocio, no del producto.

    Lo que falte NO se pinta, y es deliberado: un dato de relleno en un checkout
    es peor que un hueco. Tiene que coincidir letra por letra con el contrato de
    comercio, y un marcador de posicion colado en produccion es exactamente la
    clase de detalle que tumba una validacion por segunda vez.
    """
    campos = {
        "razon_social": os.getenv("LICITAPRO_RAZON_SOCIAL", ""),
        "ruc": os.getenv("LICITAPRO_RUC", ""),
        "email": os.getenv("LICITAPRO_CONTACTO_EMAIL", ""),
        "telefono": os.getenv("LICITAPRO_CONTACTO_TELEFONO", ""),
        "direccion": os.getenv("LICITAPRO_DIRECCION", ""),
        # LA MARCA NO ES DE LA EMPRESA, Y LA REDACCION LO RESPETA
        #
        #   Los certificados 00165236 ("Star Insights IT") y 00162741 (la letra
        #   S y logotipo), ambos de clase 42, estan a nombre de DOS PERSONAS
        #   NATURALES, no de la S.A.C. Por eso en ningun sitio se dice "titular
        #   de la marca": se dice que el servicio se presta BAJO esa marca, que
        #   es lo unico que los certificados respaldan.
        #
        #   Afirmar una titularidad que el registro no dice es exactamente lo
        #   que revienta cuando alguien va a Indecopi a comprobarlo -- y quien
        #   valida un comercio comprueba.
        "marca": os.getenv("LICITAPRO_MARCA", ""),
        "marca_certificado": os.getenv("LICITAPRO_MARCA_CERTIFICADO", ""),
    }
    datos = {k: v.strip() for k, v in campos.items() if v.strip()}
    # El numero para marcar viaja junto al que se lee, y NO sustituye a ese:
    # en pantalla se quiere "(082) 573844", que es como lo reconoce un local;
    # en el enlace, la forma internacional, que es la que marca siempre.
    if datos.get("telefono"):
        datos["telefono_uri"] = _tel_uri(datos["telefono"])
    return datos


# Los cuatro datos que la Ley 29733 exige para identificar al responsable del
# tratamiento. La direccion y el correo no son adorno: sin una via de contacto,
# el derecho a acceder o a borrar tus datos no se puede ejercer.
IDENTIDAD_MINIMA = ("razon_social", "ruc", "direccion", "email")


def identidad_completa() -> bool:
    """Si se puede identificar y contactar al responsable del tratamiento.

    Ata el aviso de borrador de /privacidad a un HECHO en vez de a que alguien
    se acuerde de borrarlo. Mientras falte uno de los cuatro, la politica se
    publica avisando de que esta incompleta; en cuanto esten los cuatro, el
    aviso desaparece solo.
    """
    comercio = _comercio()
    return all(comercio.get(c) for c in IDENTIDAD_MINIMA)


_COLUMNAS_PLAN = """codigo, nombre, precio_mensual, precio_anual, max_empresas,
                    max_regiones, analisis_ia, alertas"""


async def _planes() -> list[dict]:
    async with connection() as conn:
        filas = await conn.fetch(
            f"SELECT {_COLUMNAS_PLAN} FROM planes WHERE activo = TRUE ORDER BY orden")
    return [dict(f) for f in filas]


async def _plan(codigo: str) -> dict | None:
    async with connection() as conn:
        fila = await conn.fetchrow(
            f"SELECT {_COLUMNAS_PLAN} FROM planes WHERE codigo = $1 AND activo = TRUE",
            codigo)
    return dict(fila) if fila else None


def _precio(plan: dict, periodo: str):
    return plan["precio_anual"] if periodo == "anual" else plan["precio_mensual"]


_COLUMNA_CULQI = {"mensual": "culqi_plan_id_mensual",
                  "anual": "culqi_plan_id_anual"}


async def plan_culqi_id(codigo: str, periodo: str) -> str | None:
    """El `pln_` de Culqi de un plan y periodo, o None si todavia no hay.

    POR QUE NO VA EN `_COLUMNAS_PLAN` CON LAS DEMAS

      Si /precios y /comprar seleccionaran estas columnas, el sitio ENTERO
      dejaria de responder mientras la migracion 0015 no este aplicada: un
      SELECT de una columna inexistente es un error, no un NULL. Aqui se
      pregunta aparte y se traga ese caso concreto, asi que el codigo funciona
      antes y despues de migrar -- como ya se hizo con `usuarios.ultimo_acceso`.

      No es teorico: el compose aplica las migraciones antes de levantar la
      web, pero una maquina de desarrollo con la base vieja no.
    """
    columna = _COLUMNA_CULQI.get(periodo)
    if not columna:
        return None
    try:
        async with connection() as conn:
            return await conn.fetchval(
                f"SELECT {columna} FROM planes WHERE codigo = $1", codigo)
    except asyncpg.UndefinedColumnError:
        log.warning("La migracion 0015 no esta aplicada: sin columnas de Culqi "
                    "el checkout recurrente no puede funcionar.")
        return None


def _config_culqi(plan: dict, periodo: str, importe: dict, plan_id: str | None) -> dict | None:
    """Lo que necesita el navegador para abrir Culqi Checkout, o None.

    Devuelve None -- y entonces la pagina se queda con el checkout de siempre,
    de pago unico -- si falta cualquiera de las tres piezas: la pasarela
    apagada, la llave publica sin poner, o el plan sin sincronizar con
    `tools/culqi_planes.py`. Media configuracion tiene que dar el flujo viejo
    entero, nunca un boton que abre un formulario de pago que no puede cobrar.
    """
    if not culqi.cobro_recurrente():
        return None
    publica = os.getenv("CULQI_LLAVE_PUBLICA", "").strip()
    if not publica or not plan_id:
        log.warning("Culqi activo pero sin llave publica o sin plan sincronizado "
                    "para %s/%s: el checkout sigue en pago unico.",
                    plan.get("codigo"), periodo)
        return None
    return {
        "llave_publica": publica,
        "script": os.getenv("CULQI_CHECKOUT_JS", "").strip() or CHECKOUT_JS,
        # Culqi Checkout trabaja en centimos, igual que la API.
        "centimos": culqi.a_centimos(importe["total"]),
        "titulo": "LicitaPro Peru",
        # Sin tildes ni signos raros: este texto viaja a Culqi y acaba en el
        # comprobante y en el extracto de la tarjeta, donde un caracter que la
        # pasarela no digiera sale como un simbolo roto delante del cliente.
        "descripcion": f"Plan {plan['nombre']} - facturacion {periodo}",
    }


# ─── Escaparate ──────────────────────────────────────────

@router.get("/precios", response_class=HTMLResponse)
async def precios(request: Request):
    """Los planes y su precio, sin pedir nada a cambio de mirarlos.

    Existe aparte del ancla `/#planes` de la portada porque una direccion propia
    se puede mandar por WhatsApp, meter en el sitemap y dar a la pasarela como
    "aqui esta el catalogo". Un ancla dentro de una portada con scroll animado
    no sirve para ninguna de las tres cosas.
    """
    return _plantillas(request).TemplateResponse("precios.html", {
        "request": request,
        "usuario": await usuario_actual(request),
        "planes": await _planes(),
        "comercio": _comercio(),
    })


@router.get("/comprar/{plan_codigo}", response_class=HTMLResponse)
async def checkout(request: Request, plan_codigo: str,
                   periodo: str = "mensual", error: str = ""):
    """Resumen del pedido y boton de pagar. Publica: no exige sesion.

    Quien llega sin cuenta ve el mismo total que un cliente, y la crea en el
    propio formulario al pagar. Mandarle antes a /registro es lo que hacia el
    sitio, y es exactamente lo que la pasarela no encontraba.
    """
    if periodo not in PERIODOS:
        periodo = "mensual"

    plan = await _plan(plan_codigo)
    # Un plan inexistente, desactivado o de precio cero no tiene checkout. El
    # gratuito cae aqui: no hay nada que cobrar, se registra uno y ya esta.
    if not plan or not _precio(plan, periodo):
        return RedirectResponse("/precios", status_code=303)

    importe = _desglose(_precio(plan, periodo))
    return _plantillas(request).TemplateResponse("comprar.html", {
        "request": request,
        "usuario": await usuario_actual(request),
        "plan": plan,
        "periodo": periodo,
        "importe": importe,
        "comercio": _comercio(),
        # None = no hay ruta recurrente y la pagina se queda como estaba: un
        # pago unico. Los textos legales cuelgan de esto, no de una frase
        # escrita a mano; ver la plantilla.
        "culqi": _config_culqi(plan, periodo, importe,
                               await plan_culqi_id(plan_codigo, periodo)),
        "error": error,
    })


@router.post("/comprar")
async def confirmar(request: Request, plan: str = Form(...),
                    periodo: str = Form("mensual"), email: str = Form(""),
                    password: str = Form(""), nombre: str = Form("")):
    """Del boton de pagar a la pasarela, creando la cuenta por el camino si falta.

    EL ORDEN NO ES EL COMODO, Y ES A PROPOSITO

      Primero se valida el plan, despues se crea la cuenta y solo al final se
      cobra. Al reves -- crear la cuenta y luego descubrir que el plan no existe
      -- dejaria cuentas huerfanas cada vez que alguien toquetee el formulario o
      que un plan se desactive con un checkout abierto en otra pestana.
    """
    if periodo not in PERIODOS:
        periodo = "mensual"
    volver_a = f"/comprar/{quote(plan)}?periodo={periodo}"

    def con_error(msg: str):
        return RedirectResponse(_con_error(volver_a, msg), status_code=303)

    elegido = await _plan(plan)
    if not elegido or not _precio(elegido, periodo):
        return RedirectResponse("/precios", status_code=303)

    usuario = await usuario_actual(request)
    if not usuario:
        # Las mismas reglas que /registro, y por el mismo camino: si aqui se
        # relajaran, el checkout seria la puerta trasera para crear cuentas con
        # contrasenas que el formulario de registro rechaza.
        if "@" not in email or "." not in email.split("@")[-1]:
            return con_error("Ese correo no parece válido.")
        motivo = password_debil(password)
        if motivo:
            return con_error(motivo)

        usuario = await crear_usuario(email, hashear_password(password),
                                      nombre.strip() or None)
        if not usuario:
            # Ya tiene cuenta: no es un error, es un cliente que vuelve. Se le
            # manda a entrar y se le devuelve a ESTE checkout, no al panel, para
            # que no tenga que volver a buscar lo que ya habia elegido.
            aviso = ("Ya tienes una cuenta con ese correo. Entra y terminamos "
                     "la compra.")
            return RedirectResponse(
                f"/entrar?siguiente={quote(volver_a, safe='')}"
                f"&error={quote(aviso)}",
                status_code=303)
        request.session["usuario_id"] = usuario["id"]
        log.info("Cuenta creada desde el checkout publico para el plan %s", plan)

    if not await cambiar_plan(usuario["id"], plan, periodo):
        return con_error("Plan o periodo no válido.")

    return await iniciar_cobro(request, usuario, volver_a=volver_a)


# ─── Checkout recurrente con Culqi ───────────────────────

@router.post("/comprar/culqi")
async def comprar_con_culqi(request: Request, plan: str = Form(...),
                            periodo: str = Form("mensual"),
                            token_id: str = Form(...), email: str = Form(""),
                            password: str = Form(""), nombre: str = Form("")):
    """Del token de la tarjeta a una suscripcion que se cobra sola cada periodo.

    LA TARJETA NO PASA POR AQUI

      `token_id` es un `tkn_…` que Culqi Checkout genero EN EL NAVEGADOR. El
      numero de la tarjeta viaja del navegador a Culqi y nunca toca este
      servidor: es lo que mantiene a LicitaPro fuera del alcance de PCI-DSS.

    EL ORDEN ES DELIBERADO, Y NO ES EL COMODO

      1. Validaciones locales (plan, periodo, token, correo, contrasena, cuenta
         existente). Todo lo que se puede rechazar sin gastar nada, primero.
      2. Cliente y tarjeta en Culqi. Estas dos llamadas NO cobran: si algo
         falla aqui, no hay dinero movido ni cuenta creada.
      3. La cuenta local, si hace falta.
      4. La SUSCRIPCION en Culqi. Esta es la que cobra.
      5. La activacion local, en una transaccion.

      Al reves -- crear la cuenta primero -- dejaria cuentas huerfanas cada vez
      que una tarjeta rebota, que es a diario. Y cobrar antes de tener cuenta
      dejaria un cargo sin servicio, que es peor.

      El unico hueco que queda es entre 4 y 5: cobrado en Culqi y sin asentar
      aqui. Se cierra cancelando la suscripcion recien creada, que es lo que
      deja al cliente como estaba. No es perfecto -- el primer cobro puede
      haberse hecho y habria que devolverlo a mano -- pero es visible en el log
      con el `sxn_` y el correo, en vez de callado.
    """
    if periodo not in PERIODOS:
        periodo = "mensual"
    volver_a = f"/comprar/{quote(plan)}?periodo={periodo}"

    def con_error(msg: str):
        return RedirectResponse(_con_error(volver_a, msg), status_code=303)

    # ─── 1. Lo que se puede rechazar sin gastar nada ────
    elegido = await _plan(plan)
    if not elegido or not _precio(elegido, periodo):
        return RedirectResponse("/precios", status_code=303)

    if not culqi.cobro_recurrente():
        # La pagina no deberia haber ensenado este boton. Si alguien llega
        # igualmente, se le manda al checkout de siempre en vez de fingir.
        log.warning("POST /comprar/culqi con la pasarela en modo simulado")
        return con_error("El pago recurrente no está disponible ahora mismo.")

    plan_id = await plan_culqi_id(plan, periodo)
    if not plan_id:
        log.error("Sin plan de Culqi para %s/%s: falta correr "
                  "tools/culqi_planes.py", plan, periodo)
        return con_error("Ese plan aún no está disponible para pago automático.")

    token_id = (token_id or "").strip()
    if not token_id.startswith("tkn_"):
        # No es una validacion de seguridad -- Culqi rechazaria el token igual
        # -- sino de claridad: asi el error es "vuelve a intentarlo" y no un
        # mensaje de la pasarela sobre un parametro.
        return con_error("No se recibió la tarjeta. Vuelve a intentarlo.")

    usuario = await usuario_actual(request)
    if not usuario:
        # Las mismas reglas que /registro y que POST /comprar: si aqui se
        # relajaran, este seria el camino para crear cuentas con contrasenas
        # que el formulario de registro rechaza.
        if "@" not in email or "." not in email.split("@")[-1]:
            return con_error("Ese correo no parece válido.")
        motivo = password_debil(password)
        if motivo:
            return con_error(motivo)

    correo = usuario["email"] if usuario else email.strip()
    monto = _precio(elegido, periodo)

    # ─── 2. Cliente y tarjeta: todavia no se cobra ──────
    try:
        cliente = await culqi.crear_cliente(correo, nombre=nombre.strip())
        tarjeta = await culqi.crear_tarjeta(cliente["id"], token_id)
    except culqi.ErrorCulqi as e:
        log.warning("Culqi rechazo la tarjeta de %s: %s", correo, e.merchant_message)
        return con_error(e.user_message)
    except culqi.ConfiguracionCulqi as e:
        log.error("Culqi mal configurado: %s", e)
        return con_error("El pago no está disponible ahora mismo. "
                         "Escríbenos y lo resolvemos.")

    # ─── 3. La cuenta, ya con la tarjeta validada ───────
    if not usuario:
        usuario = await crear_usuario(email, hashear_password(password),
                                      nombre.strip() or None)
        if not usuario:
            # Ya tiene cuenta: no es un error, es un cliente que vuelve. Se le
            # manda a entrar y se le devuelve a ESTE checkout. No se ha cobrado
            # nada: el cus_/crd_ creado arriba se reaprovecha o se queda
            # inerte en Culqi, que no cuesta nada.
            aviso = ("Ya tienes una cuenta con ese correo. Entra y terminamos "
                     "la compra.")
            return RedirectResponse(
                f"/entrar?siguiente={quote(volver_a, safe='')}"
                f"&error={quote(aviso)}",
                status_code=303)
        request.session["usuario_id"] = usuario["id"]
        log.info("Cuenta creada desde el checkout de Culqi para el plan %s", plan)

    # ─── 4. La suscripcion: AQUI se cobra ───────────────
    try:
        suscripcion = await culqi.crear_suscripcion(
            tarjeta["id"], plan_id,
            metadata={"usuario_id": usuario["id"], "plan_codigo": plan,
                      "periodo": periodo, "producto": "licitapro"})
    except culqi.ErrorCulqi as e:
        log.warning("Culqi rechazo la suscripcion de %s: %s",
                    correo, e.merchant_message)
        return con_error(e.user_message)

    # ─── 5. Asentarlo aqui, o deshacerlo alli ──────────
    marca, ultimos = culqi.marca_y_ultimos(tarjeta)
    asentado = await activar_por_culqi(
        usuario_id=usuario["id"], plan_codigo=plan, periodo=periodo,
        monto=monto, customer_id=cliente["id"], card_id=tarjeta["id"],
        subscription_id=suscripcion["id"], marca=marca, ultimos=ultimos,
        respuesta={"origen": "checkout", "suscripcion": suscripcion})

    if not asentado:
        log.error("Suscripcion %s creada en Culqi para %s y NO asentada aqui: "
                  "se cancela en Culqi", suscripcion["id"], correo)
        try:
            await culqi.cancelar_suscripcion(suscripcion["id"])
        except culqi.ErrorCulqi as e:
            log.error("Y tampoco se pudo cancelar %s: %s. REVISAR A MANO en el "
                      "panel de Culqi.", suscripcion["id"], e.merchant_message)
        return con_error("No pudimos activar tu plan. No se te cobrará; "
                         "escríbenos y lo resolvemos.")

    return RedirectResponse(
        "/suscripcion?aviso=Suscripcion+activada.+Se+renueva+sola+cada+periodo",
        status_code=303)
