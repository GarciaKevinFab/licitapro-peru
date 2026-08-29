"""Panel web de LicitaPro -- FastAPI + Jinja2 + HTMX.

Deliberadamente sobrio: esto se abre todos los dias para trabajar, no para
impresionar. El tratamiento cinematografico vive en la landing; aqui manda la
densidad de informacion y la velocidad.
"""
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from shared.db import connection, licitaciones_para_usuario
from shared.config import DEPARTAMENTOS
from shared.seguridad import clave_sesion

BASE = Path(__file__).parent
app = FastAPI(title="LicitaPro Panel")
templates = Jinja2Templates(directory=str(BASE / "templates"))
# Los routers alcanzan las plantillas por app.state y no importando este modulo,
# que los importa a ellos: al reves habria ciclo.
app.state.templates = templates

# La guarda de suscripcion se registra ANTES que SessionMiddleware: en
# Starlette el ultimo en registrarse envuelve a los anteriores, asi que
# este queda por dentro y puede leer request.session. Registrado despues,
# veia la sesion vacia y dejaba pasar a todo el mundo.
@app.middleware("http")
async def exigir_suscripcion(request: Request, call_next):
    """Corta el producto cuando la suscripción muere; deja pagar siempre.

    Sin esto la prueba gratuita no termina nunca: los 14 días se cumplen y el
    usuario sigue usando todo. Un producto que no corta al vencer no es una
    prueba, es producto gratis.

    Se comprueba aquí y no ruta por ruta porque olvidarse de una sola sería
    dejar la puerta abierta, y las rutas van a seguir creciendo.
    """
    from shared.suscripciones import estado_suscripcion, ruta_libre

    camino = request.url.path
    if ruta_libre(camino):
        return await call_next(request)

    uid = request.session.get("usuario_id")
    if not uid:
        # Sin sesión no hay nada que cobrar: que lo resuelva la propia ruta,
        # que sabe a dónde devolver al usuario después de entrar.
        return await call_next(request)

    susc = await estado_suscripcion(uid)
    # El aviso viaja en request.state y no en el contexto de cada ruta: si
    # hubiera que acordarse de pasarlo en cada una, faltaria en la mitad.
    request.state.susc_aviso = _aviso_desde_estado(susc)
    if susc.get("acceso"):
        return await call_next(request)

    # Suspendida: se manda a pagar. Sus datos siguen intactos y vuelven en
    # cuanto el cobro entre.
    if request.headers.get("hx-request"):
        # Petición de HTMX: devolver una redirección haría que se inyectara el
        # login dentro de un fragmento. Mejor un mensaje que se vea.
        return HTMLResponse(
            '<div class="vacio"><p><strong>Tu suscripción está suspendida.</strong></p>'
            '<p><a href="/suscripcion">Actívala para seguir viendo tus licitaciones</a>.</p></div>',
            status_code=402)
    return RedirectResponse("/suscripcion?error=Tu+suscripcion+esta+suspendida", status_code=303)


