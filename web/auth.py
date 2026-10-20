"""Autenticacion del panel: registro, inicio de sesion y dependencia de usuario.

La sesion viaja en una cookie firmada (SessionMiddleware de Starlette). Guarda
el id del usuario y una huella corta de su contrasena: cualquier otro dato se
relee de la base, para que desactivar una cuenta surta efecto en la peticion
siguiente y no al expirar la cookie. La huella hace lo mismo con el cambio de
contrasena; ver `_huella`.
"""
import hashlib
import logging
import os
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.admin_cuentas import anotar_acceso
from shared.db import (
    anotar_intento_fallido,
    crear_usuario,
    get_usuario,
    get_usuario_por_email,
    intentos_recientes,
    limpiar_intentos,
)
from shared.seguridad import hashear_password, password_debil, verificar_password
from web.limites import ip_del_cliente

log = logging.getLogger("web.auth")
router = APIRouter()

# Mensaje unico para credenciales invalidas: distinguir "no existe" de
# "contrasena incorrecta" le regala al atacante la lista de correos registrados.
ERROR_CREDENCIALES = "Correo o contraseña incorrectos."
# Cuenta desactivada por el dueno del producto. Se muestra solo a quien
# acierta la contrasena; ver hacer_entrar.
ERROR_DESACTIVADA = "Cuenta desactivada. Escribe a soporte@sisac.pe."
# Alta con un correo que ya tiene cuenta. Generico a proposito: ver
# hacer_registro.
ERROR_REGISTRO = ("No pudimos crear la cuenta con ese correo. Si ya tienes una, "
                  "entra o restablece tu contraseña.")


# ─── Sesion revocable sin tabla de sesiones ──────────────
#
# EL PROBLEMA
#
#   La cookie firmada vale 12 horas pase lo que pase. Quien restablecia la
#   contrasena porque sospechaba que alguien habia entrado en su cuenta seguia
#   teniendo al intruso dentro hasta que la cookie caducara: cambiar la clave no
#   cerraba nada.
#
# POR QUE UNA HUELLA Y NO UNA TABLA
#
#   Una tabla de sesiones exige migracion, una consulta mas por peticion y
#   limpieza de filas viejas. La huella reutiliza la consulta que
#   `usuario_actual` ya hace: se guarda en la cookie un trozo del sha256 del
#   `password_hash` vigente y se compara en cada peticion. Cambia la
#   contrasena -> cambia el hash (bcrypt lleva sal nueva siempre, aunque la
#   clave se repita) -> ninguna cookie anterior casa y todas quedan fuera.
#
#   Va el sha256 del hash y no el hash: la cookie esta firmada pero NO cifrada,
#   cualquiera que la tenga lee su contenido. Un trozo de 16 hex de un sha256 no
#   sirve para atacar la contrasena; el hash bcrypt entero si.
#
#   La huella lleva delante el id del usuario. La suplantacion del admin
#   (web/admin.py) y el alta desde /comprar cambian `usuario_id` sin pasar por
#   iniciar_sesion; si la huella es de otro usuario, se toma por una sesion
#   recien abierta y no por una revocada. Ver `usuario_actual`.
_CLAVE_HUELLA = "huella"


def _huella(usuario_id: int, password_hash: str | None) -> str:
    resumen = hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]
    return f"{usuario_id}:{resumen}"


def iniciar_sesion(request: Request, fila) -> None:
    """Abre sesion para `fila` (necesita `id` y `password_hash`) desde cero.

    `clear()` primero: la cookie anterior pudo traer datos de otra cuenta o de
    un visitante anonimo, y heredar la sesion previa al login es la base de la
    fijacion de sesion. Despues, el id y la huella de la contrasena.
    """
    request.session.clear()
    request.session["usuario_id"] = fila["id"]
    request.session[_CLAVE_HUELLA] = _huella(fila["id"], fila["password_hash"])


