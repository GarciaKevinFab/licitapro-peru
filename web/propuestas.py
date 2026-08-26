"""Flujo de preparacion: postular, responder preguntas y armar el expediente.

Esto vivia solo en Telegram (/estado, /r, /aprobar). Un chat esta bien para
avisar, pero es pesimo para formularios: no se puede revisar lo escrito, no hay
validacion y el historial se pierde entre mensajes. Aqui el mismo flujo se
maneja con formularios de verdad.

La propiedad se valida SIEMPRE por la empresa de la propuesta, con un JOIN
contra empresas.usuario_id: una propuesta es del usuario si su empresa lo es.
No se guarda usuario_id en propuestas a proposito, para que no puedan discrepar.
"""
import logging
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from shared.banderas import describir
from shared.db import connection, empresa_es_de, empresas_de, responder_pregunta
from web.auth import usuario_actual

log = logging.getLogger("web.propuestas")
router = APIRouter()


def _plantillas(request: Request):
    return request.app.state.templates


async def _propuesta_del_usuario(propuesta_id: int, usuario_id: int):
    """Propuesta + licitacion + empresa, solo si le pertenece al usuario."""
    async with connection() as conn:
        return await conn.fetchrow(
            """SELECT p.*, e.razon_social, e.id AS emp_id,
                      l.objeto, l.entidad, l.fecha_cierre, l.monto_referencial,
                      l.tipo, l.departamento, l.bases_urls, l.url AS lic_url,
                      l.score_viabilidad
                 FROM propuestas p
                 JOIN empresas e ON e.id = p.empresa_id
                 LEFT JOIN licitaciones l ON l.id = p.licitacion_id
                WHERE p.id = $1 AND e.usuario_id = $2""",
            propuesta_id, usuario_id,
        )


# ─── Detalle de licitacion y alta de propuesta ───────────

@router.get("/licitacion/{licitacion_id}", response_class=HTMLResponse)
async def detalle_licitacion(request: Request, licitacion_id: str, error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse(f"/entrar?siguiente=/licitacion/{licitacion_id}",
                                status_code=303)

    async with connection() as conn:
        lic = await conn.fetchrow("SELECT * FROM licitaciones WHERE id=$1", licitacion_id)
        if not lic:
            return RedirectResponse("/", status_code=303)
        mias = await conn.fetch(
            """SELECT p.id, p.estado, e.razon_social
                 FROM propuestas p JOIN empresas e ON e.id = p.empresa_id
                WHERE p.licitacion_id = $1 AND e.usuario_id = $2""",
            licitacion_id, usuario["id"])

    detalle = lic["score_detalle"]
    if isinstance(detalle, str):
        import json
        try:
            detalle = json.loads(detalle)
        except ValueError:
            detalle = None

    return _plantillas(request).TemplateResponse("licitacion.html", {
        # Se describen aqui y no en la plantilla: el texto de cada bandera
        # vive en un solo sitio, junto a la regla que la produce.
        "banderas": [describir(c) for c in (lic["banderas"] or [])],
        "request": request, "usuario": usuario, "lic": lic,
        "score_detalle": detalle or {}, "propuestas": mias,
        "empresas": await empresas_de(usuario["id"]), "error": error,
    })


@router.post("/postular")
async def postular(request: Request, licitacion_id: str = Form(...),
                   empresa_id: int = Form(...)):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse(f"/licitacion/{licitacion_id}?error=Esa+empresa+no+es+tuya",
                                status_code=303)

    async with connection() as conn:
        ya = await conn.fetchval(
            "SELECT id FROM propuestas WHERE licitacion_id=$1 AND empresa_id=$2",
            licitacion_id, empresa_id)
        if ya:
            return RedirectResponse(f"/propuestas/{ya}", status_code=303)
        propuesta_id = await conn.fetchval(
            """INSERT INTO propuestas (licitacion_id, empresa_id, estado)
               VALUES ($1, $2, 'iniciado') RETURNING id""",
            licitacion_id, empresa_id)

    # Las preguntas se generan al abrir la propuesta: asi el usuario ve de
    # entrada que le falta, en vez de descubrirlo al generar el expediente.
    try:
        from prep_bot.questioner import generar_preguntas_propuesta
        await generar_preguntas_propuesta(propuesta_id, empresa_id)
    except Exception as e:
        log.error("No se pudieron generar preguntas para %s: %s", propuesta_id, e,
                  exc_info=True)

    return RedirectResponse(f"/propuestas/{propuesta_id}", status_code=303)


# ─── Propuestas ──────────────────────────────────────────

@router.get("/propuestas", response_class=HTMLResponse)
async def listar(request: Request, aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/propuestas", status_code=303)

    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT p.*, e.razon_social, l.objeto, l.entidad, l.fecha_cierre,
                      l.monto_referencial,
                      (SELECT COUNT(*) FROM preguntas q
                        WHERE q.propuesta_id = p.id AND q.respondida = FALSE) AS pendientes
                 FROM propuestas p
                 JOIN empresas e ON e.id = p.empresa_id
                 LEFT JOIN licitaciones l ON l.id = p.licitacion_id
                WHERE e.usuario_id = $1
                ORDER BY l.fecha_cierre ASC NULLS LAST, p.created_at DESC""",
            usuario["id"])

    return _plantillas(request).TemplateResponse("propuestas.html", {
        "request": request, "usuario": usuario, "propuestas": filas,
        "aviso": aviso, "error": error,
    })


@router.get("/propuestas/{propuesta_id}", response_class=HTMLResponse)
async def detalle(request: Request, propuesta_id: int, aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    prop = await _propuesta_del_usuario(propuesta_id, usuario["id"])
    if not prop:
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)

    async with connection() as conn:
        preguntas = await conn.fetch(
            """SELECT * FROM preguntas WHERE propuesta_id=$1
               ORDER BY respondida, id""", propuesta_id)

    return _plantillas(request).TemplateResponse("propuesta.html", {
        "request": request, "usuario": usuario, "p": prop,
        "preguntas": preguntas,
        "pendientes": [q for q in preguntas if not q["respondida"]],
        "aviso": aviso, "error": error,
    })


@router.post("/propuestas/{propuesta_id}/responder")
async def responder(request: Request, propuesta_id: int,
                    pregunta_id: int = Form(...), respuesta: str = Form(...)):
    """Guarda la respuesta y la aprende: la knowledge base evita repreguntar."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _propuesta_del_usuario(propuesta_id, usuario["id"]):
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)

    async with connection() as conn:
        # La pregunta tiene que ser de ESTA propuesta: el id viene del formulario.
        pertenece = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM preguntas WHERE id=$1 AND propuesta_id=$2)",
            pregunta_id, propuesta_id)
    if not pertenece:
        return RedirectResponse(f"/propuestas/{propuesta_id}?error=Pregunta+no+valida",
                                status_code=303)
    if not respuesta.strip():
        return RedirectResponse(f"/propuestas/{propuesta_id}?error=La+respuesta+esta+vacia",
                                status_code=303)

    await responder_pregunta(pregunta_id, respuesta.strip())
    return RedirectResponse(f"/propuestas/{propuesta_id}?aviso=Respuesta+guardada",
                            status_code=303)


