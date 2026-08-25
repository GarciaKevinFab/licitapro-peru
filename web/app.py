"""Panel web de LicitaPro -- FastAPI + Jinja2 + HTMX.

Deliberadamente sobrio: esto se abre todos los dias para trabajar, no para
impresionar. El tratamiento cinematografico vive en la landing; aqui manda la
densidad de informacion y la velocidad.
"""
import os
from pathlib import Path

from fastapi import FastAPI, Request
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

app.include_router(router_auth)
app.include_router(router_config)
app.include_router(router_empresas)
app.include_router(router_propuestas)
app.include_router(router_contratos)
app.include_router(router_suscripcion)


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
                        score_min: int = 0, solo_vigentes: bool = True) -> list[dict]:
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
    if estado == "suspendida":
        return {"clase": "urg", "texto": "suspendida"}
    return None


@app.get("/", response_class=HTMLResponse)
async def panel(request: Request, q: str = "", region: str = "",
                score_min: int = 0, vigentes: int = 1):
    # El inquilino sale de la sesion firmada, nunca de la peticion: antes se
    # elegia por querystring y cualquiera podia pasar ?usuario=N.
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/", status_code=303)
    uid = usuario["id"]
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "resumen": await _resumen(uid),
        "licitaciones": await _licitaciones(uid, q, region, score_min, bool(vigentes)),
        "departamentos": DEPARTAMENTOS,
        "usuario": usuario,
        "q": q, "region": region, "score_min": score_min, "vigentes": vigentes,
    })


@app.get("/parts/tabla", response_class=HTMLResponse)
async def parte_tabla(request: Request, q: str = "", region: str = "",
                      score_min: int = 0, vigentes: int = 1):
    """Fragmento que HTMX inyecta al filtrar, sin recargar la pagina."""
    usuario = await usuario_actual(request)
    if not usuario:
        return HTMLResponse('<div class="vacio"><p>Tu sesión expiró. '
                            '<a href="/entrar">Vuelve a entrar</a>.</p></div>',
                            status_code=401)
    return templates.TemplateResponse("_tabla.html", {
        "request": request,
        "licitaciones": await _licitaciones(usuario["id"], q, region, score_min, bool(vigentes)),
    })


@app.get("/salud")
async def salud():
    return {"estado": "ok"}


# Ejecutar: uvicorn web.app:app --reload --port 8200
