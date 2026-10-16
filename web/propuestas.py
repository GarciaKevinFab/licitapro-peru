"""Flujo de preparacion: postular, responder preguntas y armar el expediente.

Esto vivia solo en Telegram (/estado, /r, /aprobar). Un chat esta bien para
avisar, pero es pesimo para formularios: no se puede revisar lo escrito, no hay
validacion y el historial se pierde entre mensajes. Aqui el mismo flujo se
maneja con formularios de verdad.

La propiedad se valida SIEMPRE por la empresa de la propuesta, con un JOIN
contra empresas.usuario_id: una propuesta es del usuario si su empresa lo es.
No se guarda usuario_id en propuestas a proposito, para que no puedan discrepar.
"""
import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from shared import cotizacion, ia, llenar_formatos
from shared.banderas import describir
from shared.db import (
    connection,
    documentos_de,
    empresa_es_de,
    empresas_de,
    kb_get,
    responder_pregunta,
)
from shared.documentos_menores import formato_dj, generar_cotizacion, generar_dj
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
                      l.score_viabilidad, l.nomenclatura, l.descripcion, l.fuente
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
    try:
        await cotizacion.sembrar(propuesta_id)
    except Exception:
        log.exception("No se pudieron copiar los items a la propuesta %s", propuesta_id)

    return RedirectResponse(f"/propuestas/{propuesta_id}", status_code=303)


# ─── Propuestas ──────────────────────────────────────────

