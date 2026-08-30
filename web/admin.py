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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from shared import ia
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
