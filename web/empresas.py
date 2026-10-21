"""Empresas del usuario: alta, edicion y baja.

Sin esto una cuenta nueva no puede hacer nada: al pasar a multi-inquilino se
quitaron las empresas sembradas en schema.sql, asi que el usuario tiene que
poder cargar las suyas.

Toda ruta que recibe un id de empresa valida la propiedad con empresa_es_de
antes de tocar nada. Es el patron de scoping en el borde: el id llega del
formulario, o sea de fuera, y no se puede confiar en el.
"""
import asyncio
import logging
import re
from datetime import date
from decimal import Decimal
from urllib.parse import quote_plus

import asyncpg
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from shared import fechas
from shared import membrete as hoja_membretada
from shared.archivos import MAX_BYTES as MAX_BYTES_IMAGEN
from shared.archivos import (
    TIPOS as TIPOS_IMAGEN,
)
from shared.archivos import (
    ArchivoInvalido,
    borrar_imagen,
    guardar_imagen,
    rutas_de,
)
from shared.config import DEPARTAMENTOS, a_soles, normalizar, parse_monto
from shared.db import connection, empresa_es_de, empresas_de
from shared.suscripciones import puede_agregar_empresa
from web.auth import usuario_actual

log = logging.getLogger("web.empresas")
router = APIRouter()

_DEPARTAMENTOS_NORMALIZADOS = {normalizar(d): d for d in DEPARTAMENTOS}

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

    return _plantillas(request).TemplateResponse(request, "empresas.html", {
        "request": request, "usuario": usuario, "empresas": empresas,
        "uso": uso, "departamentos": DEPARTAMENTOS,
        "aviso": aviso, "error": error,
        # Para pintar el RNP como vigente / por vencer / vencido en la lista.
        "hoy": fechas.hoy(),
    })


@router.get("/empresas/nueva", response_class=HTMLResponse)
async def form_nueva(request: Request):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/empresas/nueva", status_code=303)
    return _vista_nueva(request, usuario)


def _vista_nueva(request: Request, usuario, pre: dict | None = None,
                 aviso: str = "", error: str = ""):
    return _plantillas(request).TemplateResponse(request, "empresa_form.html", {
        "request": request, "usuario": usuario, "empresa": None,
        "departamentos": DEPARTAMENTOS, "pre": pre,
        "aviso": aviso, "error": error,
    })


@router.get("/empresas/{empresa_id}/editar", response_class=HTMLResponse)
async def form_editar(request: Request, empresa_id: int,
                      aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)
    return await _vista_editar(request, usuario, empresa_id, aviso=aviso, error=error)


async def _vista_editar(request: Request, usuario, empresa_id: int,
                        pre: dict | None = None, aviso: str = "", error: str = ""):
    """La pantalla de edicion. `pre` son valores leidos de un documento que
    aun NO se han guardado: el formulario los ensena en lugar de los de la base."""
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

    return _plantillas(request).TemplateResponse(request, "empresa_form.html", {
        "request": request, "usuario": usuario, "empresa": empresa,
        "departamentos": DEPARTAMENTOS, "pre": pre,
        "imagenes": await rutas_de(empresa_id),
        "tipos_imagen": TIPOS_IMAGEN,
        # `de` puede tener que medir la hoja si falta su .json: en un hilo.
        "membrete": await asyncio.to_thread(hoja_membretada.de, empresa_id),
        "experiencia": experiencia, "equipo": equipo,
        "vencimientos": vencimientos, "hoy": fechas.hoy(),
        "aviso": aviso, "error": error,
    })


# ─── Rellenar desde la ficha RUC y la constancia del RNP ─
#
# El usuario sube los PDF que ya tiene y el formulario sale relleno. NO se
# guarda nada aqui: se pinta la misma pantalla con los valores leidos y el
# usuario los revisa y pulsa Guardar, igual que con el autocompletado por RUC.

_CAMPOS_FICHA = ("ruc", "razon_social", "direccion", "departamento", "telefono",
                 "email", "representante_legal", "dni_representante",
                 "cargo_representante")
_CAMPOS_RNP = ("rnp_numero", "rnp_categoria", "rnp_vigencia")
MAX_PDF = 5 * 1024 * 1024


