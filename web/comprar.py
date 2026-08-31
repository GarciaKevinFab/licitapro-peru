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

from shared.db import connection, crear_usuario
from shared.seguridad import hashear_password, password_debil
from shared.suscripciones import cambiar_plan
from web.auth import usuario_actual
from web.suscripcion import _con_error, iniciar_cobro

log = logging.getLogger("web.comprar")
router = APIRouter()

PERIODOS = ("mensual", "anual")

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
    }
    return {k: v.strip() for k, v in campos.items() if v.strip()}


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

    return _plantillas(request).TemplateResponse("comprar.html", {
        "request": request,
        "usuario": await usuario_actual(request),
        "plan": plan,
        "periodo": periodo,
        "importe": _desglose(_precio(plan, periodo)),
        "comercio": _comercio(),
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
