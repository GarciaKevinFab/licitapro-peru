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

from datetime import date, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared import ia
from shared.admin_cuentas import (
    FILTROS_ESTADO, filtrar_cuentas, ingresos_del_mes, listar_cuentas,
    planes_activos, resumir_cuentas,
)
from shared.suscripciones import activar_manual
from web.auth import usuario_actual

log = logging.getLogger("web.admin")
router = APIRouter()


def _plantillas(request: Request):
    return request.app.state.templates


async def _exige_dueno(request: Request):
    """El usuario de la sesion, si es el dueno. Si no, 404."""
    correo_dueno = (os.getenv("LICITAPRO_ADMIN_EMAIL") or "").strip().lower()
    if not correo_dueno:
        raise HTTPException(status_code=404)

    usuario = await usuario_actual(request)
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


@router.post("/admin/clientes/{usuario_id}/plan")
async def poner_plan(request: Request, usuario_id: int, plan: str = Form(...),
                     estado: str = Form(...), periodo: str = Form("mensual"),
                     vence: str = Form(""), monto: str = Form(""), nota: str = Form("")):
    usuario = await _exige_dueno(request)
    try:
        vence_dt = datetime.fromisoformat(vence) if vence.strip() else None
        monto_num = float(monto.replace(",", ".")) if monto.strip() else None
    except ValueError:
        return RedirectResponse("/admin/clientes?error=Fecha+o+monto+no+validos", status_code=303)
    ok = await activar_manual(usuario_id, plan, estado, periodo, vence_dt,
                              nota.strip() or None, monto_num, por=usuario["email"])
    if not ok:
        return RedirectResponse("/admin/clientes?error=Plan,+estado+o+periodo+no+validos", status_code=303)
    log.info("Plan puesto a mano por %s a la cuenta %s: %s/%s", usuario["email"], usuario_id, plan, estado)
    return RedirectResponse("/admin/clientes?aviso=Plan+guardado", status_code=303)