@router.get("/propuestas", response_class=HTMLResponse)
async def listar(request: Request, aviso: str = "", error: str = "", cerradas: int = 0):
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

    # Lo que ya cerro no se puede enviar: sale de la lista y queda archivado
    # en "Cerradas", lo ultimo primero. La fecha de cierre va en hora de Lima.
    from shared import fechas
    ahora = fechas.ahora()
    vigentes = [f for f in filas if not f["fecha_cierre"] or f["fecha_cierre"] >= ahora]
    pasadas = [f for f in filas if f["fecha_cierre"] and f["fecha_cierre"] < ahora][::-1]
    return _plantillas(request).TemplateResponse("propuestas.html", {
        "request": request, "usuario": usuario,
        "propuestas": pasadas if cerradas else vigentes, "cerradas": bool(cerradas),
        "n_vigentes": len(vigentes), "n_cerradas": len(pasadas), "hay_alguna": bool(filas),
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

    # Las propuestas abiertas antes de existir la cotizacion por items reciben
    # sus items la primera vez que se abren. `sembrar` lo hace una sola vez.
    await cotizacion.sembrar(propuesta_id)
    # Lo que se aprendio despues de abrir la propuesta no se vuelve a pedir.
    try:
        from prep_bot.questioner import cerrar_las_ya_resueltas
        await cerrar_las_ya_resueltas(propuesta_id)
    except Exception:
        log.exception("No se pudieron cerrar las preguntas resueltas de %s", propuesta_id)

    async with connection() as conn:
        preguntas = await conn.fetch(
            """SELECT * FROM preguntas WHERE propuesta_id=$1
               ORDER BY respondida, id""", propuesta_id)
    items = await cotizacion.items_de(propuesta_id)
    menor = cotizacion.es_cotizacion_menor(prop["tipo"])

    # Que falta para que el expediente sea presentable, y cuanto se ha pagado
    # por trabajos parecidos. Los dos modulos existian desde el principio y no
    # los llamaba nadie: el ZIP se armaba con los campos legales vacios, y
    # `historico_precios` -- que los scrapers llenan en cada corrida -- no lo
    # leia ninguna consulta del producto.
    validacion = await _validacion(propuesta_id)
    condiciones = await cotizacion.condiciones_de(propuesta_id)
    async with connection() as conn:
        empresa = await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", prop["emp_id"])
    return _plantillas(request).TemplateResponse("propuesta.html", {
        "portal": _envio_portal(dict(prop), dict(empresa) if empresa else {}, condiciones,
                                cotizacion.total(items) if items else None),
        "request": request, "usuario": usuario, "p": prop,
        "preguntas": preguntas,
        "pendientes": [q for q in preguntas if not q["respondida"]],
        "validacion": validacion,
        # El rango de mercado compara procesos enteros por palabras del objeto.
        # Para una cotizacion por items no dice nada: en la SC 5884-2026 ponia
        # una mediana de S/ 96 mil junto a una oferta de S/ 12 mil.
        "precio": None if (items or menor) else await _precio_de_mercado(prop),
        "items": items, "menor": menor,
        "formatos": await llenar_formatos.disponibles(propuesta_id, prop["licitacion_id"]),
        "anexos": _anexos_de(prop["emp_id"]),
        "documentos": await documentos_de(prop["licitacion_id"]),
        "total_items": cotizacion.total(items),
        "condiciones": condiciones,
        "monto": cotizacion.monto, "cantidad_legible": cotizacion.cantidad_legible,
        "precio_legible": cotizacion.precio_legible,
        "formato_dj": formato_dj(dict(prop)),
        "armado": _contenido_zip(armado) if (armado := paquete_armado(propuesta_id)) else None,
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
        # `nomenclatura` va para que la portada diga COT-5884-2026-GOREMAD y no
        # el id interno; `descripcion` lleva los items, sin los cuales la IA
        # escribia sobre "alimentos" sin saber cuales.
        licitacion = {"id": prop["licitacion_id"], "objeto": prop["objeto"],
                      "entidad": prop["entidad"], "tipo": prop["tipo"],
                      "nomenclatura": prop["nomenclatura"],
                      "descripcion": prop["descripcion"],
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
    if paquete_armado(propuesta_id):
        return _volver(propuesta_id, aviso="El envío ya está armado con tus documentos "
                       "firmados: descárgalo en «Enviar por el portal».")

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
    if prop and paquete_armado(propuesta_id):
        return RedirectResponse(f"/propuestas/{propuesta_id}/paquete", status_code=303)
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
                             modo: str = "dnie", propio: int = 0):
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

    cci = await kb_get(prop["emp_id"], "financiero", "cci")
    # Si el usuario subio la DJ de la entidad, sale ESA llena. Si no se puede
    # llenar, se le dice por que y puede pedir la propia con ?propio=1.
    if not propio:
        try:
            pdf = await llenar_formatos.dj_llena(
                propuesta_id, prop["licitacion_id"], dict(empresa), cci,
                await _imagenes(prop["emp_id"], modo))
        except llenar_formatos.FormatoNoReconocido as e:
            return _volver(propuesta_id, error=(
                f"No pudimos llenar la declaración jurada de la entidad: {e} Puedes "
                "descargar la nuestra o subir el formato a mano."))
        if pdf:
            return FileResponse(_escribir(f"declaracion-jurada-formato-{propuesta_id}.pdf", pdf),
                                filename=f"declaracion-jurada-{propuesta_id}.pdf",
                                media_type="application/pdf")

    # El formato lo decide la entidad: el GOREMAD exige el suyo, con la CCI.
    con_dnie = (modo or "dnie").lower() != "escaneada"
    nombre = f"declaracion-jurada-{propuesta_id}.pdf"
    try:
        ruta = await generar_dj(dict(prop), dict(empresa), cci, con_dnie)
    except Exception:
        log.exception("No se pudo generar la declaracion jurada de la propuesta %s",
                      propuesta_id)
        return RedirectResponse(
            f"/propuestas/{propuesta_id}?error="
            + quote_plus("No se pudo generar el documento. Inténtalo de nuevo."),
            status_code=303)

    return FileResponse(ruta, filename=nombre, media_type="application/pdf")


# ─── Cotizacion por items ────────────────────────────────

def _volver(propuesta_id: int, **query) -> RedirectResponse:
    clave, texto = next(iter(query.items()))
    return RedirectResponse(
        f"/propuestas/{propuesta_id}?{clave}={quote_plus(texto)}#cotizacion",
        status_code=303)


@router.post("/propuestas/{propuesta_id}/items")
async def guardar_items(request: Request, propuesta_id: int):
    """Marca y precio unitario de cada item, y las condiciones del pie.

    Los campos llegan como marca_<id> y precio_<id>. Se recorren los items de
    ESTA propuesta, no los del formulario: un id ajeno metido a mano no
    corresponde a ningun item y se ignora.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _propuesta_del_usuario(propuesta_id, usuario["id"]):
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)

    form = await request.form()
    cambios = {}
    for n, it in enumerate(await cotizacion.items_de(propuesta_id), 1):
        crudo = form.get(f"precio_{it['id']}", "")
        precio = cotizacion.a_decimal(crudo)
        if str(crudo).strip() and precio is None:
            return _volver(propuesta_id,
                           error=f"El precio del ítem {n} no es un número válido")
        cambios[it["id"]] = (form.get(f"marca_{it['id']}", ""),
                             form.get(f"presentacion_{it['id']}", ""), precio)

    suma = await cotizacion.guardar(propuesta_id, cambios, dict(form))
    return _volver(propuesta_id,
                   aviso=f"Cotización guardada. Total S/ {cotizacion.monto(suma)}")


@router.post("/propuestas/{propuesta_id}/items/nuevo")
async def agregar_item(request: Request, propuesta_id: int,
                       descripcion: str = Form(...), cantidad: str = Form(...),
                       unidad: str = Form("")):
    """Para las fuentes que no publican items: se cargan a mano desde la SC."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _propuesta_del_usuario(propuesta_id, usuario["id"]):
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)
    cant = cotizacion.a_decimal(cantidad)
    if not descripcion.strip() or not cant:
        return _volver(propuesta_id,
                       error="El ítem necesita descripción y una cantidad mayor que cero")
    await cotizacion.agregar(propuesta_id, descripcion, cant, unidad)
    return _volver(propuesta_id, aviso="Ítem agregado")


@router.post("/propuestas/{propuesta_id}/items/{item_id}/borrar")
async def quitar_item(request: Request, propuesta_id: int, item_id: int):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _propuesta_del_usuario(propuesta_id, usuario["id"]):
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)
    await cotizacion.quitar(propuesta_id, item_id)
    return _volver(propuesta_id, aviso="Ítem quitado")


@router.get("/propuestas/{propuesta_id}/cotizacion")
async def descargar_cotizacion(request: Request, propuesta_id: int, modo: str = "dnie",
                               propio: int = 0):
    """La cotizacion en PDF, con la misma tabla que mando la entidad ya llena."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    prop = await _propuesta_del_usuario(propuesta_id, usuario["id"])
    if not prop:
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)

    items = await cotizacion.items_de(propuesta_id)
    if not items:
        return _volver(propuesta_id, error="La cotización no tiene ítems todavía")
    async with connection() as conn:
        empresa = await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", prop["emp_id"])
    condiciones = await cotizacion.condiciones_de(propuesta_id)
    if not propio:
        try:
            pdf = await llenar_formatos.sc_llena(
                propuesta_id, prop["licitacion_id"], dict(empresa), items, condiciones,
                await _imagenes(prop["emp_id"], modo))
        except llenar_formatos.FormatoNoReconocido as e:
            return _volver(propuesta_id, error=(
                f"No pudimos llenar la solicitud de cotización de la entidad: {e} Puedes "
                "descargar la nuestra o subir el formato a mano."))
        if pdf:
            return FileResponse(_escribir(f"cotizacion-formato-{propuesta_id}.pdf", pdf),
                                filename=f"cotizacion-{propuesta_id}.pdf",
                                media_type="application/pdf")
    try:
        ruta = await generar_cotizacion(
            dict(prop), dict(empresa), items, condiciones,
            await kb_get(prop["emp_id"], "financiero", "cci"),
            con_dnie=(modo or "dnie").lower() != "escaneada")
    except Exception:
        log.exception("No se pudo generar la cotizacion de la propuesta %s", propuesta_id)
        return _volver(propuesta_id,
                       error="No se pudo generar la cotización. Inténtalo de nuevo.")
    return FileResponse(ruta, filename=f"cotizacion-{propuesta_id}.pdf",
                        media_type="application/pdf")


# ─── Formatos de la entidad (SC y DJ en su propio PDF) ───

def _salida(nombre: str) -> str:
    from shared.pdf_firmable import SALIDA
    SALIDA.mkdir(parents=True, exist_ok=True)
    return str(SALIDA / nombre)


def _escribir(nombre: str, contenido: bytes) -> str:
    destino = _salida(nombre)
    with open(destino, "wb") as f:
        f.write(contenido)
    return destino


async def _imagenes(empresa_id: int, modo: str) -> dict:
    """Firma y sello solo en la descarga "con firma escaneada". Para firmar con
    DNIe el documento sale limpio: ReFirma pone su propia firma."""
    if (modo or "dnie").lower() != "escaneada":
        return {}
    from shared.archivos import rutas_de
    return await rutas_de(empresa_id)


@router.post("/propuestas/{propuesta_id}/formato")
async def subir_formato(request: Request, propuesta_id: int, tipo: str = Form(...),
                        archivo: UploadFile = File(...)):  # noqa: B008
    """Sube la SC o la DJ tal como la mando la entidad, para devolverla llena."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _propuesta_del_usuario(propuesta_id, usuario["id"]):
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)
    if tipo not in llenar_formatos.TIPOS:
        return _volver(propuesta_id, error="Tipo de formato desconocido")
    contenido = await archivo.read(llenar_formatos.MAX_BYTES + 1)
    try:
        llenar_formatos.guardar(propuesta_id, tipo, contenido)
    except llenar_formatos.FormatoNoReconocido as e:
        return _volver(propuesta_id, error=str(e))
    return _volver(propuesta_id, aviso=(
        f"{llenar_formatos.TIPOS[tipo]} cargada: al descargarla sale con su formato, ya llena."))


@router.post("/propuestas/{propuesta_id}/formato/{tipo}/borrar")
async def borrar_formato(request: Request, propuesta_id: int, tipo: str):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await _propuesta_del_usuario(propuesta_id, usuario["id"]):
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)
    if tipo in llenar_formatos.TIPOS:
        llenar_formatos.borrar(propuesta_id, tipo)
    return _volver(propuesta_id, aviso="Formato quitado: se usará el nuestro.")