@router.post("/propuestas/{propuesta_id}/generar")
async def generar(request: Request, propuesta_id: int):
    """Autocompleta los documentos con lo que ya sabemos de la empresa."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    prop = await _propuesta_del_usuario(propuesta_id, usuario["id"])
    if not prop:
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)

    try:
        from prep_bot.autofill.engine import autofill_propuesta
        await autofill_propuesta(propuesta_id, prop["emp_id"])
    except Exception as e:
        log.error("Autofill fallo en %s: %s", propuesta_id, e, exc_info=True)
        return RedirectResponse(
            f"/propuestas/{propuesta_id}?error=No+se+pudieron+generar+los+documentos",
            status_code=303)

    return RedirectResponse(f"/propuestas/{propuesta_id}?aviso=Documentos+generados",
                            status_code=303)


@router.post("/propuestas/{propuesta_id}/expediente")
async def armar_expediente(request: Request, propuesta_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _propuesta_del_usuario(propuesta_id, usuario["id"]):
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)

    try:
        from prep_bot.zip_builder import generar_expediente_zip
        ruta = await generar_expediente_zip(propuesta_id)
    except Exception as e:
        log.error("ZIP fallo en %s: %s", propuesta_id, e, exc_info=True)
        ruta = None

    if not ruta:
        return RedirectResponse(
            f"/propuestas/{propuesta_id}?error=No+se+pudo+armar+el+expediente",
            status_code=303)
    return RedirectResponse(f"/propuestas/{propuesta_id}?aviso=Expediente+listo",
                            status_code=303)


@router.get("/propuestas/{propuesta_id}/descargar")
async def descargar(request: Request, propuesta_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    prop = await _propuesta_del_usuario(propuesta_id, usuario["id"])
    if not prop or not prop["expediente_zip_path"]:
        return RedirectResponse(f"/propuestas/{propuesta_id}?error=Todavia+no+hay+expediente",
                                status_code=303)

    ruta = prop["expediente_zip_path"]
    if not os.path.isfile(ruta):
        return RedirectResponse(
            f"/propuestas/{propuesta_id}?error=El+archivo+ya+no+esta+en+el+servidor",
            status_code=303)
    return FileResponse(ruta, filename=os.path.basename(ruta),
                        media_type="application/zip")


@router.post("/propuestas/{propuesta_id}/precio")
async def fijar_precio(request: Request, propuesta_id: int, precio: float = Form(...)):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _propuesta_del_usuario(propuesta_id, usuario["id"]):
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)
    async with connection() as conn:
        await conn.execute(
            "UPDATE propuestas SET precio_ofertado=$2, updated_at=NOW() WHERE id=$1",
            propuesta_id, precio)
    return RedirectResponse(f"/propuestas/{propuesta_id}?aviso=Precio+guardado",
                            status_code=303)
