"""Lo que el dueno de la plataforma necesita ver y ningun cliente debe ver.

POR QUE UNA VARIABLE DE ENTORNO Y NO UNA COLUMNA `es_admin`

  Una columna de rol es la respuesta correcta el dia que haya varias funciones
  de administracion, usuarios con permisos distintos y alguien a quien conceder
  o quitar el acceso. Hoy hay una pagina y un dueno.

  La columna traeria una migracion, un formulario para marcarla y una decision
  nueva -- quien puede convertir a otro en administrador -- para resolver algo
  que una linea del .env resuelve entero. Y tiene una propiedad util: el acceso
  NO vive en la base. Quien consiga escribir en `usuarios` no se hace
  administrador; hace falta ademas el entorno del servidor.

  Si esto crece, pasar a una columna es directo y este modulo es el unico sitio
  que cambia.

POR QUE FALLA CERRADO

  Sin `LICITAPRO_ADMIN_EMAIL` definida, la pagina responde 404 a todo el mundo,
  incluido el dueno. La alternativa -- abrirla cuando no hay nadie configurado
  -- convertiria un despliegue con una variable olvidada en una lista publica
  de los correos de tus clientes y de cuanto consume cada uno.

POR QUE 404 Y NO 403

  Un 403 confirma que la ruta existe. Para una pagina que usa una sola persona,
  no responder es mejor que responder "aqui hay algo que no puedes ver".
"""
import logging
import os

from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared import ia
from shared.admin_cuentas import (
    FILTROS_ESTADO, borrar_cuenta_completa, cambiar_password, crear_cuenta, cuenta,
    detalle_cuenta, editar_cuenta, filtrar_cuentas, ingresos_del_mes, listar_cuentas,
    password_temporal, planes_activos, poner_activo, resumir_cuentas,
)
from shared.db import get_usuario
from shared.seguridad import hashear_password, password_debil
from shared.suscripciones import DIAS_PRUEBA, activar_manual
from web.auth import usuario_actual

log = logging.getLogger("web.admin")
router = APIRouter()


def _plantillas(request: Request):
    return request.app.state.templates


async def _exige_dueno(request: Request):
    """El usuario de la sesion, si es el dueno. Si no, 404.

    Mientras el dueno esta DENTRO de la cuenta de un cliente ("entrar como"),
    `usuario_id` en la sesion es el del cliente y `admin_original` guarda el
    suyo. Aqui se mira el original: asi el panel de administracion sigue
    respondiendo desde dentro -- para salir, para abrir otra ficha -- y las
    acciones quedan registradas a nombre del dueno, no del cliente.
    """
    correo_dueno = (os.getenv("LICITAPRO_ADMIN_EMAIL") or "").strip().lower()
    if not correo_dueno:
        raise HTTPException(status_code=404)

    original = request.session.get("admin_original")
    usuario = await get_usuario(original) if original else await usuario_actual(request)
    if not usuario or (usuario["email"] or "").strip().lower() != correo_dueno:
        # Se registra el intento CON sesion: si alguien esta probando rutas de
        # administracion, conviene que quede escrito quien.
        if usuario:
            log.warning("Acceso a /admin denegado a %s", usuario["email"])
        raise HTTPException(status_code=404)
    return usuario


@router.get("/admin/ia", response_class=HTMLResponse)
async def gasto_ia(request: Request):
    """Cuanto cuesta la IA: por mes, por plan y por cliente.

    Es la respuesta a "¿el plan Pro deja margen?", que antes solo se podia
    contestar con la factura de Anthropic a fin de mes, y sin manera de
    repartirla por plan.
    """
    usuario = await _exige_dueno(request)
    return _plantillas(request).TemplateResponse("admin_ia.html", {
        "request": request, "usuario": usuario,
        "g": await ia.gasto_detallado(),
    })