def _impedimento_sunat(datos: dict) -> str | None:
    """El motivo por el que esta ficha no sirve para contratar, o None.

    Un RUC dado de BAJA o con domicilio NO HABIDO no puede ser postor (Ley
    32069: impedimento por la condicion del RUC). Antes se leia igual y se
    rellenaba la empresa; el problema aparecia al presentar la oferta, cuando
    la entidad la descalifica.
    """
    estado = normalizar(datos.get("estado") or "").upper()
    condicion = normalizar(datos.get("condicion") or "").upper()
    if "BAJA" in estado:
        return (f"SUNAT reporta el RUC {datos.get('ruc')} como «{datos['estado']}». "
                "Con el RUC de baja la empresa no puede contratar con el Estado. "
                "No se usó.")
    if "NO HABIDO" in condicion:
        return (f"SUNAT reporta el domicilio del RUC {datos.get('ruc')} como "
                "«NO HABIDO». Así la empresa no puede contratar con el Estado: "
                "regulariza el domicilio en SUNAT y vuelve a subir la ficha. No se usó.")
    return None


async def _leer_subida(archivo: UploadFile | None) -> bytes | None:
    if archivo is None or not archivo.filename:
        return None
    # Uno de mas para saber si se paso del tope sin leer un archivo entero de 1 GB.
    return await archivo.read(MAX_PDF + 1)


