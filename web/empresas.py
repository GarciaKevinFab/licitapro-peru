"""Empresas del usuario: alta, edicion y baja.

Sin esto una cuenta nueva no puede hacer nada: al pasar a multi-inquilino se
quitaron las empresas sembradas en schema.sql, asi que el usuario tiene que
poder cargar las suyas.

Toda ruta que recibe un id de empresa valida la propiedad con empresa_es_de
antes de tocar nada. Es el patron de scoping en el borde: el id llega del
formulario, o sea de fuera, y no se puede confiar en el.
"""
import logging

from urllib.parse import quote_plus

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse,
)

from datetime import date

from shared.config import DEPARTAMENTOS, normalizar, parse_monto
from shared.db import connection, empresa_es_de, empresas_de
from shared.archivos import (
    ArchivoInvalido, TIPOS as TIPOS_IMAGEN, borrar_imagen, guardar_imagen, rutas_de,
)
from shared.suscripciones import puede_agregar_empresa
from web.auth import usuario_actual

log = logging.getLogger("web.empresas")
router = APIRouter()

CAMPOS = (
    "razon_social", "ruc", "representante_legal", "dni_representante",
    "cargo_representante", "direccion", "departamento", "telefono", "email",
    "rnp_numero", "rnp_categoria",
)

# `rnp_vigencia` NO va en CAMPOS porque es DATE y todo lo de arriba es texto.
# Llevaba en el esquema desde el principio sin que nadie pudiera escribirla: el
# formulario tenia el numero de RNP y no su fecha de caducidad. Y mientras
# tanto el analisis de viabilidad afirma que la inscripcion vigente es
# condicion para contratar.


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
async def form_editar(request: Request, empresa_id: int,
                      aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)

    async with connection() as conn:
        empresa = await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", empresa_id)
        # Las dos tablas que el producto leia sin que nadie pudiera rellenarlas.
        experiencia = await conn.fetch(
            """SELECT * FROM experiencia WHERE empresa_id=$1
                ORDER BY fecha_fin DESC NULLS LAST, monto DESC NULLS LAST""",
            empresa_id)
        equipo = await conn.fetch(
            """SELECT * FROM equipo_tecnico WHERE empresa_id=$1
                ORDER BY anos_experiencia DESC NULLS LAST, nombre_completo""",
            empresa_id)
        vencimientos = await conn.fetch(
            """SELECT * FROM vencimientos WHERE empresa_id=$1
                ORDER BY fecha_vencimiento""", empresa_id)

    return _plantillas(request).TemplateResponse("empresa_form.html", {
        "request": request, "usuario": usuario, "empresa": empresa,
        "departamentos": DEPARTAMENTOS,
        "imagenes": await rutas_de(empresa_id),
        "tipos_imagen": TIPOS_IMAGEN,
        "experiencia": experiencia, "equipo": equipo,
        "vencimientos": vencimientos, "hoy": date.today(),
        "aviso": aviso, "error": error,
    })


# ─── Experiencia del postor y equipo tecnico ─────────────
#
# Estas dos tablas se leian en cuatro sitios -- el contexto de empresa del
# analisis con IA, `03_Experiencia_Postor.docx` del expediente, los avisos del
# validador y la tabla de equipo de la propuesta tecnica -- y NINGUN formulario
# ni comando escribia en ellas. El resultado, igual para todos los clientes a
# la vez: el analisis decia "no acredita experiencia", el validador avisaba de
# algo que no habia forma de arreglar, y el documento de experiencia salia
# vacio. En una licitacion peruana ese documento es justo el que puntua.

def _fecha(texto: str):
    """Fecha del formulario, o None. Una fecha mal escrita no tumba el alta."""
    texto = (texto or "").strip()
    if not texto:
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


def _claves(objeto: str) -> list[str]:
    """Palabras con las que se buscara esta experiencia mas adelante.

    Se derivan del objeto en vez de pedirselas al usuario: `knowledge_base`
    cruza estas claves contra el objeto de la licitacion, y ese cruce solo
    funciona si los dos lados estan normalizados igual. Pedirlas a mano es
    pedirle al usuario que haga el trabajo de la maquina, y ademas que acierte
    con la normalizacion.
    """
    vistas, salida = set(), []
    for palabra in normalizar(objeto or "").split():
        limpia = "".join(c for c in palabra if c.isalnum())
        if len(limpia) > 5 and limpia not in vistas:
            vistas.add(limpia)
            salida.append(limpia)
    return salida[:12]