# ─── Envio por el portal de la entidad ───────────────────
#
# El formulario "Enviar cotizacion" del portal del GOREMAD lleva reCAPTCHA: el
# envio lo hace el usuario, a mano, y no se intenta automatizar (seria saltarse
# un control anti-bot de un sistema del Estado; ademas el VPS no alcanza ese
# portal). Lo que si se hace es dejarle todo listo: los valores de cada campo
# para copiar y un ZIP con los adjuntos ya firmados y sellados.

def _envio_portal(prop: dict, empresa: dict, condiciones: dict, total) -> dict | None:
    """Lo que el usuario copia en el formulario de la entidad, y donde esta.

    Cada portal pide cosas distintas. El del GOREMAD tiene una pagina de envio
    por solicitud ("/solicitud/<id>/enviar") con captcha. El de la
    Municipalidad de Tambopata recibe la postulacion en una ventana de su lista
    de convocatorias, que no se puede enlazar: su buscador no lee la URL. Se da
    el portal y el numero de E/M para pegar en su buscador.
    """
    if not empresa:
        return None
    ruc = empresa.get("ruc") or ""
    if prop.get("fuente") == "mpt_tambopata":
        from radar_bot.scrapers.mpt_tambopata import PORTAL, numero_em
        return {
            "mpt": True,
            "enviar": PORTAL,
            "boton": "Abrir el portal de la Municipalidad",
            "buscar": numero_em(prop.get("nomenclatura")),
            "campos": [
                ("Buscar (pégalo en su buscador)", numero_em(prop.get("nomenclatura"))),
                ("Número de RUC", ruc),
                ("Correo Electrónico", empresa.get("email") or ""),
                ("Nombre y/o Razón Social", empresa.get("razon_social") or ""),
            ],
            "paso_final": "En la fila de esa convocatoria pulsa «Postular», sube el ZIP en "
                          "«Cargar archivo» (hasta 40 MB) y pulsa <b>Enviar propuesta</b>.",
            "nota": "Solo se puede postular una vez por proceso: revisa el ZIP antes de enviar. "
                    "La municipalidad confirma por correo.",
        }
    url = prop.get("lic_url") or ""
    if "/solicitud/" not in url:
        return None
    observaciones = "Los precios incluyen I.G.V."
    if total is not None:
        observaciones += f" Total de la cotización: S/ {cotizacion.monto(total)}."
    campos = [
        ("Tipo de persona", "Jurídica" if ruc.startswith("20") else "Natural"),
        ("Nro de RUC", ruc),
        ("Nombres o Razón Social", empresa.get("razon_social") or ""),
        ("Correo", empresa.get("email") or ""),
        ("Teléfono", empresa.get("telefono") or ""),
        ("Dirección", empresa.get("direccion") or ""),
        ("Plazo de Entrega / Ejecución del Servicio", condiciones.get("plazo_entrega") or ""),
        ("Validez de la cotización", condiciones.get("validez") or ""),
        ("Observaciones", observaciones),
    ]
    return {"enviar": url.rstrip("/") + "/enviar", "ficha": url, "campos": campos,
            "boton": "Abrir «Enviar cotización»",
            "paso_final": "En «Archivos anexos» sube los PDF del ZIP, marca «No soy un robot» "
                          "y pulsa <b>Guardar</b>.",
            "nota": "El último paso lo haces tú: el formulario de la entidad tiene captcha y "
                    "nosotros no lo saltamos."}