@router.get("/admin/clientes", response_class=HTMLResponse)
async def clientes(request: Request, q: str = "", plan: str = "", estado: str = "",
                   aviso: str = "", error: str = ""):
    """Cada cuenta con su plan, sus recuentos y las acciones sobre ella.

    Es la consola multi-cuenta: la misma pantalla desde la que en CargoXprez
    se listan las empresas y se entra en cualquiera. Aqui el inquilino es la
    cuenta (`usuarios`), y las empresas cuelgan de ella.

    Los filtros van por GET para que una busqueda se pueda guardar como
    marcador y compartir por el enlace: "mira esta cuenta" es un URL.
    """
    usuario = await _exige_dueno(request)
    filas, con_acceso = await listar_cuentas()
    if estado not in FILTROS_ESTADO:
        estado = ""
    return _plantillas(request).TemplateResponse("admin_clientes.html", {
        "request": request, "usuario": usuario,
        "cuentas": filtrar_cuentas(filas, q, plan, estado),
        "cifras": {**resumir_cuentas(filas), "ingresos": await ingresos_del_mes()},
        "planes": await planes_activos(), "con_acceso": con_acceso,
        "q": q, "plan": plan, "estado": estado, "estados": FILTROS_ESTADO,
        "hoy": date.today(), "aviso": aviso, "error": error,
    })


def _volver(usuario_id: int, volver: str, **params) -> RedirectResponse:
    """A donde se vuelve tras una accion: al detalle de la cuenta si el
    formulario lo pidio (`volver=detalle`), si no a la lista. El aviso o el
    error viajan en la URL, como en el resto del panel."""
    destino = f"/admin/clientes/{usuario_id}" if volver == "detalle" else "/admin/clientes"
    limpios = {k: v for k, v in params.items() if v}
    return RedirectResponse(destino + ("?" + urlencode(limpios) if limpios else ""),
                            status_code=303)


@router.post("/admin/clientes/{usuario_id}/plan")
async def poner_plan(request: Request, usuario_id: int, plan: str = Form(...),
                     estado: str = Form(...), periodo: str = Form("mensual"),
                     vence: str = Form(""), monto: str = Form(""), nota: str = Form(""),
                     volver: str = Form("")):
    usuario = await _exige_dueno(request)
    try:
        vence_dt = datetime.fromisoformat(vence) if vence.strip() else None
        monto_num = float(monto.replace(",", ".")) if monto.strip() else None
    except ValueError:
        return _volver(usuario_id, volver, error="Fecha o monto no válidos")
    ok = await activar_manual(usuario_id, plan, estado, periodo, vence_dt,
                              nota.strip() or None, monto_num, por=usuario["email"])
    if not ok:
        return _volver(usuario_id, volver, error="Plan, estado o periodo no válidos")
    log.info("Plan puesto a mano por %s a la cuenta %s: %s/%s", usuario["email"], usuario_id, plan, estado)
    return _volver(usuario_id, volver, aviso="Plan guardado")


# ─── Detalle ─────────────────────────────────────────────

@router.get("/admin/clientes/{usuario_id}", response_class=HTMLResponse)
async def detalle(request: Request, usuario_id: int, aviso: str = "", error: str = ""):
    """Una cuenta entera: suscripcion, pagos, empresas, uso y canales, con las
    acciones al lado de lo que describen. Es la ficha que se abre cuando un
    cliente escribe diciendo que algo no le funciona."""
    usuario = await _exige_dueno(request)
    d = await detalle_cuenta(usuario_id)
    if not d:
        raise HTTPException(status_code=404)
    return _plantillas(request).TemplateResponse("admin_cliente.html", {
        "request": request, "usuario": usuario, **d,
        "planes": await planes_activos(), "hoy": date.today(),
        "es_dueno": (d["c"]["email"] or "").lower() == (usuario["email"] or "").lower(),
        "aviso": aviso, "error": error,
    })


# ─── Entrar como, salir y eliminar ───────────────────────

@router.post("/admin/clientes/{usuario_id}/entrar")
async def entrar_como(request: Request, usuario_id: int):
    """El dueno pasa a ver el producto exactamente como lo ve el cliente.

    Es el "entrar a esta empresa" de CargoXprez: la sesion pasa a ser la del
    cliente y se guarda de quien era antes para poder volver. La franja ambar
    de _nav.html avisa mientras dure, porque lo que se haga dentro queda en
    los datos del cliente.
    """
    usuario = await _exige_dueno(request)
    c = await cuenta(usuario_id)
    if not c:
        raise HTTPException(status_code=404)
    if (c["email"] or "").lower() == (usuario["email"] or "").lower():
        return _volver(usuario_id, "detalle", error="Ya eres tú: no hay en qué entrar.")
    if not c["activo"]:
        # usuario_actual limpia la sesion si la cuenta esta desactivada: el
        # dueno se quedaria fuera del todo en la peticion siguiente.
        return _volver(usuario_id, "detalle", error="Activa la cuenta antes de entrar en ella.")
    request.session["admin_original"] = usuario["id"]
    request.session["usuario_id"] = c["id"]
    log.info("Admin %s entro en la cuenta %s (%s)", usuario["email"], c["id"], c["email"])
    return RedirectResponse("/panel", status_code=303)