@router.post("/empresas/{empresa_id}/experiencia")
async def agregar_experiencia(
        request: Request, empresa_id: int,
        entidad_contratante: str = Form(...), objeto_contrato: str = Form(...),
        monto: str = Form(""), numero_contrato: str = Form(""),
        fecha_inicio: str = Form(""), fecha_fin: str = Form(""),
        conformidad: str = Form("")):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya",
                                status_code=303)

    destino = f"/empresas/{empresa_id}/editar"
    entidad, objeto = entidad_contratante.strip(), objeto_contrato.strip()
    if not entidad or not objeto:
        return RedirectResponse(
            f"{destino}?error={quote_plus('La entidad y el objeto del contrato son obligatorios')}",
            status_code=303)

    # El monto se limpia aqui y no se confia al navegador: llega como texto y
    # puede venir con separadores de miles, con "S/" delante, o vacio.
    valor = parse_monto(monto) if monto.strip() else None

    async with connection() as conn:
        await conn.execute(
            """INSERT INTO experiencia
                   (empresa_id, entidad_contratante, objeto_contrato,
                    numero_contrato, monto, fecha_inicio, fecha_fin,
                    conformidad, keywords)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            empresa_id, entidad, objeto, numero_contrato.strip() or None,
            valor, _fecha(fecha_inicio), _fecha(fecha_fin),
            bool(conformidad), _claves(objeto))

    return RedirectResponse(f"{destino}?aviso=Experiencia+agregada", status_code=303)


@router.post("/empresas/{empresa_id}/experiencia/{exp_id}/borrar")
async def borrar_experiencia(request: Request, empresa_id: int, exp_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya",
                                status_code=303)

    async with connection() as conn:
        # El empresa_id va en el WHERE ademas del id: los dos vienen de la URL,
        # y sin el segundo bastaria cambiar un numero para borrar la
        # experiencia de otra empresa.
        await conn.execute("DELETE FROM experiencia WHERE id=$1 AND empresa_id=$2",
                           exp_id, empresa_id)
    return RedirectResponse(f"/empresas/{empresa_id}/editar?aviso=Experiencia+eliminada",
                            status_code=303)


@router.post("/empresas/{empresa_id}/equipo")
async def agregar_miembro(
        request: Request, empresa_id: int,
        nombre_completo: str = Form(...), titulo_profesional: str = Form(""),
        especialidad: str = Form(""), colegiatura: str = Form(""),
        dni: str = Form(""), anos_experiencia: int = Form(0),
        cargo_habitual: str = Form("")):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya",
                                status_code=303)

    destino = f"/empresas/{empresa_id}/editar"
    nombre = nombre_completo.strip()
    if not nombre:
        return RedirectResponse(
            f"{destino}?error={quote_plus('El nombre del profesional es obligatorio')}",
            status_code=303)

    async with connection() as conn:
        await conn.execute(
            """INSERT INTO equipo_tecnico
                   (empresa_id, nombre_completo, dni, titulo_profesional,
                    colegiatura, especialidad, anos_experiencia, cargo_habitual)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            empresa_id, nombre, dni.strip() or None,
            titulo_profesional.strip() or None, colegiatura.strip() or None,
            especialidad.strip() or None, max(0, anos_experiencia),
            cargo_habitual.strip() or None)

    return RedirectResponse(f"{destino}?aviso=Profesional+agregado", status_code=303)