@router.post("/empresas/leer-documentos", response_class=HTMLResponse)
async def leer_documentos(request: Request, empresa_id: int = Form(0),
                          ficha: UploadFile | None = File(None),  # noqa: B008
                          constancia: UploadFile | None = File(None)):  # noqa: B008
    from shared.constancia_rnp import ConstanciaInvalida, leer_constancia_rnp
    from shared.ficha_ruc import FichaInvalida, leer_ficha_ruc

    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if empresa_id and not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)

    actual: dict = {}
    if empresa_id:
        async with connection() as conn:
            fila = await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", empresa_id)
        actual = dict(fila)

    async def responder(pre=None, aviso="", error=""):
        if empresa_id:
            return await _vista_editar(request, usuario, empresa_id, pre=pre,
                                       aviso=aviso, error=error)
        return _vista_nueva(request, usuario, pre=pre, aviso=aviso, error=error)

    bytes_ficha = await _leer_subida(ficha)
    bytes_rnp = await _leer_subida(constancia)
    if not bytes_ficha and not bytes_rnp:
        return await responder(error="Elige al menos un PDF: la ficha RUC o la constancia del RNP.")

    for nombre, contenido in (("ficha RUC", bytes_ficha), ("constancia del RNP", bytes_rnp)):
        if contenido and len(contenido) > MAX_PDF:
            return await responder(error=(
                f"El PDF de la {nombre} pesa más de {MAX_PDF // (1024 * 1024)} MB."))

    # PRIMERO se valida todo y DESPUES se guarda. POR QUE: la ficha se escribia
    # en disco antes de mirar la constancia; si la constancia era de otro RUC
    # la pantalla decia "No se usó" pero la ficha ya habia quedado guardada
    # como documento de la empresa, y salia adjunta en la siguiente cotizacion.
    pre = dict(actual)
    leidos, notas = [], []
    try:
        if bytes_ficha:
            # pypdf con un PDF escaneado tarda: fuera del bucle de eventos.
            datos = await asyncio.to_thread(leer_ficha_ruc, bytes_ficha)
            if actual.get("ruc") and datos["ruc"] != actual["ruc"]:
                return await responder(error=(
                    f"La ficha es del RUC {datos['ruc']} y esta empresa es "
                    f"{actual['ruc']}. No se usó."))
            impedimento = _impedimento_sunat(datos)
            if impedimento:
                return await responder(error=impedimento)
            for c in _CAMPOS_FICHA:
                if datos.get(c):
                    pre[c] = datos[c]
            # Los rubros de la ficha son las actividades CIIU de SUNAT: sirven
            # para empezar, pero si la empresa ya tiene los suyos no se pisan.
            if datos.get("rubros") and not actual.get("rubros"):
                pre["rubros"] = datos["rubros"]
            if datos.get("origen") == "consulta":
                leidos.append("la consulta RUC")
                notas.append("La consulta RUC no trae representante legal, DNI, "
                             "teléfono ni correo: complétalos a mano.")
            else:
                leidos.append("la ficha RUC")
            if datos.get("estado") or datos.get("condicion"):
                notas.append(f"SUNAT: {datos.get('estado') or '?'} · {datos.get('condicion') or '?'}.")
        if bytes_rnp:
            # El OCR tarda un par de segundos: fuera del bucle de eventos.
            rnp = await asyncio.to_thread(leer_constancia_rnp, bytes_rnp)
            ruc_doc = pre.get("ruc")
            if not rnp.get("ruc"):
                # Sin RUC leido no hay forma de saber de quien es la constancia.
                # Antes se aceptaba igual, y una constancia de OTRA empresa
                # podia quedar como la de esta.
                return await responder(pre=pre if bytes_ficha else None, error=(
                    "No se pudo leer el RUC en la constancia del RNP, así que no "
                    "podemos confirmar que sea de esta empresa. No se usó: "
                    "escribe los datos del RNP a mano."))
            if ruc_doc and rnp["ruc"] != ruc_doc:
                return await responder(pre=pre if bytes_ficha else None, error=(
                    f"La constancia del RNP es del RUC {rnp['ruc']} y la empresa es "
                    f"{ruc_doc}. No se usó."))
            for c in _CAMPOS_RNP:
                pre[c] = rnp.get(c)
            leidos.append("la constancia del RNP")
            if rnp.get("indeterminada"):
                notas.append("El RNP es de vigencia indeterminada: no vence, así que "
                             "la fecha de vencimiento queda vacía.")
            elif rnp.get("vigencia_leida") is False:
                notas.append("No se pudo leer la vigencia de algún capítulo del RNP: "
                             "revisa la fecha de vencimiento a mano.")
            vence = rnp.get("rnp_vigencia")
            if vence and vence < fechas.hoy():
                notas.append(f"OJO: según la constancia el RNP venció el "
                             f"{vence.strftime('%d/%m/%Y')}. Sin RNP vigente no se "
                             f"puede contratar con el Estado.")
    except (FichaInvalida, ConstanciaInvalida) as e:
        return await responder(error=str(e))

    if empresa_id:
        # Ya validado todo: se guardan los PDF para adjuntarlos en las
        # cotizaciones que los piden (la ficha, firmada y sellada).
        from shared.constancia_rnp import guardar_pdf as guardar_rnp
        from shared.ficha_ruc import guardar_pdf as guardar_ficha
        if bytes_ficha:
            await asyncio.to_thread(guardar_ficha, empresa_id, bytes_ficha)
        if bytes_rnp:
            await asyncio.to_thread(guardar_rnp, empresa_id, bytes_rnp)

    aviso = (f"Leímos {' y '.join(leidos)}. Revisa los datos y pulsa Guardar: "
             f"todavía no se ha guardado nada. " + " ".join(notas)).strip()
    return await responder(pre=pre, aviso=aviso)


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


class DatoInvalido(ValueError):
    """Un campo del formulario no se entiende. El mensaje es para el usuario."""


def _fecha_estricta(texto: str, nombre: str):
    """Fecha del formulario: None si viene vacia, DatoInvalido si no se entiende.

    POR QUE NO `_fecha`: esa convierte una fecha mal escrita en None, y en la
    experiencia eso guardaba el contrato SIN fechas sin decir nada. Para un
    documento que puntua en la evaluacion, un dato perdido en silencio es
    peor que un aviso.
    """
    texto = (texto or "").strip()
    if not texto:
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        raise DatoInvalido(f"La {nombre} no es una fecha válida.") from None


