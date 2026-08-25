"""Empresas del usuario: alta, edicion y baja.

Sin esto una cuenta nueva no puede hacer nada: al pasar a multi-inquilino se
quitaron las empresas sembradas en schema.sql, asi que el usuario tiene que
poder cargar las suyas.

Toda ruta que recibe un id de empresa valida la propiedad con empresa_es_de
antes de tocar nada. Es el patron de scoping en el borde: el id llega del
formulario, o sea de fuera, y no se puede confiar en el.
"""
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import DEPARTAMENTOS
from shared.db import connection, empresa_es_de, empresas_de
from web.auth import usuario_actual

log = logging.getLogger("web.empresas")
router = APIRouter()

CAMPOS = (
    "razon_social", "ruc", "representante_legal", "dni_representante",
    "cargo_representante", "direccion", "departamento", "telefono", "email",
    "rnp_numero",
)


def _plantillas(request: Request):
    return request.app.state.templates


def _rubros(texto: str) -> list[str]:
    return [r.strip() for r in texto.replace("\n", ",").split(",") if r.strip()]


@router.get("/empresas", response_class=HTMLResponse)
async def listar(request: Request, aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/empresas", status_code=303)

    empresas = await empresas_de(usuario["id"])
    ids = [e["id"] for e in empresas] or [0]
    async with connection() as conn:
        # Cuantas propuestas tiene cada empresa: se avisa antes de desactivar
        # una que ya esta en uso.
        filas = await conn.fetch(
            """SELECT empresa_id, COUNT(*) AS n FROM propuestas
               WHERE empresa_id = ANY($1::int[]) GROUP BY empresa_id""",
            ids,
        )
    uso = {f["empresa_id"]: f["n"] for f in filas}

    return _plantillas(request).TemplateResponse("empresas.html", {
        "request": request, "usuario": usuario, "empresas": empresas,
        "uso": uso, "departamentos": DEPARTAMENTOS,
        "aviso": aviso, "error": error,
    })


@router.get("/empresas/nueva", response_class=HTMLResponse)
async def form_nueva(request: Request):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/empresas/nueva", status_code=303)
    return _plantillas(request).TemplateResponse("empresa_form.html", {
        "request": request, "usuario": usuario, "empresa": None,
        "departamentos": DEPARTAMENTOS,
    })


@router.get("/empresas/{empresa_id}/editar", response_class=HTMLResponse)
async def form_editar(request: Request, empresa_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)

    async with connection() as conn:
        empresa = await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", empresa_id)
    return _plantillas(request).TemplateResponse("empresa_form.html", {
        "request": request, "usuario": usuario, "empresa": empresa,
        "departamentos": DEPARTAMENTOS,
    })


@router.post("/empresas/guardar")
async def guardar(request: Request, empresa_id: int = Form(0), rubros: str = Form("")):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    formulario = await request.form()
    datos = {c: (formulario.get(c) or "").strip() or None for c in CAMPOS}
    if not datos["razon_social"]:
        return RedirectResponse("/empresas?error=La+razon+social+es+obligatoria",
                                status_code=303)

    lista_rubros = _rubros(rubros)

    async with connection() as conn:
        if empresa_id:
            if not await empresa_es_de(empresa_id, usuario["id"]):
                return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya",
                                        status_code=303)
            await conn.execute(
                """UPDATE empresas SET razon_social=$2, ruc=$3,
                       representante_legal=$4, dni_representante=$5,
                       cargo_representante=$6, direccion=$7, departamento=$8,
                       telefono=$9, email=$10, rnp_numero=$11, rubros=$12
                   WHERE id=$1""",
                empresa_id, *[datos[c] for c in CAMPOS], lista_rubros,
            )
            aviso = "Empresa+actualizada"
        else:
            # El RUC es unico en toda la tabla: si otro inquilino ya lo cargo, el
            # INSERT falla. Se avisa en vez de reventar con un 500.
            try:
                await conn.execute(
                    """INSERT INTO empresas (razon_social, ruc, representante_legal,
                           dni_representante, cargo_representante, direccion,
                           departamento, telefono, email, rnp_numero, rubros,
                           usuario_id, activa)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,TRUE)""",
                    *[datos[c] for c in CAMPOS], lista_rubros, usuario["id"],
                )
            except Exception as e:
                log.warning("Alta de empresa fallida: %s", e)
                return RedirectResponse(
                    "/empresas?error=No+se+pudo+crear.+Revisa+que+el+RUC+no+este+ya+registrado",
                    status_code=303)
            aviso = "Empresa+creada"

    return RedirectResponse(f"/empresas?aviso={aviso}", status_code=303)


@router.post("/empresas/{empresa_id}/desactivar")
async def desactivar(request: Request, empresa_id: int):
    """Se desactiva, no se borra: las propuestas y contratos que la referencian
    dejarian de tener sentido, y el historial hay que poder consultarlo."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)

    async with connection() as conn:
        await conn.execute("UPDATE empresas SET activa=FALSE WHERE id=$1", empresa_id)
    return RedirectResponse("/empresas?aviso=Empresa+desactivada", status_code=303)