async def usuario_actual(request: Request):
    """Usuario de la sesion, o None. Relee de la BD en cada peticion.

    Devuelve un dict SIN `password_hash`: este valor va directo al contexto de
    casi todas las plantillas, y el hash no tiene nada que hacer en un render.
    Quien necesite comprobar la contrasena usa `verificar_password_de`.
    """
    uid = request.session.get("usuario_id")
    if not uid:
        return None
    fila = await get_usuario(uid)
    if not fila:
        # La cuenta se desactivo o se borro mientras la sesion seguia viva.
        request.session.clear()
        return None

    vigente = _huella(fila["id"], fila["password_hash"])
    guardada = request.session.get(_CLAVE_HUELLA)
    if not isinstance(guardada, str) or not guardada.startswith(f"{fila['id']}:"):
        # Sesion sin huella de ESTE usuario: la abrio una ruta que no pasa por
        # iniciar_sesion (suplantacion del admin, alta en /comprar) o es una
        # cookie de antes de que existiera la huella. Se sella ahora; las de
        # antes caducan solas en 12 horas como mucho.
        request.session[_CLAVE_HUELLA] = vigente
    elif guardada != vigente:
        # La contrasena cambio despues de abrir esta sesion: queda revocada.
        log.info("Sesion revocada por cambio de contrasena del usuario %s",
                 fila["id"])
        request.session.clear()
        return None

    datos = dict(fila)
    datos.pop("password_hash", None)
    return datos


async def verificar_password_de(usuario_id: int, password: str) -> bool:
    """Comprueba la contrasena de un usuario sin que el hash pase por las vistas."""
    fila = await get_usuario(usuario_id)
    return bool(fila) and verificar_password(password, fila["password_hash"])


def _plantillas(request: Request):
    return request.app.state.templates


@router.get("/entrar", response_class=HTMLResponse)
async def form_entrar(request: Request, siguiente: str = "/panel", error: str = ""):
    """El `error` lo trae quien redirige hasta aqui, no un fallo de esta ruta.

    Lo usa el checkout publico: si el correo ya tiene cuenta, manda a entrar
    diciendo "ya tienes una cuenta, entra y terminamos la compra". Sin este
    parametro el usuario aterrizaba en un login mudo, sin saber por que le
    habian sacado a mitad del pago.
    """
    if await usuario_actual(request):
        # Por _destino_seguro y no en crudo: `siguiente` viene de la URL, y sin
        # filtrarlo esta redireccion acepta un destino absoluto. Es el mismo
        # filtro que ya se aplicaba al entrar; faltaba solo en este atajo.
        return RedirectResponse(_destino_seguro(siguiente), status_code=303)
    return _plantillas(request).TemplateResponse(
        "entrar.html", {"request": request, "modo": "entrar",
                        "siguiente": siguiente, "error": error})


# Cuantos fallos se admiten dentro de la ventana antes de frenar. 8 deja sitio
# de sobra a quien se equivoca de tecla o prueba una contrasena vieja, y corta
# la fuerza bruta mucho antes de que sea util: con bcrypt de por medio, 8
# intentos cada 15 minutos son unos 32 al dia contra una cuenta.
MAX_INTENTOS_ACCESO = 8
VENTANA_INTENTOS_MIN = 15
# La IP admite mas porque detras puede haber una oficina entera compartiendo
# salida. Aun asi corta el barrido de una contrasena comun contra muchas cuentas.
MAX_INTENTOS_POR_IP = 30


