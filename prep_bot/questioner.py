"""Questioner — Preguntador inteligente que aprende del usuario.

La clave del sistema: pregunta UNA vez, guarda para SIEMPRE.
Después de 10-15 licitaciones, el bot ya sabe TODO.
"""
import logging
from shared.db import connection, kb_get, kb_set, get_empresa

log = logging.getLogger("prep.questioner")


# Campos estándar que se necesitan para TODA licitación
CAMPOS_EMPRESA = [
    # (categoria_kb, clave_kb, pregunta, campo_tabla_empresas)
    ("legal", "representante_legal", "Cual es el nombre completo del representante legal?", "representante_legal"),
    ("legal", "dni_representante", "Cual es el DNI del representante legal?", "dni_representante"),
    ("legal", "cargo_representante", "Cual es el cargo del representante legal (ej: Gerente General)?", None),
    ("legal", "partida_registral", "Cual es el numero de partida registral en SUNARP?", "partida_registral"),
    ("legal", "domicilio_legal", "Cual es el domicilio legal completo de la empresa?", "direccion"),
    ("legal", "telefono_empresa", "Telefono de contacto de la empresa?", "telefono"),
    ("legal", "email_empresa", "Email oficial de la empresa?", "email"),
    ("legal", "vigencia_poder", "Fecha de la vigencia de poder del representante legal?", None),
    ("financiero", "entidad_bancaria", "En que banco tiene cuenta corriente la empresa?", None),
    ("financiero", "cuenta_corriente", "Numero de cuenta corriente? (para pagos del contrato)", None),
    ("financiero", "cci", "Codigo de cuenta interbancario (CCI) de 20 digitos?", None),
]


