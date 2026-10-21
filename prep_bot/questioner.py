"""Questioner — Preguntador inteligente que aprende del usuario.

La clave del sistema: pregunta UNA vez, guarda para SIEMPRE.
Después de 10-15 licitaciones, el bot ya sabe TODO.

NUNCA DOS CONEXIONES A LA VEZ

  `kb_get` y `kb_set` piden su propia conexion al pool. Llamarlas DENTRO de
  un `async with connection()` retiene una conexion mientras se pide otra: con
  diez peticiones asi a la vez (el pool tiene diez) cada una espera a una
  conexion que solo se libera cuando otra termina, y la app se cuelga sin un
  solo error en el log. Por eso aqui se lee todo primero, se escribe la
  knowledge_base fuera, y solo entonces se abre la conexion de los INSERT.
"""
import logging

from prep_bot.autofill.validator import NO_EXIGIDOS_EN_MENORES
from shared.cotizacion import es_cotizacion_menor
from shared.db import (
    connection,
    get_empresa,
    kb_get,
    kb_para_registro,
    kb_set,
    kb_todos,
)

log = logging.getLogger("prep.questioner")


# Campos estándar que se necesitan para TODA licitación
CAMPOS_EMPRESA = [
    # (categoria_kb, clave_kb, pregunta, campo_tabla_empresas)
    ("legal", "representante_legal", "Cual es el nombre completo del representante legal?", "representante_legal"),
    ("legal", "dni_representante", "Cual es el DNI del representante legal?", "dni_representante"),
    ("legal", "cargo_representante", "Cual es el cargo del representante legal (ej: Gerente General)?", "cargo_representante"),
    ("legal", "partida_registral", "Cual es el numero de partida registral en SUNARP?", "partida_registral"),
    ("legal", "domicilio_legal", "Cual es el domicilio legal completo de la empresa?", "direccion"),
    ("legal", "telefono_empresa", "Telefono de contacto de la empresa?", "telefono"),
    ("legal", "email_empresa", "Email oficial de la empresa?", "email"),
    ("legal", "vigencia_poder", "Fecha de la vigencia de poder del representante legal?", None),
    ("financiero", "entidad_bancaria", "En que banco tiene cuenta corriente la empresa?", None),
    ("financiero", "cuenta_corriente", "Numero de cuenta corriente? (para pagos del contrato)", None),
    ("financiero", "cci", "Codigo de cuenta interbancario (CCI) de 20 digitos?", None),
]


async def cerrar_las_ya_resueltas(propuesta_id: int) -> int:
    """Da por respondidas las preguntas abiertas cuyo dato ya se sabe.

    Las preguntas se crean al abrir la propuesta y no se miraban mas. Si el
    dato llegaba despues por otro camino -- otra propuesta de la misma
    empresa, la ficha de la empresa, una carga directa a la base de
    conocimiento --, la pregunta seguia pidiendolo para siempre: la
    propuesta 1 del Minimarket pedia el CCI que la DJ ya imprimia.

    Devuelve cuantas cerro o retiro. La respuesta registrada es el dato
    conocido.
    """
    del_empresa = {clave: campo for _, clave, _, campo in CAMPOS_EMPRESA if campo}
    async with connection() as conn:
        # Lo que una contratacion menor no exige no se pide (ver
        # generar_preguntas_propuesta). Las preguntas creadas antes de esa
        # regla se retiran: no hay dato que responder.
        tipo = await conn.fetchval(
            """SELECT l.tipo FROM propuestas p JOIN licitaciones l ON l.id = p.licitacion_id
                WHERE p.id=$1""", propuesta_id)
        retiradas = 0
        if es_cotizacion_menor(tipo):
            retiradas = len(await conn.fetch(
                """DELETE FROM preguntas WHERE propuesta_id=$1 AND respondida=FALSE
                       AND kb_clave = ANY($2::text[]) RETURNING id""",
                propuesta_id, sorted(NO_EXIGIDOS_EN_MENORES)))
        abiertas = await conn.fetch(
            """SELECT id, empresa_id, kb_categoria, kb_clave FROM preguntas
                WHERE propuesta_id=$1 AND respondida=FALSE
                  AND kb_categoria IS NOT NULL AND kb_clave IS NOT NULL""", propuesta_id)
    empresa = await get_empresa(abiertas[0]["empresa_id"]) if abiertas else None
    cerradas = 0
    for preg in abiertas:
        valor = await kb_get(preg["empresa_id"], preg["kb_categoria"], preg["kb_clave"])
        campo = del_empresa.get(preg["kb_clave"])
        if not valor and campo and empresa and empresa.get(campo):
            valor = str(empresa[campo])
            await kb_set(preg["empresa_id"], preg["kb_categoria"], preg["kb_clave"], valor,
                         "tabla_empresas")
        if not valor:
            continue
        async with connection() as conn:
            await conn.execute(
                """UPDATE preguntas SET respuesta=$2, respondida=TRUE, responded_at=NOW(),
                          guardada_en_kb=TRUE WHERE id=$1""", preg["id"],
                kb_para_registro(preg["kb_categoria"], preg["kb_clave"], valor))
        cerradas += 1
    if cerradas or retiradas:
        async with connection() as conn:
            pendientes = await conn.fetchval(
                "SELECT COUNT(*) FROM preguntas WHERE propuesta_id=$1 AND respondida=FALSE",
                propuesta_id)
            await conn.execute(
                """UPDATE propuestas SET preguntas_pendientes=$2,
                          estado = CASE WHEN $2 = 0 AND estado = 'preguntas_pendientes'
                                        THEN 'listo' ELSE estado END
                    WHERE id=$1""", propuesta_id, pendientes)
    return cerradas + retiradas