def _monto_formulario(texto: str) -> Decimal | None:
    """Monto escrito por el usuario: None si viene vacio, DatoInvalido si no vale.

    Se marca como soles antes de pasarlo a `parse_monto`: sin moneda esa
    funcion descarta los enteros sueltos ("15000") por si son un ano o un
    correlativo, y aqui el campo ES un monto. Los espacios entre grupos de
    miles ("120 000") se juntan para no leer solo "120".

    Sale en Decimal porque va a experiencia.monto, NUMERIC desde 0021.
    `parse_monto` da un float con a lo sumo dos decimales, que `a_soles`
    devuelve exacto a lo que se escribio.
    """
    texto = (texto or "").strip()
    if not texto:
        return None
    junto = re.sub(r"(?<=\d)\s+(?=\d{3}\b)", "", texto)
    if not re.search(r"s/|us\$|\$|\bpen\b|\busd\b|\bsoles\b", junto, re.IGNORECASE):
        junto = f"S/ {junto}"
    valor = parse_monto(junto)
    if valor is None:
        raise DatoInvalido(
            "El monto no es válido. Escríbelo en soles, por ejemplo 15000 o 15,000.50.")
    return a_soles(valor)


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
        entidad_contratante: str = Form(""), objeto_contrato: str = Form(""),
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
    # puede venir con separadores de miles, con "S/" delante, o vacio. Un monto
    # o una fecha que no se entienden se devuelven al usuario: antes se
    # guardaba la experiencia con el dato vacio y nadie se enteraba.
    try:
        valor = _monto_formulario(monto)
        inicio = _fecha_estricta(fecha_inicio, "fecha de inicio")
        fin = _fecha_estricta(fecha_fin, "fecha de fin")
        if inicio and fin and fin < inicio:
            raise DatoInvalido("La fecha de fin es anterior a la de inicio.")
    except DatoInvalido as e:
        return RedirectResponse(f"{destino}?error={quote_plus(str(e))}", status_code=303)

    async with connection() as conn:
        await conn.execute(
            """INSERT INTO experiencia
                   (empresa_id, entidad_contratante, objeto_contrato,
                    numero_contrato, monto, fecha_inicio, fecha_fin,
                    conformidad, keywords)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            empresa_id, entidad, objeto, numero_contrato.strip() or None,
            valor, inicio, fin,
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
        nombre_completo: str = Form(""), titulo_profesional: str = Form(""),
        especialidad: str = Form(""), colegiatura: str = Form(""),
        # Texto y no int: con `int` un "5 años" o un "cinco" daba un 422 en
        # JSON en vez de volver al formulario con el motivo.
        dni: str = Form(""), anos_experiencia: str = Form(""),
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
    anos_txt = anos_experiencia.strip() or "0"
    if not re.fullmatch(r"[0-9]{1,2}", anos_txt) or int(anos_txt) > 70:
        return RedirectResponse(
            f"{destino}?error={quote_plus('Los años de experiencia tienen que ser un número entero entre 0 y 70')}",
            status_code=303)
    anos = int(anos_txt)

    async with connection() as conn:
        await conn.execute(
            """INSERT INTO equipo_tecnico
                   (empresa_id, nombre_completo, dni, titulo_profesional,
                    colegiatura, especialidad, anos_experiencia, cargo_habitual)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            empresa_id, nombre, dni.strip() or None,
            titulo_profesional.strip() or None, colegiatura.strip() or None,
            especialidad.strip() or None, anos,
            cargo_habitual.strip() or None)

    return RedirectResponse(f"{destino}?aviso=Profesional+agregado", status_code=303)


