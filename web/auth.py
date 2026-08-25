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


# ─── Recuperacion de contrasena ──────────────────────────

# Mismo mensaje exista o no la cuenta. Decir "ese correo no esta registrado"
# convierte el formulario en un comprobador de correos: cualquiera puede
# averiguar quien es cliente probando direcciones.
MENSAJE_ENVIADO = ("Si ese correo tiene una cuenta, te enviamos un enlace para "
                   "restablecer la contraseña. Revisa tu bandeja y el spam. "
                   "El enlace vence en una hora.")


@router.get("/recuperar", response_class=HTMLResponse)
async def form_recuperar(request: Request):
    return _plantillas(request).TemplateResponse(
        "recuperar.html", {"request": request, "paso": "pedir"})


@router.post("/recuperar", response_class=HTMLResponse)
async def pedir_recuperar(request: Request, email: str = Form(...)):
    from shared.db import (
        crear_token_recuperacion, get_usuario_por_email, peticiones_recientes,
    )
    from shared.seguridad import MAX_PETICIONES_POR_HORA, nuevo_token_recuperacion

    plantillas = _plantillas(request)
    respuesta_uniforme = plantillas.TemplateResponse(
        "recuperar.html",
        {"request": request, "paso": "pedir", "aviso": MENSAJE_ENVIADO})

    usuario = await get_usuario_por_email(email)
    if not usuario:
        # Se responde igual que si existiera, y no se hace nada mas.
        log.info("Recuperacion pedida para un correo sin cuenta")
        return respuesta_uniforme

    if await peticiones_recientes(usuario["id"]) >= MAX_PETICIONES_POR_HORA:
        # Tampoco se avisa del limite: decirlo confirmaria que la cuenta existe.
        log.warning("Limite de recuperaciones alcanzado para el usuario %s",
                    usuario["id"])
        return respuesta_uniforme

    token, token_hash, expira = nuevo_token_recuperacion()
    ip = request.client.host if request.client else None
    await crear_token_recuperacion(usuario["id"], token_hash, expira, ip)

    enlace = str(request.base_url).rstrip("/") + f"/recuperar/{token}"
    enviado = await _enviar_correo_recuperacion(usuario["email"], enlace)
    if not enviado:
        # Sin SMTP configurado el enlace no llega a ningun lado. Se deja en el
        # log del servidor para no bloquear el desarrollo, y se avisa fuerte.
        log.warning("SMTP no disponible. Enlace de recuperacion para %s: %s",
                    usuario["email"], enlace)
    return respuesta_uniforme


async def _enviar_correo_recuperacion(destinatario: str, enlace: str) -> bool:
    from shared.email_sender import enviar_email
    cuerpo = f"""
    <p>Pediste restablecer la contraseña de tu cuenta en LicitaPro.</p>
    <p><a href="{enlace}">Elegir una contraseña nueva</a></p>
    <p>El enlace vence en una hora y solo se puede usar una vez.</p>
    <p style="color:#666;font-size:13px">Si no fuiste tú, ignora este correo:
       tu contraseña actual sigue funcionando.</p>
    """
    try:
        return await enviar_email(destinatario, "Restablecer tu contraseña", cuerpo)
    except Exception as e:
        log.error("Fallo al enviar el correo de recuperacion: %s", e, exc_info=True)
        return False


@router.get("/recuperar/{token}", response_class=HTMLResponse)
async def form_nueva_password(request: Request, token: str):
    from shared.db import usuario_por_token_recuperacion
    from shared.seguridad import hash_token

    usuario = await usuario_por_token_recuperacion(hash_token(token))
    if not usuario:
        return _plantillas(request).TemplateResponse(
            "recuperar.html",
            {"request": request, "paso": "invalido"}, status_code=400)

    return _plantillas(request).TemplateResponse(
        "recuperar.html",
        {"request": request, "paso": "cambiar", "token": token,
         "email": usuario["email"]})


@router.post("/recuperar/{token}", response_class=HTMLResponse)
async def cambiar_password(request: Request, token: str,
                           password: str = Form(...)):
    from shared.db import (
        consumir_token_y_cambiar_password, usuario_por_token_recuperacion,
    )
    from shared.seguridad import hash_token

    plantillas = _plantillas(request)
    th = hash_token(token)

    usuario = await usuario_por_token_recuperacion(th)
    if not usuario:
        return plantillas.TemplateResponse(
            "recuperar.html", {"request": request, "paso": "invalido"},
            status_code=400)

    motivo = password_debil(password)
    if motivo:
        return plantillas.TemplateResponse(
            "recuperar.html",
            {"request": request, "paso": "cambiar", "token": token,
             "email": usuario["email"], "error": motivo},
            status_code=400)

    if not await consumir_token_y_cambiar_password(th, hashear_password(password)):
        return plantillas.TemplateResponse(
            "recuperar.html", {"request": request, "paso": "invalido"},
            status_code=400)

    log.info("Contrasena restablecida para el usuario %s", usuario["id"])
    # No se inicia sesion automaticamente: si el enlace se filtro por el
    # historial del navegador o un proxy, abrirlo no debe dar acceso directo.
    # Se pide entrar con la contrasena nueva, que solo conoce quien la eligio.
    request.session.clear()
    return plantillas.TemplateResponse(
        "recuperar.html", {"request": request, "paso": "listo"})