@router.post("/entrar", response_class=HTMLResponse)
async def hacer_entrar(request: Request, email: str = Form(...),
                       password: str = Form(...), siguiente: str = Form("/panel")):
    """Inicio de sesion con freno a la fuerza bruta.

    El freno se comprueba ANTES de mirar la contrasena. Comprobarlo despues
    dejaria que cada intento siguiera costando una verificacion bcrypt, que es
    justo el trabajo caro que un atacante quiere hacernos repetir.

    La respuesta al bloqueo es la MISMA que a una contrasena incorrecta, y es
    deliberado: decir "esta cuenta esta bloqueada" confirmaria que el correo
    existe, que es la mitad de lo que buscaba quien esta probando. El unico
    aviso distinto va al log, que solo vemos nosotros.
    """
    # La IP sale de cf-connecting-ip y no de request.client: ver
    # web/limites.ip_del_cliente. Con la de la conexion, un X-Forwarded-For
    # inventado saltaba este freno o bloqueaba la IP de otro.
    ip = ip_del_cliente(request.scope)

    def rechazar():
        return _plantillas(request).TemplateResponse(
            "entrar.html",
            {"request": request, "modo": "entrar", "siguiente": siguiente,
             "error": ERROR_CREDENCIALES, "email": email},
            status_code=401)

    por_email, por_ip = await intentos_recientes(email, ip, VENTANA_INTENTOS_MIN)
    if por_email >= MAX_INTENTOS_ACCESO or por_ip >= MAX_INTENTOS_POR_IP:
        log.warning("Acceso frenado: %r lleva %s fallos y la IP %s lleva %s",
                    email[:40], por_email, ip, por_ip)
        return rechazar()

    fila = await get_usuario_por_email(email, incluso_inactivo=True)
    if not fila or not verificar_password(password, fila["password_hash"]):
        await anotar_intento_fallido(email, ip)
        log.info("Intento de acceso fallido para %r", email[:40])
        return rechazar()

    if not fila["activo"]:
        # Solo se dice DESPUES de acertar la contrasena: asi no revela nada a
        # quien no la tiene, y a quien si la tiene le ahorra pedir una
        # recuperacion que no le devolveria el acceso.
        log.info("Acceso a cuenta desactivada: %s", fila["id"])
        return _plantillas(request).TemplateResponse(
            "entrar.html",
            {"request": request, "modo": "entrar", "siguiente": siguiente,
             "error": ERROR_DESACTIVADA, "email": email},
            status_code=403)

    # Entro bien: sus fallos previos no deben acercarle al bloqueo manana.
    await limpiar_intentos(email, ip)
    await anotar_acceso(fila["id"])
    iniciar_sesion(request, fila)
    return RedirectResponse(_destino_seguro(siguiente), status_code=303)


@router.get("/registro", response_class=HTMLResponse)
async def form_registro(request: Request):
    if await usuario_actual(request):
        return RedirectResponse("/panel", status_code=303)
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

    password_hash = hashear_password(password)
    fila = await crear_usuario(email, password_hash, nombre.strip() or None)
    if not fila:
        # Sin decir que el correo ya esta registrado: esa frase convertia el
        # alta en un comprobador de quien es cliente. El mensaje manda a entrar
        # o a recuperar, que le sirve igual al dueno legitimo y no le confirma
        # nada a quien esta probando correos.
        log.info("Alta rechazada: el correo ya tenia cuenta")
        return con_error(ERROR_REGISTRO)

    iniciar_sesion(request, {"id": fila["id"], "password_hash": password_hash})
    return RedirectResponse("/configuracion", status_code=303)


@router.post("/salir")
async def salir(request: Request):
    request.session.clear()
    return RedirectResponse("/entrar", status_code=303)


def _destino_seguro(destino: str) -> str:
    """Solo rutas internas: un destino absoluto permitiria usar el login como
    trampolin hacia un sitio ajeno tras autenticar.

    Mirar solo el principio no basta. `/\\evil.com` empieza por `/` y no por
    `//`, pero los navegadores tratan la barra invertida como una barra normal
    y lo resuelven como `//evil.com`: otro sitio. Lo mismo con tabuladores o
    saltos de linea entre las barras, que el navegador descarta antes de
    interpretar la URL. Por eso se rechaza cualquier barra invertida o caracter
    de control, y ademas urlparse no puede ver ni esquema ni host: una ruta, y
    nada mas que una ruta.
    """
    if not destino or not destino.startswith("/") or destino.startswith("//"):
        return "/"
    if "\\" in destino or any(ord(c) < 32 or ord(c) == 127 for c in destino):
        return "/"
    partes = urlparse(destino)
    if partes.scheme or partes.netloc:
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


def _es_desarrollo() -> bool:
    return os.getenv("LICITAPRO_ENTORNO", "dev") == "dev"


def _origen_para_enlaces(request: Request) -> str:
    """De donde cuelga el enlace del correo de recuperacion.

    NO de request.base_url fuera de desarrollo: ese valor sale de la cabecera
    Host, que la escribe quien hace la peticion. Pidiendo la recuperacion de la
    cuenta de otro con `Host: sitio-del-atacante`, el correo legitimo le llegaba
    a la victima con el token apuntando al servidor del atacante; un clic y el
    token era suyo. El origen publico configurado no lo controla nadie de fuera.

    En desarrollo si se usa: ahi el sitio es localhost y no hay victima.
    """
    if _es_desarrollo():
        return str(request.base_url).rstrip("/")
    from web.app import _base_url  # aqui y no arriba: web.app importa este modulo
    return _base_url()