# ─── Cabeceras de seguridad ──────────────────────────────
# Va DESPUES de todo lo demas a proposito: en Starlette el ultimo registrado
# envuelve a los anteriores, asi que este queda por fuera y sus cabeceras
# acompanan tambien a las respuestas de error y a las redirecciones.
#
# script-src NO admite 'unsafe-inline': con eso puesto, un XSS que consiga
# inyectar un <script> no llega a ejecutarse. Se pudo cerrar al sacar del HTML
# los manejadores on*= y el bloque <script> de la portada.
#
# style-src tampoco lo admite ya. Ahi hubo que resolver dos cosas distintas:
#
#   - Los 44 atributos style= repartidos por las plantillas. Esos se borran y
#     punto: pasan a clases. Un nonce NO sirve para ellos -- solo vale para
#     elementos <style>, no para atributos.
#   - Los bloques <style> que cada plantilla inyecta por {% block estilos %}.
#     Esos no se pueden sacar a un archivo sin deshacer esa estructura, y su
#     contenido cambia por pagina, asi que un hash tampoco vale. Por eso llevan
#     un NONCE distinto en cada peticion: el navegador ejecuta el <style> que
#     trae el nonce del dia y rechaza cualquier otro que alguien inyecte.
#
# El nonce se genera por peticion y NUNCA se reutiliza: un nonce fijo es lo
# mismo que 'unsafe-inline' con pasos extra, porque el atacante puede leerlo
# del HTML y ponerselo a su propia etiqueta.
def _csp(nonce: str) -> str:
    return "; ".join([
        "default-src 'self'",
        "script-src 'self' https://unpkg.com",
        f"style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ])


@app.middleware("http")
async def cabeceras_seguridad(request: Request, call_next):
    # Se genera antes de llamar a la vista: la plantilla tiene que poder
    # ponerlo en sus <style>, y la cabecera tiene que llevar el mismo.
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    respuesta = await call_next(request)
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["X-Frame-Options"] = "DENY"
    respuesta.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # La app no usa camara, microfono ni ubicacion: negarlos evita que un script
    # inyectado los pida en nuestro nombre.
    respuesta.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=()")
    respuesta.headers["Content-Security-Policy"] = _csp(nonce)

    # NADA DE HTML SE CACHEA. Los dos motivos son independientes y cada uno
    # basta por si solo.
    #
    #   1. El panel es por sesion. Si un intermediario guarda /panel y se lo
    #      sirve a otro, un cliente ve las licitaciones, las propuestas y los
    #      contratos de otro. Hoy no ocurre porque Cloudflare no cachea HTML
    #      por defecto, pero eso es configuracion ajena a este repositorio: una
    #      regla de "Cache Everything" puesta con buena intencion convierte el
    #      producto en una fuga. La aplicacion tiene que defenderse sola.
    #
    #   2. TODAS las plantillas llevan un nonce de CSP distinto en cada
    #      peticion, la portada incluida. Un cuerpo guardado con el nonce de
    #      ayer, servido junto a la cabecera de hoy, no casa: el navegador
    #      rechaza cada bloque <style> y la pagina sale sin estilos.
    #
    # /static queda fuera: son archivos sin nonce y sin datos de nadie, y ahi
    # el cache si vale la pena.
    if not request.url.path.startswith("/static"):
        respuesta.headers["Cache-Control"] = "private, no-store"
    if os.getenv("LICITAPRO_ENTORNO", "dev") != "dev":
        # Solo fuera de desarrollo: en local no hay TLS, y mandar HSTS desde
        # localhost deja el navegador del desarrollador forzando https contra
        # un puerto que no lo habla. Se arregla borrando el estado del
        # navegador, que es una tarde perdida por una cabecera de mas.
        respuesta.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains")
    return respuesta


# Cookie de sesion firmada. https_only se activa fuera de desarrollo; en local
# forzarlo impediria entrar, porque no hay TLS.
app.add_middleware(
    SessionMiddleware,
    secret_key=clave_sesion(),
    session_cookie="licitapro_sesion",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=os.getenv("LICITAPRO_ENTORNO", "dev") != "dev",
)

from web.auth import router as router_auth, usuario_actual  # noqa: E402
from web.configuracion import router as router_config  # noqa: E402
from web.empresas import router as router_empresas  # noqa: E402
from web.propuestas import router as router_propuestas  # noqa: E402
from web.contratos import router as router_contratos  # noqa: E402
from web.suscripcion import router as router_suscripcion  # noqa: E402
from web.webhooks_whatsapp import router as router_wa_webhook
from web.admin import router as router_admin  # noqa: E402
from web.informes import router as router_informes  # noqa: E402

# Los scripts salen de aqui y no del HTML. Es lo que permite que la politica
# de seguridad prohiba el script embebido, y sin eso la CSP no protege contra
# XSS por mucho que este puesta.
app.mount("/static", StaticFiles(directory="web/static"), name="static")

app.include_router(router_auth)
app.include_router(router_config)
app.include_router(router_empresas)
app.include_router(router_propuestas)
app.include_router(router_contratos)
app.include_router(router_suscripcion)
app.include_router(router_wa_webhook)
app.include_router(router_admin)
app.include_router(router_informes)


def _dias(fecha) -> int | None:
    if not fecha:
        return None
    from datetime import datetime
    return (fecha - datetime.now()).days


async def _resumen(usuario_id: int) -> dict:
    """Resumen del usuario: cuenta sobre SU seleccion, no sobre el pozo entero.

    El pozo de licitaciones es compartido -- son datos publicos -- pero el
    numero que le importa a cada cuenta es cuantas le corresponden a ella.
    """
    from datetime import datetime, timedelta
    suyas = await licitaciones_para_usuario(usuario_id, limite=1000, solo_vigentes=False)
    ahora = datetime.now()
    vigentes = [l for l in suyas if l["fecha_cierre"] and l["fecha_cierre"] > ahora]
    urgentes = [l for l in vigentes if l["fecha_cierre"] < ahora + timedelta(days=3)]
    fila = {"total": len(suyas), "vigentes": len(vigentes), "urgentes": len(urgentes)}
    async with connection() as conn:
        historico = await conn.fetchval("SELECT COUNT(*) FROM historico_precios")
        ultimo = await conn.fetchrow(
            """SELECT fuente, fin, registros_nuevos FROM scraping_log
               WHERE status='done' ORDER BY fin DESC NULLS LAST LIMIT 1"""
        )
    return {
        "total": fila["total"],
        "vigentes": fila["vigentes"],
        "urgentes": fila["urgentes"],
        "historico": historico,
        "ultimo": dict(ultimo) if ultimo else None,
    }


async def _licitaciones(usuario_id: int, q: str = "", region: str = "",
                        score_min: int = 0, solo_vigentes: bool = True,
                        con_banderas: bool = False) -> list[dict]:
    """Filtros de la interfaz aplicados SOBRE el conjunto ya scopeado.

    El scoping por inquilino ocurre primero y una sola vez, en
    licitaciones_para_usuario. Asi ningun filtro de la UI puede ampliar el
    conjunto mas alla de lo que le corresponde a la cuenta.
    """
    from shared.config import normalizar
    filas = await licitaciones_para_usuario(usuario_id, limite=500,
                                            solo_vigentes=solo_vigentes)
    if q:
        aguja = normalizar(q)
        filas = [f for f in filas
                 if aguja in normalizar(f["objeto"] or "")
                 or aguja in normalizar(f["entidad"] or "")]
    if region:
        filas = [f for f in filas if f["departamento"] == region]
    if score_min:
        filas = [f for f in filas
                 if (f["score_viabilidad"] or 0) >= float(score_min)]
    if con_banderas:
        # "Solo con indicios" es lo que hace util la funcion: sin este filtro
        # habria que abrir los procesos uno a uno, que es justo lo que las
        # banderas pretendian evitar.
        filas = [f for f in filas if (f.get("banderas_nivel") or 0) > 0]
    filas = filas[:200]

    salida = []
    for f in filas:
        d = dict(f)
        d["dias"] = _dias(d["fecha_cierre"])
        detalle = d.get("score_detalle")
        if isinstance(detalle, str):
            import json
            try:
                detalle = json.loads(detalle)
            except ValueError:
                detalle = None
        d["score_detalle"] = detalle or {}
        salida.append(d)
    return salida


def _aviso_desde_estado(s: dict) -> dict | None:
    """Aviso de estado de la suscripción para la barra superior.

    Una prueba que vence sin que el usuario se entere no convierte a pago: se
    entera el día que deja de funcionar, y ese día ya está molesto. Por eso el
    aviso aparece desde que quedan pocos días, no cuando ya es tarde.
    """
    estado, dias = s.get("estado_efectivo"), s.get("dias_restantes")

    if estado == "prueba":
        if dias is None:
            return None
        if dias <= 0:
            return {"clase": "urg", "texto": "prueba terminada"}
        if dias <= 5:
            return {"clase": "urg", "texto": f"prueba: {dias} día{'s' if dias != 1 else ''}"}
        return {"clase": "", "texto": f"prueba: {dias} días"}
    if estado == "vencida":
        return {"clase": "urg", "texto": "pago pendiente"}
    if s.get("degradado"):
        # No dice "suspendida" porque no lo esta: sigue teniendo el panel. Decir
        # lo que perdio -- los avisos -- es lo que da motivo para volver a pagar;
        # "suspendida" solo suena a puerta cerrada y a error nuestro.
        return {"clase": "urg", "texto": "plan gratis · sin avisos"}
    if estado == "suspendida":
        return {"clase": "urg", "texto": "suspendida"}
    return None


@app.get("/", response_class=HTMLResponse)
async def portada(request: Request):
    """Pagina publica para quien todavia no tiene cuenta.

    La plantilla existia y no la servia ninguna ruta: quien llegaba al sitio
    era redirigido a /entrar sin ver que hace el producto ni cuanto cuesta.
    Pedirle a alguien que se registre antes de contarle nada es la forma mas
    rapida de que cierre la pestana.

    Quien ya tiene sesion va derecho a su panel: para el la portada es un paso
    de mas.
    """
    if await usuario_actual(request):
        return RedirectResponse("/panel", status_code=303)
    return templates.TemplateResponse("landing.html", {
        "request": request,
        # Los planes se leen de la base y no se repiten a mano en la plantilla:
        # estaban escritos a fuego y dejaron de coincidir en cuanto se anadio el
        # plan gratuito. Una pagina publica que promete otra cosa que el sistema
        # es peor que no tenerla.
        "planes": await _planes_publicos(),
    })


_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def _hoy_en_letras() -> str:
    from datetime import date
    h = date.today()
    return f"{h.day} de {_MESES[h.month - 1]} de {h.year}"


@app.get("/privacidad", response_class=HTMLResponse)
async def privacidad(request: Request):
    """Politica de privacidad. Publica: hay que poder leerla ANTES de registrarse.

    Va tambien en RUTAS_LIBRES: quien tiene la suscripcion suspendida conserva
    el derecho a leer que hacemos con sus datos y a pedir que los borremos.
    Condicionar eso a estar al dia con el pago seria lo contrario de lo que
    exige la Ley 29733.
    """
    return templates.TemplateResponse("privacidad.html", {
        "request": request,
        "usuario": await usuario_actual(request),
        "hoy": _hoy_en_letras(),
    })


@app.get("/terminos", response_class=HTMLResponse)
async def terminos(request: Request):
    return templates.TemplateResponse("terminos.html", {
        "request": request,
        "usuario": await usuario_actual(request),
        "hoy": _hoy_en_letras(),
    })


async def _primeros_pasos(usuario_id: int) -> dict | None:
    """Los tres pasos que hacen util la cuenta, y cuales lleva hechos.

    Una cuenta recien creada ve el pozo entero sin que nadie le explique nada, y
    el primer minuto es el que decide si vuelve. El problema no es que falte
    informacion: es que sin empresa cargada no puede postular a nada, y sin
    filtros el panel le muestra seis mil licitaciones que no le interesan.

    Se calcula cada vez en vez de guardar una marca de "ya vio el tutorial":
    asi el aviso desaparece solo cuando de verdad completo los pasos, y vuelve
    si algun dia se queda sin empresas. Una marca de vista miente en cuanto el
    estado cambia.

    Devuelve None cuando esta todo hecho, para que la plantilla no tenga que
    decidir si vale la pena pintar el bloque.
    """
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT
                 (SELECT COUNT(*) FROM empresas
                   WHERE usuario_id = $1 AND activa = TRUE) AS empresas,
                 (SELECT COALESCE(array_length(keywords, 1), 0) FROM user_config
                   WHERE usuario_id = $1) AS keywords,
                 (SELECT (telegram_chat_id IS NOT NULL
                          OR whatsapp_estado = 'activo') FROM usuarios
                   WHERE id = $1) AS canal""",
            usuario_id)

    pasos = [
        {"hecho": bool(fila["empresas"]),
         "titulo": "Carga tu empresa",
         "detalle": "Con su RUC, representante legal y rubros. Sin esto no puedes "
                    "postular ni generar documentos.",
         "enlace": "/empresas/nueva", "accion": "Cargar empresa"},
        {"hecho": bool(fila["keywords"]),
         "titulo": "Dinos a qué te dedicas",
         "detalle": "Unas palabras clave y tus regiones. Ahora mismo te estamos "
                    "mostrando todo lo que publica el Estado, que es demasiado.",
         "enlace": "/configuracion", "accion": "Ajustar filtros"},
        {"hecho": bool(fila["canal"]),
         "titulo": "Elige por dónde te avisamos",
         "detalle": "WhatsApp o Telegram. Es lo que convierte esto en un radar: "
                    "si no, tienes que acordarte de entrar.",
         "enlace": "/configuracion", "accion": "Conectar un canal"},
    ]
    if all(p["hecho"] for p in pasos):
        return None
    return {"pasos": pasos, "hechos": sum(p["hecho"] for p in pasos),
            "total": len(pasos)}


async def _planes_publicos() -> list[dict]:
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT codigo, nombre, precio_mensual, precio_anual,
                      max_empresas, max_regiones, analisis_ia, alertas
                 FROM planes WHERE activo = TRUE ORDER BY orden""")
    return [dict(f) for f in filas]


@app.get("/panel", response_class=HTMLResponse)
async def panel(request: Request, q: str = "", region: str = "",
                score_min: int = 0, vigentes: int = 1,
                banderas: int = 0):
    # El inquilino sale de la sesion firmada, nunca de la peticion: antes se
    # elegia por querystring y cualquiera podia pasar ?usuario=N.
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/panel", status_code=303)
    uid = usuario["id"]
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "resumen": await _resumen(uid),
        "licitaciones": await _licitaciones(uid, q, region, score_min, bool(vigentes),
                                            bool(banderas)),
        "departamentos": DEPARTAMENTOS,
        "usuario": usuario,
        "q": q, "region": region, "score_min": score_min, "vigentes": vigentes,
        "banderas": banderas,
        "primeros_pasos": await _primeros_pasos(uid),
    })


@app.get("/parts/tabla", response_class=HTMLResponse)
async def parte_tabla(request: Request, q: str = "", region: str = "",
                      score_min: int = 0, vigentes: int = 1,
                      banderas: int = 0):
    """Fragmento que HTMX inyecta al filtrar, sin recargar la pagina."""
    usuario = await usuario_actual(request)
    if not usuario:
        return HTMLResponse('<div class="vacio"><p>Tu sesión expiró. '
                            '<a href="/entrar">Vuelve a entrar</a>.</p></div>',
                            status_code=401)
    return templates.TemplateResponse("_tabla.html", {
        "request": request,
        "licitaciones": await _licitaciones(usuario["id"], q, region, score_min,
                                            bool(vigentes), bool(banderas)),
    })


@app.get("/salud")
async def salud():
    return {"estado": "ok"}


# Ejecutar: uvicorn web.app:app --reload --port 8200