@router.post("/empresas/{empresa_id}/vencimiento")
async def agregar_vencimiento(request: Request, empresa_id: int,
                              tipo: str = Form(...),
                              fecha_vencimiento: str = Form(...),
                              descripcion: str = Form("")):
    """Anota algo que caduca: poliza, carta fianza, certificado, vigencia.

    El RNP NO se anota aqui: vive en `empresas.rnp_vigencia` porque es un
    atributo de la empresa exigido por ley, con su categoria, y el analisis de
    viabilidad lo consulta como tal. La vista de informes une los dos origenes.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya",
                                status_code=303)

    destino = f"/empresas/{empresa_id}/editar"
    fecha = _fecha(fecha_vencimiento)
    if not tipo.strip() or not fecha:
        return RedirectResponse(
            f"{destino}?error={quote_plus('Hace falta el tipo y una fecha valida')}",
            status_code=303)

    async with connection() as conn:
        await conn.execute(
            """INSERT INTO vencimientos (empresa_id, tipo, descripcion,
                                         fecha_vencimiento)
               VALUES ($1,$2,$3,$4)""",
            empresa_id, tipo.strip(), descripcion.strip() or None, fecha)
    return RedirectResponse(f"{destino}?aviso=Vencimiento+anotado", status_code=303)


@router.post("/empresas/{empresa_id}/vencimiento/{venc_id}/borrar")
async def borrar_vencimiento(request: Request, empresa_id: int, venc_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya",
                                status_code=303)
    async with connection() as conn:
        await conn.execute(
            "DELETE FROM vencimientos WHERE id=$1 AND empresa_id=$2",
            venc_id, empresa_id)
    return RedirectResponse(f"/empresas/{empresa_id}/editar?aviso=Vencimiento+eliminado",
                            status_code=303)


@router.post("/empresas/{empresa_id}/equipo/{miembro_id}/borrar")
async def borrar_miembro(request: Request, empresa_id: int, miembro_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya",
                                status_code=303)

    async with connection() as conn:
        await conn.execute(
            "DELETE FROM equipo_tecnico WHERE id=$1 AND empresa_id=$2",
            miembro_id, empresa_id)
    return RedirectResponse(f"/empresas/{empresa_id}/editar?aviso=Profesional+eliminado",
                            status_code=303)


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
                       telefono=$9, email=$10, rnp_numero=$11,
                       rnp_categoria=$12, rnp_vigencia=$13, rubros=$14
                   WHERE id=$1""",
                empresa_id, *[datos[c] for c in CAMPOS],
                _fecha(formulario.get('rnp_vigencia') or ''), lista_rubros,
            )
            aviso = "Empresa+actualizada"
        else:
            # El tope del plan se comprueba aqui, al guardar, no al pintar el
            # boton: ocultarlo no impide que alguien mande el formulario.
            permitido, motivo = await puede_agregar_empresa(usuario["id"])
            if not permitido:
                from urllib.parse import quote_plus
                return RedirectResponse(f"/empresas?error={quote_plus(motivo)}",
                                        status_code=303)
            # El RUC es unico en toda la tabla: si otro inquilino ya lo cargo, el
            # INSERT falla. Se avisa en vez de reventar con un 500.
            try:
                await conn.execute(
                    """INSERT INTO empresas (razon_social, ruc, representante_legal,
                           dni_representante, cargo_representante, direccion,
                           departamento, telefono, email, rnp_numero,
                           rnp_categoria, rnp_vigencia, rubros,
                           usuario_id, activa)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,TRUE)""",
                    *[datos[c] for c in CAMPOS],
                    _fecha(formulario.get('rnp_vigencia') or ''),
                    lista_rubros, usuario["id"],
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


# ─── Logo, firma y sello ─────────────────────────────────

@router.post("/empresas/{empresa_id}/imagen")
async def subir_imagen(request: Request, empresa_id: int,
                       tipo: str = Form(...), archivo: UploadFile = File(...)):
    """Sube el logo, la firma o el sello de una empresa.

    La imagen se valida y se reescribe en firma_manager: aqui solo se comprueba
    que la empresa sea del usuario. El id viene del formulario, o sea de fuera.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)

    try:
        await guardar_imagen(empresa_id, tipo, await archivo.read())
    except ArchivoInvalido as e:
        # El mensaje de ArchivoInvalido esta escrito para el usuario.
        return RedirectResponse(
            f"/empresas/{empresa_id}/editar?error={quote_plus(str(e))}", status_code=303)
    return RedirectResponse(
        f"/empresas/{empresa_id}/editar?aviso={quote_plus('Imagen guardada.')}",
        status_code=303)


@router.post("/empresas/{empresa_id}/imagen/borrar")
async def quitar_imagen(request: Request, empresa_id: int, tipo: str = Form(...)):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)
    await borrar_imagen(empresa_id, tipo)
    return RedirectResponse(f"/empresas/{empresa_id}/editar?aviso=Imagen+eliminada",
                            status_code=303)


@router.get("/empresas/{empresa_id}/imagen/{tipo}")
async def ver_imagen(request: Request, empresa_id: int, tipo: str):
    """Sirve la imagen para la vista previa.

    Va por una ruta autenticada y no por una carpeta estatica a proposito: la
    firma de un representante legal no debe quedar accesible por URL adivinable
    para cualquiera que pase por ahi.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return PlainTextResponse("No autorizado", status_code=403)

    ruta = (await rutas_de(empresa_id)).get(tipo)
    if not ruta:
        return PlainTextResponse("Sin imagen", status_code=404)
    return FileResponse(ruta, media_type="image/png")