def _anexos_de(empresa_id: int) -> dict[str, bool]:
    """Que PDF de la empresa hay para adjuntar: la ficha RUC y el RNP se guardan
    cuando el usuario los sube para rellenar la empresa."""
    from shared import constancia_rnp, ficha_ruc
    return {"ficha": ficha_ruc.ruta_pdf(empresa_id).exists(),
            "rnp": constancia_rnp.ruta_pdf(empresa_id).exists()}


async def _anexo(prop, tipo: str, imagenes: dict) -> bytes | None:
    """Un adjunto de la cotizacion, firmado y sellado en cada hoja."""
    from shared import constancia_rnp, ficha_ruc
    if tipo == "eett":
        return await llenar_formatos.eett_selladas(prop["id"], prop["licitacion_id"], imagenes)
    ruta = (ficha_ruc if tipo == "ficha" else constancia_rnp).ruta_pdf(prop["emp_id"])
    if not ruta.exists():
        return None
    contenido = await asyncio.to_thread(ruta.read_bytes)
    try:
        return await asyncio.to_thread(llenar_formatos.sellar_paginas, contenido, imagenes)
    except llenar_formatos.FormatoNoReconocido as e:
        nombre = "la ficha RUC" if tipo == "ficha" else "la constancia del RNP"
        raise llenar_formatos.FormatoNoReconocido(
            f"{nombre} que subiste no se pudo sellar ({e}). Vuelve a subirla en la empresa.") from e


