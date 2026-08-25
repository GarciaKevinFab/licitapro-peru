"""Panel web de LicitaPro -- FastAPI + Jinja2 + HTMX.

Deliberadamente sobrio: esto se abre todos los dias para trabajar, no para
impresionar. El tratamiento cinematografico vive en la landing; aqui manda la
densidad de informacion y la velocidad.
"""
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from shared.db import connection, licitaciones_para_usuario, get_usuario
from shared.config import DEPARTAMENTOS

BASE = Path(__file__).parent
app = FastAPI(title="LicitaPro Panel")
templates = Jinja2Templates(directory=str(BASE / "templates"))


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


# TODO(fase-3): reemplazar por la sesion autenticada. Hasta que exista login,
# el inquilino se elige por querystring y el panel NO debe exponerse fuera de
# localhost: cualquiera podria pasar ?usuario=N y ver otra cuenta.
async def _usuario_actual(usuario: int) -> tuple[int, str]:
    fila = await get_usuario(usuario)
    if fila:
        return fila["id"], (fila["nombre"] or fila["email"])
    async with connection() as conn:
        primero = await conn.fetchrow(
            "SELECT id, nombre, email FROM usuarios WHERE activo=TRUE ORDER BY id LIMIT 1")
    if not primero:
        return 0, "sin cuentas"
    return primero["id"], (primero["nombre"] or primero["email"])


@app.get("/", response_class=HTMLResponse)
async def panel(request: Request, q: str = "", region: str = "",
                score_min: int = 0, vigentes: int = 1, usuario: int = 0):
    uid, nombre = await _usuario_actual(usuario)
    async with connection() as conn:
        cuentas = await conn.fetch(
            "SELECT id, COALESCE(nombre, email) AS etiqueta FROM usuarios WHERE activo=TRUE ORDER BY id")
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "resumen": await _resumen(uid),
        "licitaciones": await _licitaciones(uid, q, region, score_min, bool(vigentes)),
        "departamentos": DEPARTAMENTOS,
        "cuentas": cuentas, "usuario_id": uid, "usuario_nombre": nombre,
        "q": q, "region": region, "score_min": score_min, "vigentes": vigentes,
    })


@app.get("/parts/tabla", response_class=HTMLResponse)
async def parte_tabla(request: Request, q: str = "", region: str = "",
                      score_min: int = 0, vigentes: int = 1, usuario: int = 0):
    """Fragmento que HTMX inyecta al filtrar, sin recargar la pagina."""
    uid, _ = await _usuario_actual(usuario)
    return templates.TemplateResponse("_tabla.html", {
        "request": request,
        "licitaciones": await _licitaciones(uid, q, region, score_min, bool(vigentes)),
    })


# Ejecutar: uvicorn web.app:app --reload --port 8200
