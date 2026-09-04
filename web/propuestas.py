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
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from shared import ia
from shared.banderas import describir
from shared.db import connection, empresa_es_de, empresas_de, responder_pregunta
from shared.pdf_firmable import generar_pdf
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
async def detalle_licitacion(request: Request, licitacion_id: str,
                             error: str = "", aviso: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse(f"/entrar?siguiente=/licitacion/{licitacion_id}",
                                status_code=303)

    async with connection() as conn:
        lic = await conn.fetchrow("SELECT * FROM licitaciones WHERE id=$1", licitacion_id)
        if not lic:
            return RedirectResponse("/panel", status_code=303)
        mias = await conn.fetch(
            """SELECT p.id, p.estado, e.razon_social
                 FROM propuestas p JOIN empresas e ON e.id = p.empresa_id
                WHERE p.licitacion_id = $1 AND e.usuario_id = $2""",
            licitacion_id, usuario["id"])
        seguida = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM licitaciones_seguidas
                              WHERE usuario_id=$1 AND licitacion_id=$2)""",
            usuario["id"], licitacion_id)

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
        "empresas": await empresas_de(usuario["id"]),
        # Los analisis son de ESTE usuario: `analisis_guardado` filtra por su
        # id. El analisis se hace contra una empresa concreta, asi que hay uno
        # por empresa suya que lo haya pedido.
        "analisis": await ia.analisis_guardado(usuario["id"], licitacion_id),
        "cuota_ia": await ia.cuota(usuario["id"]),
        "seguida": seguida,
        "error": error, "aviso": aviso,
    })


@router.post("/licitacion/{licitacion_id}/seguir")
async def alternar_seguimiento(request: Request, licitacion_id: str,
                               volver: str = Form("")):
    """Marca o desmarca interes en una licitacion, sin abrir expediente.

    POR QUE HACE FALTA UN PASO INTERMEDIO

      Hasta ahora la unica accion era postular, y postular abre un expediente
      con sus preguntas y sus documentos. Es demasiado compromiso para algo que
      todavia estas evaluando: entre "me avisaron" y "me presento" hay dias de
      leer bases y mandar consultas.

      Sin donde apuntarlo, quien duda acaba llevando la lista en otro sitio --
      que es justo donde empieza a no necesitar el producto.

    El destino vuelve por formulario porque se sigue desde dos sitios, la ficha
    y la tabla del panel, y el usuario espera quedarse donde estaba.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse(f"/entrar?siguiente=/licitacion/{licitacion_id}",
                                status_code=303)

    async with connection() as conn:
        # DELETE primero y, si no borro nada, INSERT. Un viaje de ida y vuelta
        # menos que consultar antes, y sin ventana entre la consulta y la
        # escritura.
        estado = await conn.execute(
            """DELETE FROM licitaciones_seguidas
                WHERE usuario_id=$1 AND licitacion_id=$2""",
            usuario["id"], licitacion_id)
        # asyncpg devuelve el estado de Postgres, "DELETE <n>". Se lee el
        # numero y no se compara contra la cadena entera: comparar con un
        # literal que parece SQL confunde a `tools/auditar_sql.py`, que lo
        # recoge como una consulta y falla al validarla.
        borradas = int(estado.rsplit(" ", 1)[-1])
        if borradas == 0:
            await conn.execute(
                """INSERT INTO licitaciones_seguidas (usuario_id, licitacion_id)
                   VALUES ($1,$2) ON CONFLICT DO NOTHING""",
                usuario["id"], licitacion_id)

    destino = volver.strip() or f"/licitacion/{licitacion_id}"
    # Solo rutas propias: `volver` llega de un formulario, y sin esta
    # comprobacion seria una redireccion abierta hacia donde quisiera quien
    # montara el enlace.
    if not destino.startswith("/") or destino.startswith("//"):
        destino = f"/licitacion/{licitacion_id}"
    return RedirectResponse(destino, status_code=303)