@router.post("/empresas/{empresa_id}/vencimiento")
async def agregar_vencimiento(request: Request, empresa_id: int,
                              tipo: str = Form(""),
                              fecha_vencimiento: str = Form(""),
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

    # Al formulario de donde vino, con el motivo: la lista no muestra que campo
    # fallo y el usuario tendria que adivinarlo.
    volver = f"/empresas/{empresa_id}/editar" if empresa_id else "/empresas"

    # Departamento, solo uno de la lista. POR QUE: el valor llega del navegador
    # y se usa para filtrar licitaciones y se imprime en los PDF; uno inventado
    # no casaba con nada y una cadena rara acababa dentro de reportlab. Se
    # acepta sin tildes o en mayusculas y se guarda con la grafia de la lista.
    if datos["departamento"]:
        canonico = _DEPARTAMENTOS_NORMALIZADOS.get(normalizar(datos["departamento"]))
        if not canonico:
            return RedirectResponse(
                f"{volver}?error={quote_plus('Elige un departamento de la lista.')}",
                status_code=303)
        datos["departamento"] = canonico
    try:
        vigencia = _fecha_estricta(formulario.get("rnp_vigencia") or "",
                                             "fecha de vencimiento del RNP")
    except DatoInvalido as e:
        return RedirectResponse(f"{volver}?error={quote_plus(str(e))}", status_code=303)

    lista_rubros = _rubros(rubros)

    # Las comprobaciones que abren su propia conexion van ANTES de pedir la de
    # esta funcion. POR QUE: dentro del `async with` cada guardado ocupaba dos
    # conexiones a la vez, y con diez guardados simultaneos el pool (10) se
    # quedaba esperando a si mismo.
    if empresa_id:
        if not await empresa_es_de(empresa_id, usuario["id"]):
            return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya",
                                    status_code=303)
    else:
        # El tope del plan se comprueba aqui, al guardar, no al pintar el
        # boton: ocultarlo no impide que alguien mande el formulario.
        permitido, motivo = await puede_agregar_empresa(usuario["id"])
        if not permitido:
            return RedirectResponse(f"/empresas?error={quote_plus(motivo)}",
                                    status_code=303)

    # El RUC es unico en toda la tabla: si otro inquilino ya lo cargo, el
    # INSERT o el UPDATE fallan. Se avisa en vez de reventar con un 500. Solo
    # se traduce ESE error: antes cualquier fallo (una columna mal escrita, la
    # base caida) se mostraba como "RUC ya registrado" y escondia el problema.
    ruc_repetido = (f"Ya hay una empresa registrada con el RUC {datos['ruc']}. "
                    "Si es tuya y no la ves, escríbenos.")
    async with connection() as conn:
        if empresa_id:
            try:
                await conn.execute(
                    """UPDATE empresas SET razon_social=$2, ruc=$3,
                           representante_legal=$4, dni_representante=$5,
                           cargo_representante=$6, direccion=$7, departamento=$8,
                           telefono=$9, email=$10, rnp_numero=$11,
                           rnp_categoria=$12, rnp_vigencia=$13, rubros=$14
                       WHERE id=$1""",
                    empresa_id, *[datos[c] for c in CAMPOS], vigencia, lista_rubros,
                )
            except asyncpg.UniqueViolationError:
                return RedirectResponse(f"{volver}?error={quote_plus(ruc_repetido)}",
                                        status_code=303)
            aviso = "Empresa actualizada."
        else:
            try:
                await conn.execute(
                    """INSERT INTO empresas (razon_social, ruc, representante_legal,
                           dni_representante, cargo_representante, direccion,
                           departamento, telefono, email, rnp_numero,
                           rnp_categoria, rnp_vigencia, rubros,
                           usuario_id, activa)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,TRUE)""",
                    *[datos[c] for c in CAMPOS], vigencia,
                    lista_rubros, usuario["id"],
                )
            except asyncpg.UniqueViolationError as e:
                log.info("Alta de empresa con RUC repetido: %s", e)
                return RedirectResponse(f"/empresas?error={quote_plus(ruc_repetido)}",
                                        status_code=303)
            aviso = "Empresa creada."

    if vigencia and vigencia < fechas.hoy():
        # Se guarda igual (es el dato que hay), pero se dice: con el RNP
        # vencido la empresa no puede presentarse a nada.
        aviso += (f" OJO: el RNP venció el {vigencia.strftime('%d/%m/%Y')}; sin RNP "
                  "vigente no se puede contratar con el Estado.")
    return RedirectResponse(f"/empresas?aviso={quote_plus(aviso)}", status_code=303)


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
                       # `File(...)` en el default es como FastAPI declara una
                       # subida, no el `def f(x=[])` que persigue B008: el
                       # objeto es el descriptor que la ruta lee al montarse.
                       tipo: str = Form(""), archivo: UploadFile = File(...)):  # noqa: B008
    """Sube el logo, la firma o el sello de una empresa.

    La imagen se valida y se reescribe en firma_manager: aqui solo se comprueba
    que la empresa sea del usuario. El id viene del formulario, o sea de fuera.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)

    # Uno de mas que el tope, y no el archivo entero. POR QUE: `read()` sin
    # limite cargaba en memoria lo que mandara el cliente antes de mirar el
    # tamano; un POST de 2 GB tumbaba el unico worker.
    datos = await archivo.read(MAX_BYTES_IMAGEN + 1)
    if len(datos) > MAX_BYTES_IMAGEN:
        error = (f"La imagen pesa más de {MAX_BYTES_IMAGEN // (1024 * 1024)} MB. "
                 "Redúcela y vuelve a intentarlo.")
        return RedirectResponse(
            f"/empresas/{empresa_id}/editar?error={quote_plus(error)}", status_code=303)
    try:
        await guardar_imagen(empresa_id, tipo, datos)
    except ArchivoInvalido as e:
        # El mensaje de ArchivoInvalido esta escrito para el usuario.
        return RedirectResponse(
            f"/empresas/{empresa_id}/editar?error={quote_plus(str(e))}", status_code=303)
    return RedirectResponse(
        f"/empresas/{empresa_id}/editar?aviso={quote_plus('Imagen guardada.')}",
        status_code=303)


@router.post("/empresas/{empresa_id}/imagen/borrar")
async def quitar_imagen(request: Request, empresa_id: int, tipo: str = Form("")):
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


# ─── Hoja membretada ─────────────────────────────────────

@router.post("/empresas/{empresa_id}/membrete")
async def subir_membrete(request: Request, empresa_id: int,
                         archivo: UploadFile = File(...)):  # noqa: B008
    """El Word membretado de la empresa: el papel de sus cotizaciones y cartas.

    Se convierte a PDF al subirlo (LibreOffice tarda unos segundos), asi cada
    documento despues solo se pone encima de la hoja ya convertida.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)
    try:
        # Con tope, como las imagenes: `guardar` rechaza lo que pase de
        # TAMANO_MAXIMO, y para eso basta leer un byte de mas.
        await hoja_membretada.guardar(
            empresa_id, await archivo.read(hoja_membretada.TAMANO_MAXIMO + 1))
    except hoja_membretada.MembreteInvalido as e:
        return RedirectResponse(
            f"/empresas/{empresa_id}/editar?error={quote_plus(str(e))}#membrete",
            status_code=303)
    return RedirectResponse(
        f"/empresas/{empresa_id}/editar?aviso={quote_plus('Hoja membretada guardada.')}#membrete",
        status_code=303)