@router.post("/admin/salir")
async def salir_de_cuenta(request: Request):
    """Devuelve al dueno a su propia sesion.

    Ruta propia y no "entra en tu cuenta": el camino de vuelta tiene que
    funcionar siempre, incluso si la cuenta visitada se borro o se desactivo
    mientras tanto y `usuario_actual` ya no la resuelve. Por eso no pasa por
    _exige_dueno: basta con que la sesion lleve `admin_original`.
    """
    original = request.session.pop("admin_original", None)
    if not original:
        return RedirectResponse("/panel", status_code=303)
    visitada = request.session.get("usuario_id")
    request.session["usuario_id"] = original
    log.info("Admin %s salio de la cuenta %s", original, visitada)
    destino = f"/admin/clientes/{visitada}" if visitada else "/admin/clientes"
    return RedirectResponse(destino, status_code=303)


@router.post("/admin/clientes/{usuario_id}/eliminar")
async def eliminar(request: Request, usuario_id: int, confirmacion: str = Form("")):
    """Borra la cuenta y todo lo suyo. Exige escribir el correo exacto.

    La comprobacion del correo se hace AQUI aunque el <dialog> ya la haga:
    lo del navegador evita el clic en falso, esto evita el POST forjado.
    """
    usuario = await _exige_dueno(request)
    c = await cuenta(usuario_id)
    if not c:
        raise HTTPException(status_code=404)
    if (c["email"] or "").lower() == (usuario["email"] or "").lower():
        return _volver(usuario_id, "detalle", error="Tu propia cuenta no se elimina desde aquí.")
    if confirmacion.strip().lower() != (c["email"] or "").lower():
        return _volver(usuario_id, "detalle",
                       error="El correo escrito no coincide: no se eliminó nada.")
    if request.session.get("usuario_id") == c["id"]:
        # Se estaba dentro de esa cuenta: primero se sale, o la sesion
        # quedaria apuntando a una fila que ya no existe.
        request.session["usuario_id"] = request.session.pop("admin_original")
    try:
        resumen = await borrar_cuenta_completa(usuario_id)
    except RuntimeError as e:
        log.error("Admin %s no pudo eliminar la cuenta %s: %s", usuario["email"], usuario_id, e)
        return _volver(usuario_id, "detalle", error=str(e))
    log.warning("Admin %s ELIMINO la cuenta %s (%s): %s", usuario["email"], usuario_id, c["email"], resumen)
    return RedirectResponse(
        "/admin/clientes?" + urlencode({"aviso": f"Cuenta {c['email']} eliminada con todos sus datos."}),
        status_code=303)


# ─── Alta y edicion ──────────────────────────────────────

def _fecha(texto: str):
    """date desde el <input type=date>, o None si viene vacio. ValueError si
    no es una fecha."""
    return datetime.fromisoformat(texto.strip()) if texto.strip() else None


@router.get("/admin/clientes/nueva", response_class=HTMLResponse)
async def form_nueva(request: Request):
    """Alta de una cuenta por el dueno: para el cliente que llama por telefono
    y al que se le manda el acceso ya hecho, con su plan puesto."""
    usuario = await _exige_dueno(request)
    return _plantillas(request).TemplateResponse("admin_cliente_form.html", {
        "request": request, "usuario": usuario, "modo": "nueva", "c": None,
        "planes": await planes_activos(),
        "valores": {"email": "", "nombre": "", "plan": "pro", "estado": "prueba",
                    "periodo": "mensual",
                    "vence": (date.today() + timedelta(days=DIAS_PRUEBA)).isoformat()},
        "password_sugerida": password_temporal(), "error": "",
    })