_ANEXOS = {"ficha": "03_Ficha_RUC.pdf", "rnp": "04_Constancia_RNP.pdf",
           "eett": "05_Especificaciones_Tecnicas.pdf"}
# El portal del GOREMAD pide cuatro: cotizacion, DJ de postor, RNP y ficha
# tecnica (las EETT de la entidad, selladas). La consulta RUC va dentro del
# RNP, en el mismo PDF y despues de la constancia: asi lo arma el usuario.
_ANEXOS_GOREMAD = ((("rnp", "ficha"), "03_Registro_Nacional_de_Proveedores_y_Consulta_RUC.pdf"),
                   (("eett",), "04_Ficha_Tecnica.pdf"))


def _unir_pdfs(partes: list[bytes]) -> bytes:
    import io

    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for p in partes:
        w.append(PdfReader(io.BytesIO(p)))
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@router.get("/propuestas/{propuesta_id}/anexo/{tipo}")
async def descargar_anexo(request: Request, propuesta_id: int, tipo: str):
    """La ficha RUC, la constancia del RNP o las EETT, selladas en cada hoja."""
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if tipo not in _ANEXOS:
        return _volver(propuesta_id, error="Ese adjunto no existe.")
    prop = await _propuesta_del_usuario(propuesta_id, usuario["id"])
    if not prop:
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)
    try:
        contenido = await _anexo(prop, tipo, await _imagenes(prop["emp_id"], "escaneada"))
    except llenar_formatos.FormatoNoReconocido as e:
        return _volver(propuesta_id, error=f"No pudimos sellar el adjunto: {e}")
    if contenido is None:
        return _volver(propuesta_id, error="Ese adjunto todavía no está: súbelo en la empresa "
                       "o espera a que el puente baje las EETT.")
    destino = _escribir(f"anexo-{propuesta_id}-{tipo}.pdf", contenido)
    return FileResponse(destino, filename=_ANEXOS[tipo], media_type="application/pdf")


