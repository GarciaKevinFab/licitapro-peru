"""Autenticacion del panel: registro, inicio de sesion y dependencia de usuario.

La sesion viaja en una cookie firmada (SessionMiddleware de Starlette). Solo
guarda el id del usuario: cualquier otro dato se relee de la base, para que
desactivar una cuenta surta efecto en la peticion siguiente y no al expirar la
cookie.
"""
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.db import (
    crear_usuario, get_usuario, get_usuario_por_email,
)
from shared.seguridad import hashear_password, password_debil, verificar_password

log = logging.getLogger("web.auth")
router = APIRouter()

# Mensaje unico para credenciales invalidas: distinguir "no existe" de
# "contrasena incorrecta" le regala al atacante la lista de correos registrados.
ERROR_CREDENCIALES = "Correo o contraseña incorrectos."


async def usuario_actual(request: Request):
    """Usuario de la sesion, o None. Relee de la BD en cada peticion."""
    uid = request.session.get("usuario_id")
    if not uid:
        return None
    fila = await get_usuario(uid)
    if not fila:
        # La cuenta se desactivo o se borro mientras la sesion seguia viva.
        request.session.clear()
        return None
    return fila


def _plantillas(request: Request):
    return request.app.state.templates


@router.get("/entrar", response_class=HTMLResponse)
async def form_entrar(request: Request, siguiente: str = "/"):
    if await usuario_actual(request):
        return RedirectResponse(siguiente, status_code=303)
    return _plantillas(request).TemplateResponse(
        "entrar.html", {"request": request, "modo": "entrar", "siguiente": siguiente})


@router.post("/entrar", response_class=HTMLResponse)
async def hacer_entrar(request: Request, email: str = Form(...),
                       password: str = Form(...), siguiente: str = Form("/")):
    fila = await get_usuario_por_email(email)
    if not fila or not verificar_password(password, fila["password_hash"]):
        log.info("Intento de acceso fallido para %r", email[:40])
        return _plantillas(request).TemplateResponse(
            "entrar.html",
            {"request": request, "modo": "entrar", "siguiente": siguiente,
             "error": ERROR_CREDENCIALES, "email": email},
            status_code=401)
    request.session["usuario_id"] = fila["id"]
    return RedirectResponse(_destino_seguro(siguiente), status_code=303)


@router.get("/registro", response_class=HTMLResponse)
async def form_registro(request: Request):
    if await usuario_actual(request):
        return RedirectResponse("/", status_code=303)
    return _plantillas(request).TemplateResponse(
        "entrar.html", {"request": request, "modo": "registro"})


@router.post("/registro", response_class=HTMLResponse)
async def hacer_registro(request: Request, email: str = Form(...),
                         password: str = Form(...), nombre: str = Form("")):
    plantillas = _plantillas(request)

    def con_error(msg: str):
        return plantillas.TemplateResponse(
            "entrar.html",
            {"request": request, "modo": "registro", "error": msg,
             "email": email, "nombre": nombre},
            status_code=400)

    if "@" not in email or "." not in email.split("@")[-1]:
        return con_error("Ese correo no parece válido.")
    motivo = password_debil(password)
    if motivo:
        return con_error(motivo)

    fila = await crear_usuario(email, hashear_password(password), nombre.strip() or None)
    if not fila:
        return con_error("Ya existe una cuenta con ese correo.")

    request.session["usuario_id"] = fila["id"]
    return RedirectResponse("/configuracion", status_code=303)


@router.post("/salir")
async def salir(request: Request):
    request.session.clear()
    return RedirectResponse("/entrar", status_code=303)


def _destino_seguro(destino: str) -> str:
    """Solo rutas internas: un destino absoluto permitiria usar el login como
    trampolin hacia un sitio ajeno tras autenticar."""
    if not destino.startswith("/") or destino.startswith("//"):
        return "/"
    return destino