@router.post("/admin/clientes/nueva", response_class=HTMLResponse)
async def crear_nueva(request: Request, email: str = Form(...), nombre: str = Form(""),
                      password: str = Form(...), plan: str = Form(...),
                      estado: str = Form("prueba"), periodo: str = Form("mensual"),
                      vence: str = Form("")):
    usuario = await _exige_dueno(request)
    valores = {"email": email, "nombre": nombre, "plan": plan, "estado": estado,
               "periodo": periodo, "vence": vence}

    def con_error(msg: str):
        return _plantillas(request).TemplateResponse("admin_cliente_form.html", {
            "request": request, "usuario": usuario, "modo": "nueva", "c": None,
            "planes": planes, "valores": valores, "password_sugerida": password,
            "error": msg}, status_code=400)

    planes = await planes_activos()
    motivo = password_debil(password)
    if motivo:
        return con_error(motivo)
    try:
        vence_dt = _fecha(vence)
    except ValueError:
        return con_error("La fecha de vencimiento no es válida.")
    fila, error = await crear_cuenta(email, nombre, hashear_password(password),
                                     plan, estado, periodo, vence_dt, por=usuario["email"])
    if not fila:
        return con_error(error)
    log.info("Admin %s creo la cuenta %s (%s)", usuario["email"], fila["id"], fila["email"])
    aviso = error or "Cuenta creada. Entrega la contraseña por un canal seguro."
    return _volver(fila["id"], "detalle", aviso=aviso)


@router.get("/admin/clientes/{usuario_id}/editar", response_class=HTMLResponse)
async def form_editar(request: Request, usuario_id: int, error: str = ""):
    usuario = await _exige_dueno(request)
    c = await cuenta(usuario_id)
    if not c:
        raise HTTPException(status_code=404)
    return _plantillas(request).TemplateResponse("admin_cliente_form.html", {
        "request": request, "usuario": usuario, "modo": "editar", "c": c,
        "planes": await planes_activos(),
        "valores": {"email": c["email"], "nombre": c["nombre"] or "", "activo": c["activo"]},
        "password_sugerida": "", "error": error,
    })


@router.post("/admin/clientes/{usuario_id}/editar", response_class=HTMLResponse)
async def guardar_editar(request: Request, usuario_id: int, email: str = Form(...),
                         nombre: str = Form(""), activo: str = Form("")):
    usuario = await _exige_dueno(request)
    c = await cuenta(usuario_id)
    if not c:
        raise HTTPException(status_code=404)
    es_dueno = (c["email"] or "").lower() == (usuario["email"] or "").lower()
    # El dueno no se desactiva ni se cambia el correo a si mismo desde aqui:
    # perderia el acceso al panel de administracion, que se decide por ese
    # correo, y no habria desde donde deshacerlo.
    if es_dueno and (not activo or email.strip().lower() != c["email"].lower()):
        error = "Tu propia cuenta no se desactiva ni cambia de correo desde aquí."
    else:
        error = await editar_cuenta(usuario_id, nombre, email, bool(activo))
    if error:
        return _plantillas(request).TemplateResponse("admin_cliente_form.html", {
            "request": request, "usuario": usuario, "modo": "editar", "c": c,
            "planes": await planes_activos(),
            "valores": {"email": email, "nombre": nombre, "activo": bool(activo)},
            "password_sugerida": "", "error": error}, status_code=400)
    log.info("Admin %s edito la cuenta %s: correo=%s activo=%s",
             usuario["email"], usuario_id, email.strip().lower(), bool(activo))
    return _volver(usuario_id, "detalle", aviso="Cuenta guardada")


@router.post("/admin/clientes/{usuario_id}/password")
async def poner_password(request: Request, usuario_id: int, password: str = Form(...),
                         volver: str = Form("detalle")):
    usuario = await _exige_dueno(request)
    motivo = password_debil(password)
    if motivo:
        return _volver(usuario_id, volver, error=motivo)
    if not await cambiar_password(usuario_id, hashear_password(password)):
        raise HTTPException(status_code=404)
    log.info("Admin %s cambio la contrasena de la cuenta %s", usuario["email"], usuario_id)
    return _volver(usuario_id, volver, aviso="Contraseña cambiada. Entrégala por un canal seguro.")


@router.post("/admin/clientes/{usuario_id}/activo")
async def poner_activo_ruta(request: Request, usuario_id: int, activo: str = Form(...),
                            volver: str = Form("")):
    usuario = await _exige_dueno(request)
    c = await cuenta(usuario_id)
    if not c:
        raise HTTPException(status_code=404)
    encender = activo.strip() in ("1", "true", "si", "on")
    if not encender and (c["email"] or "").lower() == (usuario["email"] or "").lower():
        return _volver(usuario_id, volver, error="Tu propia cuenta no se desactiva desde aquí.")
    await poner_activo(usuario_id, encender)
    log.info("Admin %s %s la cuenta %s (%s)", usuario["email"],
             "activo" if encender else "desactivo", usuario_id, c["email"])
    return _volver(usuario_id, volver,
                   aviso="Cuenta activada" if encender else "Cuenta desactivada: ya no puede entrar")