# Cuando el usuario firma a mano la cotizacion y los anexos, el envio se arma
# con sus escaneos y no con lo que genera el sistema: ese ZIP se deja aqui y
# la descarga lo sirve tal cual.
PAQUETES = Path(os.getenv("PAQUETES_DIR", "data/paquetes"))


def paquete_armado(propuesta_id: int) -> Path | None:
    ruta = PAQUETES / f"propuesta_{int(propuesta_id)}.zip"
    return ruta if ruta.exists() else None


def _contenido_zip(ruta: Path) -> list[str]:
    import zipfile
    with zipfile.ZipFile(ruta) as z:
        return sorted(z.namelist())


async def _paquete_mpt(prop, empresa: dict, items: list[dict], condiciones: dict,
                      cci: str | None, imagenes: dict) -> list[tuple[str, bytes]]:
    """La postulacion a la Municipalidad de Tambopata, en el orden que piden:
    la SC del SIGA llena, los anexos 05 a 09 llenos, y lo que acredita al
    postor (ficha RUC, RNP) con el TDR, todo firmado y sellado.

    La SC es obligatoria: sin ella no hay oferta. Los demas van si estan."""
    from radar_bot.scrapers.mpt_tambopata import numero_em
    from shared import anexos_mpt, fechas

    sc = await llenar_formatos.sc_llena(prop["id"], prop["licitacion_id"], empresa, items,
                                        condiciones, imagenes)
    if sc is None:
        raise llenar_formatos.FormatoNoReconocido(
            "la municipalidad no publicó la solicitud de cotización de esta convocatoria.")
    archivos = [("01_Solicitud_de_Cotizacion.pdf", sc)]
    emp_id = prop["emp_id"]
    datos = {
        "empresa": empresa, "items": items, "condiciones": condiciones,
        "total": cotizacion.total(items), "referencia": numero_em(prop["nomenclatura"]),
        "cci": cci, "banco": await kb_get(emp_id, "financiero", "entidad_bancaria"),
        "partida": empresa.get("partida_registral")
                   or await kb_get(emp_id, "legal", "partida_registral"),
        "hoy": fechas.hoy(), "imagenes": imagenes,
    }
    anexos, aviso = await anexos_mpt.anexos_pdf(datos)
    if aviso:
        log.warning("Paquete MPT %s: %s", prop["id"], aviso)
    for k, (nombre, contenido) in enumerate(sorted(anexos.items()), start=2):
        archivos.append((f"{k:02d}_{nombre}", contenido))
    siguiente = len(archivos) + 1
    for tipo, rotulo in (("ficha", "Ficha_RUC"), ("rnp", "Constancia_RNP"),
                         ("eett", "TDR_Especificaciones_Tecnicas")):
        contenido = await _anexo(prop, tipo, imagenes)
        if contenido is not None:
            archivos.append((f"{siguiente:02d}_{rotulo}.pdf", contenido))
            siguiente += 1
    return archivos