@router.post("/empresas/{empresa_id}/membrete/borrar")
async def quitar_membrete(request: Request, empresa_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return RedirectResponse("/empresas?error=Esa+empresa+no+es+tuya", status_code=303)
    hoja_membretada.borrar(empresa_id)
    return RedirectResponse(
        f"/empresas/{empresa_id}/editar?aviso={quote_plus('Hoja membretada quitada.')}#membrete",
        status_code=303)


@router.get("/empresas/{empresa_id}/membrete.png")
async def ver_membrete(request: Request, empresa_id: int):
    """Vista previa de la hoja tal como sale en los documentos. Autenticada,
    como el logo y la firma: lleva RUC, direccion y telefonos."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await empresa_es_de(empresa_id, usuario["id"]):
        return PlainTextResponse("No autorizado", status_code=403)
    # Renderizar la hoja con pdfium tarda: en un hilo, no en el bucle de eventos.
    png = await asyncio.to_thread(hoja_membretada.miniatura_png, empresa_id)
    if not png:
        return PlainTextResponse("Sin hoja membretada", status_code=404)
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "private, no-store"})


# ─── Autocompletar por RUC ───────────────────────────────

@router.get("/empresas/ruc/{ruc}")
async def datos_por_ruc(request: Request, ruc: str):
    """Razon social, direccion y region de un RUC, para rellenar el alta.

    Lo consume el formulario de empresa al teclear 11 digitos
    (web/static/licitapro.js). Va detras del login para que el servicio no sea
    un consultor de RUC abierto a internet a costa de la cuota de los
    republicadores que usa shared/sunat.py. No escribe nada: lo que devuelve
    pasa por el formulario y lo guarda el usuario.
    """
    from shared.sunat import consultar_ruc, ruc_valido

    usuario = await usuario_actual(request)
    if not usuario:
        return JSONResponse({"error": "sin sesion"}, status_code=401)
    ruc = ruc.strip()
    if not ruc_valido(ruc):
        return JSONResponse({"error": "RUC no valido"}, status_code=400)
    datos = await consultar_ruc(ruc)
    if not datos:
        return JSONResponse({"error": "RUC no encontrado"}, status_code=404)
    return JSONResponse(datos)