async def generar_preguntas_propuesta(propuesta_id: int, empresa_id: int, requisitos_bases: dict | None = None) -> list[dict]:
    """Genera preguntas para lo que falta.

    1. Revisa KB para cada campo
    2. Revisa tabla empresas
    3. Lo que no encuentre → genera pregunta
    4. Preguntas se guardan en tabla `preguntas`

    Returns: lista de preguntas creadas
    """
    empresa = await get_empresa(empresa_id)
    sabido = await kb_todos(empresa_id)
    async with connection() as conn:
        tipo = await conn.fetchval(
            """SELECT l.tipo FROM propuestas p JOIN licitaciones l ON l.id = p.licitacion_id
                WHERE p.id=$1""", propuesta_id)
        equipo = (await conn.fetch(
            """SELECT nombre_completo, titulo_profesional, especialidad FROM equipo_tecnico
                WHERE empresa_id=$1 AND disponible=TRUE""", empresa_id)
            if requisitos_bases else [])

    # Lo que una contratacion menor no pide no se pregunta: el usuario de
    # una cotizacion de abarrotes no tiene por que buscar su partida
    # registral para algo que la entidad no va a mirar.
    campos = [c for c in CAMPOS_EMPRESA
              if not (es_cotizacion_menor(tipo) and c[1] in NO_EXIGIDOS_EN_MENORES)]
    total_campos = len(campos)
    campos_completos = 0
    por_aprender: list[tuple[str, str, str, str]] = []
    por_preguntar: list[dict] = []
    for cat, clave, pregunta_text, campo_empresa in campos:
        # 1. Buscar en KB
        if sabido.get((cat, clave)):
            campos_completos += 1
            continue
        # 2. Buscar en tabla empresas
        if campo_empresa and empresa and empresa.get(campo_empresa):
            por_aprender.append((cat, clave, str(empresa[campo_empresa]), "tabla_empresas"))
            campos_completos += 1
            continue
        # 3. Crear pregunta
        por_preguntar.append({
            "campo": clave, "pregunta": pregunta_text, "categoria": cat,
            "contexto": f"Necesario para completar anexos de la propuesta #{propuesta_id}"})

    # Preguntas adicionales basadas en requisitos de las bases
    if requisitos_bases:
        extras = _preguntas_especificas(requisitos_bases, sabido, equipo, por_aprender)
        por_preguntar.extend(extras)
        total_campos += len(extras)

    # La knowledge_base se escribe sin ninguna conexion tomada: kb_set pide la suya.
    for cat, clave, valor, fuente in por_aprender:
        await kb_set(empresa_id, cat, clave, valor, fuente)

    preguntas_creadas = []
    async with connection() as conn:
        for preg in por_preguntar:
            preg_id = await conn.fetchval(
                """INSERT INTO preguntas
                (propuesta_id, empresa_id, campo_requerido, pregunta,
                 kb_categoria, kb_clave, contexto)
                VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
                propuesta_id, empresa_id, preg["campo"], preg["pregunta"],
                preg["categoria"], preg["campo"], preg["contexto"],
            )
            preguntas_creadas.append({
                "id": preg_id, "campo": preg["campo"],
                "pregunta": preg["pregunta"], "categoria": preg["categoria"],
            })

        # Actualizar propuesta
        await conn.execute(
            """UPDATE propuestas SET
            anexos_completados=$2, anexos_totales=$3,
            preguntas_pendientes=$4,
            estado = CASE WHEN estado IN ('enviado','ganada','adjudicado','cancelado') THEN estado ELSE $5 END
            WHERE id=$1""",
            propuesta_id, campos_completos, total_campos,
            len(preguntas_creadas),
            "preguntas_pendientes" if preguntas_creadas else "listo",
        )

    log.info(
        f"Propuesta #{propuesta_id}: {campos_completos}/{total_campos} completos, "
        f"{len(preguntas_creadas)} preguntas nuevas"
    )
    return preguntas_creadas


def _preguntas_especificas(requisitos: dict, sabido: dict, equipo: list,
                           por_aprender: list) -> list[dict]:
    """Preguntas especificas basadas en las bases de la licitacion.

    No toca la base: lee de `sabido` (la knowledge_base ya leida) y de `equipo`
    (los profesionales disponibles), y deja en `por_aprender` lo que aprende.
    Quien llama escribe y crea las preguntas.
    """
    preguntas = []

    # Equipo técnico requerido
    for req in requisitos.get("equipo_tecnico_requerido", []):
        cargo = req.get("cargo", "profesional")
        clave = f"equipo_{cargo.lower().replace(' ', '_')}"
        if sabido.get(("equipo", clave)):
            continue

        # Buscar en equipo_tecnico, como hacia el ILIKE '%titulo%'.
        titulo_req = req.get("titulo", "")
        aguja = titulo_req.lower()
        match = next((m for m in equipo
                      if aguja in (m["titulo_profesional"] or "").lower()
                      or aguja in (m["especialidad"] or "").lower()), None)
        if match:
            por_aprender.append((
                "equipo", clave,
                f"{match['nombre_completo']} | {match['titulo_profesional']}", "tabla_equipo"))
            continue

        anos = req.get("experiencia_anos", 0)
        pregunta_text = (
            f"Las bases requieren un {cargo} con titulo en {titulo_req}"
            f"{f' y {anos} anos de experiencia' if anos else ''}. "
            f"Quien sera? Necesito nombre completo, titulo profesional, colegiatura y anos de experiencia."
        )
        preguntas.append({"campo": clave, "pregunta": pregunta_text, "categoria": "equipo",
                          "contexto": f"Requisito de bases: {cargo}"})

    # Experiencia requerida
    exp_req = requisitos.get("experiencia_requerida", {})
    if exp_req and not sabido.get(("experiencia", "experiencia_principal")):
        desc = exp_req.get("descripcion", "experiencia similar")
        monto_min = exp_req.get("monto_minimo", 0)
        pregunta_text = (
            f"Las bases requieren experiencia en: {desc}. "
            f"{'Monto minimo acumulado: S/' + str(monto_min) + '. ' if monto_min else ''}"
            f"Tienes contratos similares? Indica cuales con sus montos."
        )
        preguntas.append({"campo": "experiencia_principal", "pregunta": pregunta_text,
                          "categoria": "experiencia",
                          "contexto": "Experiencia requerida por las bases"})

    return preguntas


async def procesar_respuesta(pregunta_id: int, respuesta: str) -> dict:
    """Procesa la respuesta del usuario, guarda en KB, actualiza propuesta.

    En tres tramos y no dentro de una sola conexion: `kb_set` pide la suya, y
    pedirla con otra tomada es lo que agota el pool (ver arriba)."""
    async with connection() as conn:
        preg = await conn.fetchrow("SELECT * FROM preguntas WHERE id=$1", pregunta_id)
        if not preg:
            return {"error": "Pregunta no encontrada"}

        # Marcar respondida
        await conn.execute(
            "UPDATE preguntas SET respuesta=$2, respondida=TRUE, responded_at=NOW() WHERE id=$1",
            # Del CCI o el DNI solo los ultimos digitos: el dato va cifrado a la KB.
            pregunta_id, kb_para_registro(preg["kb_categoria"], preg["kb_clave"], respuesta),
        )

    # Guardar en KB
    en_kb = bool(preg["kb_categoria"] and preg["kb_clave"])
    if en_kb:
        await kb_set(preg["empresa_id"], preg["kb_categoria"], preg["kb_clave"], respuesta)

    async with connection() as conn:
        if en_kb:
            await conn.execute("UPDATE preguntas SET guardada_en_kb=TRUE WHERE id=$1", pregunta_id)

        # Actualizar conteo de preguntas pendientes
        pendientes = await conn.fetchval(
            "SELECT COUNT(*) FROM preguntas WHERE propuesta_id=$1 AND respondida=FALSE",
            preg["propuesta_id"],
        )
        estado = "listo" if pendientes == 0 else "preguntas_pendientes"
        await conn.execute(
            "UPDATE propuestas SET preguntas_pendientes=$2, estado = CASE WHEN estado IN "
            "('enviado','ganada','adjudicado','cancelado') THEN estado ELSE $3 END WHERE id=$1",
            preg["propuesta_id"], pendientes, estado,
        )

    return {
        "guardado": True,
        "pendientes": pendientes,
        "completado": pendientes == 0,
        "kb_key": f"{preg['kb_categoria']}/{preg['kb_clave']}",
    }