@router.get("/propuestas/{propuesta_id}/paquete")
async def paquete_envio(request: Request, propuesta_id: int):
    """ZIP con todo lo que se adjunta en el portal, como se manda en papel:
    cotizacion y DJ llenas, mas ficha RUC, constancia del RNP y EETT de la
    entidad, todo con la firma y el sello escaneados en cada hoja."""
    import io
    import zipfile

    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    prop = await _propuesta_del_usuario(propuesta_id, usuario["id"])
    if not prop:
        return RedirectResponse("/propuestas?error=Esa+propuesta+no+es+tuya",
                                status_code=303)
    nombre = (prop["nomenclatura"] or f"propuesta-{propuesta_id}").replace("/", "-")
    armado = paquete_armado(propuesta_id)
    if armado:
        prefijo = "Postulacion" if prop["fuente"] == "mpt_tambopata" else "Envio"
        return FileResponse(armado, filename=f"{prefijo}_{nombre}.zip",
                            media_type="application/zip")
    items = await cotizacion.items_de(propuesta_id)
    if not items or any(it.get("precio_unitario") is None for it in items):
        return _volver(propuesta_id, error="Pon precio a todos los ítems antes de armar el envío.")
    async with connection() as conn:
        empresa = dict(await conn.fetchrow("SELECT * FROM empresas WHERE id=$1",
                                           prop["emp_id"]))
    imagenes = await _imagenes(prop["emp_id"], "escaneada")
    cci = await kb_get(prop["emp_id"], "financiero", "cci")
    condiciones = await cotizacion.condiciones_de(propuesta_id)

    archivos: list[tuple[str, bytes]] = []
    if prop["fuente"] == "mpt_tambopata":
        try:
            archivos = await _paquete_mpt(prop, empresa, items, condiciones, cci, imagenes)
        except llenar_formatos.FormatoNoReconocido as e:
            return _volver(propuesta_id, error=f"No pudimos llenar el formato de la entidad: {e}")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, contenido in archivos:
                z.writestr(n, contenido)
        destino = _escribir(f"envio-{propuesta_id}.zip", buf.getvalue())
        return FileResponse(destino, filename=f"Postulacion_{nombre}.zip",
                            media_type="application/zip")
    try:
        sc = await llenar_formatos.sc_llena(propuesta_id, prop["licitacion_id"], empresa,
                                            items, condiciones, imagenes)
        if sc is None:
            ruta = await generar_cotizacion(dict(prop), empresa, items, condiciones, cci,
                                            con_dnie=False)
            sc = await asyncio.to_thread(Path(ruta).read_bytes)
        archivos.append(("01_Cotizacion.pdf", sc))
        dj = await llenar_formatos.dj_llena(propuesta_id, prop["licitacion_id"], empresa,
                                            cci, imagenes)
        if dj is None:
            ruta = await generar_dj(dict(prop), empresa, cci, False)
            dj = await asyncio.to_thread(Path(ruta).read_bytes)
        archivos.append(("02_Declaracion_Jurada_de_Postor.pdf", dj))
        for tipos, nombre_pdf in _ANEXOS_GOREMAD:
            partes = [c for t in tipos if (c := await _anexo(prop, t, imagenes)) is not None]
            if partes:
                contenido = partes[0] if len(partes) == 1 else await asyncio.to_thread(
                    _unir_pdfs, partes)
                archivos.append((nombre_pdf, contenido))
    except llenar_formatos.FormatoNoReconocido as e:
        return _volver(propuesta_id, error=f"No pudimos llenar el formato de la entidad: {e}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, contenido in archivos:
            z.writestr(n, contenido)
    destino = _escribir(f"envio-{propuesta_id}.zip", buf.getvalue())
    return FileResponse(destino, filename=f"Envio_{nombre}.zip", media_type="application/zip")