@router.post("/licitacion/{licitacion_id}/analizar")
async def analizar_con_ia(request: Request, licitacion_id: str,
                          empresa_id: int = Form(...)):
    """Pide el analisis de viabilidad de esta licitacion para una empresa.

    Cada pulsacion es una llamada de pago a la API de Anthropic que paga la
    plataforma, no el cliente. De ahi las tres comprobaciones antes de gastar:
    que la empresa sea suya, que su plan incluya IA, y que le quede cuota. Sin
    la ultima, una cuenta que pulse en bucle gasta mas de lo que paga al mes.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse(f"/entrar?siguiente=/licitacion/{licitacion_id}",
                                status_code=303)

    destino = f"/licitacion/{licitacion_id}"
    if not await empresa_es_de(empresa_id, usuario["id"]):
        # Mismo mensaje que si la empresa no existiera: confirmar que un id
        # ajeno es valido ya es filtrar algo.
        return RedirectResponse(f"{destino}?error={quote_plus('Empresa no valida')}",
                                status_code=303)

    permiso = await ia.cuota(usuario["id"])
    if not permiso["permitido"]:
        if not permiso["por_plan"]:
            motivo = (f"El analisis con IA no esta incluido en tu plan "
                      f"{permiso['plan']}. Cambia de plan para usarlo.")
        else:
            motivo = (f"Has usado los {permiso['tope']} analisis con IA de este "
                      f"mes. El contador vuelve a cero el dia 1.")
        return RedirectResponse(f"{destino}?error={quote_plus(motivo)}",
                                status_code=303)

    async with connection() as conn:
        lic = await conn.fetchrow("SELECT * FROM licitaciones WHERE id=$1",
                                  licitacion_id)
    if not lic:
        return RedirectResponse("/panel", status_code=303)

    from radar_bot.analyzer import analizar
    salida = await analizar(usuario["id"], empresa_id, dict(lic))

    if salida["aviso"]:
        return RedirectResponse(f"{destino}?error={quote_plus(salida['aviso'])}",
                                status_code=303)
    return RedirectResponse(f"{destino}?aviso={quote_plus('Analisis actualizado')}",
                            status_code=303)


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
    except Exception:
        log.exception("No se pudieron generar preguntas para %s", propuesta_id)

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

    # Que falta para que el expediente sea presentable, y cuanto se ha pagado
    # por trabajos parecidos. Los dos modulos existian desde el principio y no
    # los llamaba nadie: el ZIP se armaba con los campos legales vacios, y
    # `historico_precios` -- que los scrapers llenan en cada corrida -- no lo
    # leia ninguna consulta del producto.
    validacion = await _validacion(propuesta_id)
    return _plantillas(request).TemplateResponse("propuesta.html", {
        "request": request, "usuario": usuario, "p": prop,
        "preguntas": preguntas,
        "pendientes": [q for q in preguntas if not q["respondida"]],
        "validacion": validacion,
        "precio": await _precio_de_mercado(prop),
        "aviso": aviso, "error": error,
    })


async def _validacion(propuesta_id: int) -> dict | None:
    """Que le falta a la propuesta. None si el validador se cae.

    Se traga el error a proposito: un fallo aqui no puede dejar sin ficha a
    alguien que solo queria leer sus preguntas. Lo que NO se traga es el fallo
    al generar el ZIP, donde si hay que parar.
    """
    try:
        from prep_bot.autofill.validator import validar_propuesta
        return await validar_propuesta(propuesta_id)
    except Exception:
        log.exception("Validacion de %s fallo", propuesta_id)
        return None


async def _precio_de_mercado(prop) -> dict | None:
    """Rango de precios de trabajos parecidos, del historico de adjudicaciones."""
    if not prop["licitacion_id"]:
        return None
    try:
        from prep_bot.autofill.pricing import estimar_precio_mercado
        estimado = await estimar_precio_mercado({
            # El id va incluido para que la propia licitacion quede fuera de su
            # comparativa: sin el, el monto referencial que se quiere estimar
            # entra como una muestra mas y arrastra la mediana hacia si mismo.
            "id": prop["licitacion_id"],
            "objeto": prop["objeto"], "tipo": prop["tipo"],
            "monto_referencial": prop["monto_referencial"],
            "departamento": prop["departamento"],
        })
        return None if estimado.get("error") else estimado
    except Exception:
        log.exception("Estimacion de precio de %s fallo", prop["id"])
        return None


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
    except Exception:
        log.exception("Autofill fallo en %s", propuesta_id)
        return RedirectResponse(
            f"/propuestas/{propuesta_id}?error=No+se+pudieron+generar+los+documentos",
            status_code=303)

    aviso = await _propuesta_tecnica(usuario["id"], prop)
    return RedirectResponse(
        f"/propuestas/{propuesta_id}?aviso={quote_plus('Documentos generados. ' + aviso)}",
        status_code=303)


async def _propuesta_tecnica(usuario_id: int, prop) -> str:
    """Genera la propuesta tecnica y devuelve que se le puede decir al usuario.

    `zip_builder` la incluye "si existe", y hasta ahora no existia nunca porque
    nadie llamaba a quien la escribe. El expediente salia sin ella y sin avisar.

    SE RIGE POR EL PLAN, PERO NO GASTA LA CUOTA DE ANALISIS

      Por dos motivos. Uno: `analisis_ia` tiene clave unica por (usuario,
      empresa, licitacion), asi que anotar aqui el documento SOBRESCRIBIRIA el
      analisis de viabilidad de esa misma ficha. Dos: el contador que se pinta
      en la ficha dice "analisis con IA", y verlo subir al generar un documento
      no cuadra con lo que el usuario acaba de hacer.

      Tampoco hace falta como freno. El tope existe porque "Analizar" se puede
      pulsar en bucle sobre cualquier ficha del pozo; una propuesta tecnica es
      una por propuesta, y abrir propuestas ya esta acotado por `max_empresas`
      y por el trabajo humano de rellenarlas.

    Sin IA se escribe igual, con la plantilla: un documento sin IA es peor que
    uno con IA, y los dos son mucho mejores que presentarse sin propuesta
    tecnica.
    """
    from prep_bot.autofill.proposal import generar_propuesta_tecnica
    from shared.knowledge_base import obtener_datos_empresa_completos

    permiso = await ia.cuota(usuario_id)
    con_ia = permiso["por_plan"] and ia.disponible()

    try:
        datos = await obtener_datos_empresa_completos(prop["emp_id"])
        licitacion = {"id": prop["licitacion_id"], "objeto": prop["objeto"],
                      "entidad": prop["entidad"], "tipo": prop["tipo"],
                      "monto_referencial": prop["monto_referencial"]}
        if con_ia:
            if await generar_propuesta_tecnica(
                    prop["id"], prop["emp_id"], licitacion, datos):
                return "Propuesta técnica redactada con IA."
        else:
            # Sin ANTHROPIC_KEY la propia funcion cae a la plantilla; sin cuota
            # hay que forzarla desde aqui, que es donde se conoce el plan.
            import prep_bot.autofill.proposal as pp
            clave, pp.ANTHROPIC_KEY = pp.ANTHROPIC_KEY, ""
            try:
                await generar_propuesta_tecnica(
                    prop["id"], prop["emp_id"], licitacion, datos)
            finally:
                pp.ANTHROPIC_KEY = clave
            motivo = ("tu plan no incluye IA" if not permiso["por_plan"]
                      else "no hay clave de IA configurada")
            return f"Propuesta técnica con plantilla, porque {motivo}."
    except Exception:
        log.exception("Propuesta tecnica de %s fallo", prop["id"])
        return "La propuesta técnica no se pudo generar; el resto sí."

    return "La propuesta técnica no se pudo generar; el resto sí."


@router.post("/propuestas/{propuesta_id}/expediente")
async def armar_expediente(request: Request, propuesta_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _propuesta_del_usuario(propuesta_id, usuario["id"]):
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)

    # Se valida ANTES de armar nada. Un expediente al que le falta el DNI del
    # representante o la partida registral no es un expediente incompleto: es
    # una oferta que la entidad devuelve en mesa de partes. Generarlo igual y
    # dejar que el proveedor lo descubra alli es el peor momento posible para
    # enterarse, porque el plazo ya venció.
    validacion = await _validacion(propuesta_id)
    if validacion and validacion.get("faltantes"):
        falta = ", ".join(f["desc"] for f in validacion["faltantes"])
        return RedirectResponse(
            f"/propuestas/{propuesta_id}?error="
            f"{quote_plus(f'Antes de armar el expediente falta: {falta}')}",
            status_code=303)

    try:
        from prep_bot.zip_builder import generar_expediente_zip
        ruta = await generar_expediente_zip(propuesta_id)
    except Exception:
        log.exception("ZIP fallo en %s", propuesta_id)
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


# ─── Declaracion jurada lista para el DNI electronico ────

@router.get("/propuestas/{propuesta_id}/declaracion-jurada")
async def declaracion_jurada(request: Request, propuesta_id: int,
                             modo: str = "dnie"):
    """Genera la Declaracion Jurada de Datos del Postor en PDF.

    Sale en PDF y no en DOCX porque ReFirma, el programa de RENIEC, firma PDF:
    un .docx no tiene donde alojar una firma digital.

    Dos modos, y no son equivalentes ante una entidad:

      dnie      deja el recuadro preparado y el documento SIN firmar, para que
                el titular lo firme en su equipo con su DNIe. Es la firma con
                validez legal equivalente a la manuscrita.
      escaneada incrusta la imagen de firma y el sello que subio el usuario.
                Es una representacion visual: no prueba quien firmo ni que el
                documento no se haya alterado despues.

    La plataforma NO puede firmar con el DNIe del cliente, y no conviene
    sortearlo: la clave privada vive en el chip de la tarjeta y firmar exigiria
    pedirle su PIN, que es justo lo que jamas hay que pedir.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    prop = await _propuesta_del_usuario(propuesta_id, usuario["id"])
    if not prop:
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)

    async with connection() as conn:
        empresa = await conn.fetchrow(
            "SELECT * FROM empresas WHERE id=$1", prop["emp_id"])
    if not empresa:
        return RedirectResponse(
            f"/propuestas/{propuesta_id}?error=No+se+encontro+la+empresa",
            status_code=303)

    emp = dict(empresa)
    parrafos = [
        (f"El que suscribe, {emp.get('representante_legal') or '________________'}, "
        f"identificado con DNI N.º {emp.get('dni_representante') or '________'}, "
        f"en calidad de {emp.get('cargo_representante') or 'representante legal'} "
        f"de {emp.get('razon_social')}, con RUC N.º {emp.get('ruc') or '___________'} "
        f"y domicilio en {emp.get('direccion') or '________________________'}, "
        f"DECLARO BAJO JURAMENTO lo siguiente:"),

        ("1. Que los datos consignados en el presente documento son veraces y "
        "corresponden a la situación actual de mi representada."),

        ("2. Que no me encuentro incurso en ninguno de los impedimentos para "
        "contratar con el Estado establecidos en la Ley General de "
        "Contrataciones Públicas."),

        ("3. Que conozco, acepto y me someto a las bases, condiciones y "
        "procedimientos del proceso de selección."),

        ("4. Que me comprometo a mantener vigente mi oferta durante el plazo "
        "señalado en las bases y a suscribir el contrato en caso de resultar "
        "adjudicado."),

        f"Proceso: {prop.get('objeto') or ''}",
        f"Entidad convocante: {prop.get('entidad') or ''}",
    ]

    con_dnie = (modo or "dnie").lower() != "escaneada"
    nombre = f"declaracion-jurada-{propuesta_id}.pdf"
    try:
        ruta = await generar_pdf(
            nombre_archivo=nombre,
            titulo="DECLARACIÓN JURADA DE DATOS DEL POSTOR",
            subtitulo=prop.get("entidad") or "",
            parrafos=parrafos,
            empresa=emp,
            con_dnie=con_dnie,
        )
    except Exception:
        log.exception("No se pudo generar la declaracion jurada de la propuesta %s",
                  propuesta_id)
        return RedirectResponse(
            f"/propuestas/{propuesta_id}?error="
            + quote_plus("No se pudo generar el documento. Inténtalo de nuevo."),
            status_code=303)

    return FileResponse(ruta, filename=nombre, media_type="application/pdf")