@router.post("/recuperar", response_class=HTMLResponse)
async def pedir_recuperar(request: Request, tareas: BackgroundTasks,
                          email: str = Form(...)):
    """Responde SIEMPRE lo mismo y en el mismo tiempo.

    El texto ya era uniforme, pero el tiempo no: con cuenta se creaba el token
    y se hablaba con el servidor SMTP antes de responder (cientos de ms, o
    segundos); sin cuenta se respondia al instante. Midiendo la espera se sabia
    quien es cliente igual que si lo dijera el texto. Ahora todo el trabajo --
    buscar la cuenta incluido -- va en segundo plano, despues de responder.
    """
    tareas.add_task(_procesar_recuperacion, email,
                    ip_del_cliente(request.scope),
                    _origen_para_enlaces(request))
    return _plantillas(request).TemplateResponse(
        "recuperar.html",
        {"request": request, "paso": "pedir", "aviso": MENSAJE_ENVIADO})


async def _procesar_recuperacion(email: str, ip: str | None, origen: str) -> None:
    """Busca la cuenta, crea el token y manda el correo. Nunca lanza.

    Corre despues de responder: una excepcion aqui ya no le llega a nadie, asi
    que se registra y se acaba.
    """
    from shared.db import (
        crear_token_recuperacion,
        get_usuario_por_email,
        peticiones_recientes,
    )
    from shared.seguridad import MAX_PETICIONES_POR_HORA, nuevo_token_recuperacion

    try:
        usuario = await get_usuario_por_email(email)
        if not usuario:
            log.info("Recuperacion pedida para un correo sin cuenta")
            return

        if await peticiones_recientes(usuario["id"]) >= MAX_PETICIONES_POR_HORA:
            # Tampoco se avisa del limite: decirlo confirmaria que la cuenta existe.
            log.warning("Limite de recuperaciones alcanzado para el usuario %s",
                        usuario["id"])
            return

        token, token_hash, expira = nuevo_token_recuperacion()
        await crear_token_recuperacion(usuario["id"], token_hash, expira, ip)

        enlace = f"{origen}/recuperar/{token}"
        if await _enviar_correo_recuperacion(usuario["email"], enlace):
            return
        if _es_desarrollo():
            # En local sin SMTP es la unica forma de seguir el flujo.
            log.warning("SMTP no disponible. Enlace de recuperacion: %s", enlace)
        else:
            # Fuera de desarrollo el enlace NO va al log: es una llave de la
            # cuenta durante una hora, y los logs los leen mas ojos y viven mas
            # que eso. Basta con saber que fallo y para quien.
            log.error("No se pudo enviar el correo de recuperacion al usuario %s",
                      usuario["id"])
    except Exception:
        log.exception("Fallo al procesar una peticion de recuperacion")


async def _enviar_correo_recuperacion(destinatario: str, enlace: str) -> bool:
    from shared import plantillas_correo
    from shared.email_sender import enviar_email
    texto, cuerpo = plantillas_correo.componer(
        titulo="Restablece tu contraseña",
        preencabezado="Enlace para elegir una contraseña nueva. "
                      "Vence en una hora.",
        intro=["Pediste restablecer la contraseña de tu cuenta en LicitaPro."],
        boton={"texto": "Elegir una contraseña nueva", "url": enlace},
        cierre=["Si el botón no funciona, copia este enlace en tu navegador:",
                enlace],
        aviso="El enlace vence en una hora y solo se puede usar una vez. Si no "
              "fuiste tú, ignora este correo: tu contraseña actual sigue "
              "funcionando.",
    )
    try:
        return await enviar_email(destinatario, "Restablecer tu contraseña",
                                  cuerpo, texto)
    except Exception:
        log.exception("Fallo al enviar el correo de recuperacion")
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
        consumir_token_y_cambiar_password,
        usuario_por_token_recuperacion,
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