async def generar_preguntas_propuesta(propuesta_id: int, empresa_id: int, requisitos_bases: dict = None) -> list[dict]:
    """Genera preguntas para lo que falta.

    1. Revisa KB para cada campo
    2. Revisa tabla empresas
    3. Lo que no encuentre → genera pregunta
    4. Preguntas se guardan en tabla `preguntas`

    Returns: lista de preguntas creadas
    """
    empresa = await get_empresa(empresa_id)
    preguntas_creadas = []
    campos_completos = 0
    total_campos = len(CAMPOS_EMPRESA)

    async with connection() as conn:
        for cat, clave, pregunta_text, campo_empresa in CAMPOS_EMPRESA:
            # 1. Buscar en KB
            valor = await kb_get(empresa_id, cat, clave)
            if valor:
                campos_completos += 1
                continue

            # 2. Buscar en tabla empresas
            if campo_empresa and empresa and empresa.get(campo_empresa):
                await kb_set(empresa_id, cat, clave, str(empresa[campo_empresa]), "tabla_empresas")
                campos_completos += 1
                continue

            # 3. Crear pregunta
            preg_id = await conn.fetchval(
                """INSERT INTO preguntas
                (propuesta_id, empresa_id, campo_requerido, pregunta,
                 kb_categoria, kb_clave, contexto)
                VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
                propuesta_id, empresa_id, clave, pregunta_text, cat, clave,
                f"Necesario para completar anexos de la propuesta #{propuesta_id}",
            )
            preguntas_creadas.append({
                "id": preg_id, "campo": clave,
                "pregunta": pregunta_text, "categoria": cat,
            })

        # Preguntas adicionales basadas en requisitos de las bases
        if requisitos_bases:
            extras = await _generar_preguntas_especificas(
                propuesta_id, empresa_id, requisitos_bases, conn
            )
            preguntas_creadas.extend(extras)
            total_campos += len(extras)

        # Actualizar propuesta
        await conn.execute(
            """UPDATE propuestas SET
            anexos_completados=$2, anexos_totales=$3,
            preguntas_pendientes=$4,
            estado=$5
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


async def _generar_preguntas_especificas(propuesta_id: int, empresa_id: int,
                                          requisitos: dict, conn) -> list[dict]:
    """Genera preguntas específicas basadas en las bases de la licitación."""
    preguntas = []

    # Equipo técnico requerido
    equipo_req = requisitos.get("equipo_tecnico_requerido", [])
    for req in equipo_req:
        cargo = req.get("cargo", "profesional")
        clave = f"equipo_{cargo.lower().replace(' ', '_')}"

        valor = await kb_get(empresa_id, "equipo", clave)
        if valor:
            continue

        # Buscar en equipo_tecnico
        titulo_req = req.get("titulo", "")
        match = await conn.fetchrow(
            """SELECT nombre, titulo_profesional FROM equipo_tecnico
            WHERE empresa_id=$1 AND disponible=TRUE
            AND (titulo_profesional ILIKE $2 OR especialidad ILIKE $2)
            LIMIT 1""",
            empresa_id, f"%{titulo_req}%",
        )
        if match:
            await kb_set(empresa_id, "equipo", clave,
                         f"{match['nombre']} | {match['titulo_profesional']}", "tabla_equipo")
            continue

        anos = req.get("experiencia_anos", 0)
        pregunta_text = (
            f"Las bases requieren un {cargo} con titulo en {titulo_req}"
            f"{f' y {anos} anos de experiencia' if anos else ''}. "
            f"Quien sera? Necesito nombre completo, titulo profesional, colegiatura y anos de experiencia."
        )
        preg_id = await conn.fetchval(
            """INSERT INTO preguntas
            (propuesta_id, empresa_id, campo_requerido, pregunta, kb_categoria, kb_clave, contexto)
            VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            propuesta_id, empresa_id, clave, pregunta_text, "equipo", clave,
            f"Requisito de bases: {cargo}",
        )
        preguntas.append({"id": preg_id, "campo": clave, "pregunta": pregunta_text, "categoria": "equipo"})

    # Experiencia requerida
    exp_req = requisitos.get("experiencia_requerida", {})
    if exp_req:
        clave = "experiencia_principal"
        valor = await kb_get(empresa_id, "experiencia", clave)
        if not valor:
            desc = exp_req.get("descripcion", "experiencia similar")
            monto_min = exp_req.get("monto_minimo", 0)
            pregunta_text = (
                f"Las bases requieren experiencia en: {desc}. "
                f"{'Monto minimo acumulado: S/' + str(monto_min) + '. ' if monto_min else ''}"
                f"Tienes contratos similares? Indica cuales con sus montos."
            )
            preg_id = await conn.fetchval(
                """INSERT INTO preguntas
                (propuesta_id, empresa_id, campo_requerido, pregunta, kb_categoria, kb_clave, contexto)
                VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
                propuesta_id, empresa_id, clave, pregunta_text, "experiencia", clave,
                "Experiencia requerida por las bases",
            )
            preguntas.append({"id": preg_id, "campo": clave, "pregunta": pregunta_text, "categoria": "experiencia"})

    return preguntas


async def procesar_respuesta(pregunta_id: int, respuesta: str) -> dict:
    """Procesa la respuesta del usuario, guarda en KB, actualiza propuesta."""
    async with connection() as conn:
        preg = await conn.fetchrow("SELECT * FROM preguntas WHERE id=$1", pregunta_id)
        if not preg:
            return {"error": "Pregunta no encontrada"}

        # Marcar respondida
        await conn.execute(
            "UPDATE preguntas SET respuesta=$2, respondida=TRUE, responded_at=NOW() WHERE id=$1",
            pregunta_id, respuesta,
        )

        # Guardar en KB
        if preg["kb_categoria"] and preg["kb_clave"]:
            await kb_set(preg["empresa_id"], preg["kb_categoria"], preg["kb_clave"], respuesta)
            await conn.execute("UPDATE preguntas SET guardada_en_kb=TRUE WHERE id=$1", pregunta_id)

        # Actualizar conteo de preguntas pendientes
        pendientes = await conn.fetchval(
            "SELECT COUNT(*) FROM preguntas WHERE propuesta_id=$1 AND respondida=FALSE",
            preg["propuesta_id"],
        )
        estado = "listo" if pendientes == 0 else "preguntas_pendientes"
        await conn.execute(
            "UPDATE propuestas SET preguntas_pendientes=$2, estado=$3 WHERE id=$1",
            preg["propuesta_id"], pendientes, estado,
        )

    return {
        "guardado": True,
        "pendientes": pendientes,
        "completado": pendientes == 0,
        "kb_key": f"{preg['kb_categoria']}/{preg['kb_clave']}",
    }
