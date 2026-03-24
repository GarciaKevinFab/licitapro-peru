"""Proposal — Genera propuesta técnica con IA (Claude API)."""
import os
import json
import logging
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
import anthropic

from shared.db import connection, get_empresa
from shared.config import ANTHROPIC_KEY, format_monto
from shared.knowledge_base import obtener_datos_empresa_completos

log = logging.getLogger("prep.autofill.proposal")

TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")


async def generar_propuesta_tecnica(propuesta_id: int, empresa_id: int,
                                     licitacion: dict, datos: dict) -> str | None:
    """Genera propuesta técnica completa usando Claude API.

    Secciones estándar:
    1. Resumen ejecutivo
    2. Objetivos
    3. Alcance del servicio
    4. Metodología
    5. Plan de trabajo
    6. Equipo técnico propuesto
    7. Equipamiento y recursos
    8. Experiencia similar
    9. Cronograma
    10. Valor agregado
    """
    empresa = await get_empresa(empresa_id)
    if not empresa:
        return None

    # Generar contenido con IA o plantilla
    if ANTHROPIC_KEY:
        contenido = await _generar_con_ia(licitacion, empresa, datos)
    else:
        contenido = _generar_plantilla(licitacion, empresa, datos)

    # Crear documento DOCX
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(11)

    # Portada
    doc.add_paragraph()
    doc.add_paragraph()
    titulo = doc.add_heading("PROPUESTA TECNICA", level=0)
    titulo.alignment = WD_ALIGN_PARAGRAPH.CENTER

    subtitulo = doc.add_paragraph()
    subtitulo.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitulo.add_run(f"\n{licitacion.get('nomenclatura', licitacion.get('id', ''))}").font.size = Pt(14)

    doc.add_paragraph()
    info = doc.add_paragraph()
    info.alignment = WD_ALIGN_PARAGRAPH.CENTER
    info.add_run(f"\n{licitacion['objeto'][:200]}").font.size = Pt(12)

    doc.add_paragraph()
    empresa_p = doc.add_paragraph()
    empresa_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    empresa_p.add_run(f"\nPresentada por:\n{empresa['razon_social']}\nRUC: {empresa['ruc']}").font.size = Pt(12)
    doc.add_page_break()

    # Secciones
    for seccion in contenido.get("secciones", []):
        doc.add_heading(seccion["titulo"], level=1)
        for parrafo in seccion.get("contenido", []):
            doc.add_paragraph(parrafo)
        if seccion.get("lista"):
            for item in seccion["lista"]:
                doc.add_paragraph(f"  - {item}")
        doc.add_paragraph()

    # Equipo técnico (tabla)
    if datos.get("equipo"):
        doc.add_heading("EQUIPO TECNICO PROPUESTO", level=1)
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        for i, h in enumerate(["Nombre", "Cargo", "Titulo", "Experiencia"]):
            table.rows[0].cells[i].text = h
        for m in datos["equipo"][:8]:
            row = table.add_row()
            row.cells[0].text = m.get("nombre", "")
            row.cells[1].text = m.get("especialidad", "")
            row.cells[2].text = m.get("titulo_profesional", "")
            row.cells[3].text = f"{m.get('anos_experiencia', 0)} años"

    output_dir = os.path.join(TEMPLATES_DIR, "output")
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"propuesta_tecnica_{propuesta_id}.docx")
    doc.save(path)
    log.info(f"Propuesta técnica generada: {path}")
    return path


async def _generar_con_ia(licitacion: dict, empresa, datos: dict) -> dict:
    """Genera contenido de propuesta técnica con Claude API."""
    equipo_text = ""
    if datos.get("equipo"):
        equipo_text = "\n".join(
            f"- {m['nombre']}: {m.get('titulo_profesional', '')} ({m.get('anos_experiencia', 0)} años)"
            for m in datos["equipo"][:5]
        )

    exp_text = ""
    if datos.get("experiencia"):
        exp_text = "\n".join(
            f"- {e['objeto'][:80]} ({e.get('entidad_contratante', '')}): S/{e.get('monto', 0):,.0f}"
            for e in datos["experiencia"][:5]
        )

    prompt = f"""Genera una propuesta técnica profesional para esta licitación pública peruana.

LICITACIÓN:
- Objeto: {licitacion['objeto']}
- Entidad: {licitacion['entidad']}
- Monto ref: {format_monto(licitacion.get('monto_referencial', 0)) if licitacion.get('monto_referencial') else 'No especificado'}

EMPRESA:
- {empresa['razon_social']} (RUC {empresa['ruc']})
- Rubros: {', '.join(empresa.get('rubros') or [])}

EQUIPO DISPONIBLE:
{equipo_text or 'No registrado'}

EXPERIENCIA:
{exp_text or 'No registrada'}

Genera un JSON con este formato:
{{
    "secciones": [
        {{"titulo": "1. RESUMEN EJECUTIVO", "contenido": ["párrafo1", "párrafo2"]}},
        {{"titulo": "2. OBJETIVOS", "contenido": ["párrafo"], "lista": ["obj1", "obj2"]}},
        {{"titulo": "3. ALCANCE DEL SERVICIO", "contenido": ["párrafo"]}},
        {{"titulo": "4. METODOLOGIA", "contenido": ["párrafo"]}},
        {{"titulo": "5. PLAN DE TRABAJO", "contenido": ["párrafo"], "lista": ["fase1", "fase2"]}},
        {{"titulo": "6. EXPERIENCIA SIMILAR", "contenido": ["párrafo"]}},
        {{"titulo": "7. VALOR AGREGADO", "contenido": ["párrafo"], "lista": ["ventaja1", "ventaja2"]}}
    ]
}}

La propuesta debe ser profesional, específica al objeto de contratación, y destacar las fortalezas de la empresa. Máximo 3-4 párrafos por sección."""

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception as e:
        log.error(f"Error generando con IA: {e}")

    return _generar_plantilla(licitacion, empresa, datos)


def _generar_plantilla(licitacion: dict, empresa, datos: dict) -> dict:
    """Plantilla básica sin IA."""
    return {
        "secciones": [
            {
                "titulo": "1. RESUMEN EJECUTIVO",
                "contenido": [
                    f"{empresa['razon_social']} presenta esta propuesta técnica para la contratación de: "
                    f"{licitacion['objeto']}.",
                    "Nuestra empresa cuenta con la experiencia, el equipo técnico y los recursos "
                    "necesarios para ejecutar el presente servicio dentro de los plazos y condiciones "
                    "establecidos en las bases del procedimiento de selección.",
                ],
            },
            {
                "titulo": "2. OBJETIVOS",
                "contenido": ["Los objetivos de la presente propuesta son:"],
                "lista": [
                    "Cumplir con todos los requerimientos técnicos establecidos en las bases.",
                    "Entregar el servicio/bien dentro del plazo establecido.",
                    "Garantizar la calidad conforme a los estándares solicitados.",
                    "Proporcionar soporte y garantía post-entrega.",
                ],
            },
            {
                "titulo": "3. ALCANCE DEL SERVICIO",
                "contenido": [
                    f"El alcance comprende la totalidad de lo solicitado en el objeto de contratación: "
                    f"{licitacion['objeto'][:200]}.",
                ],
            },
            {
                "titulo": "4. METODOLOGIA",
                "contenido": [
                    "Nuestra metodología se basa en las mejores prácticas del sector, "
                    "con un enfoque estructurado en fases: planificación, ejecución, "
                    "control de calidad y entrega.",
                ],
            },
            {
                "titulo": "5. PLAN DE TRABAJO",
                "contenido": ["El plan de trabajo se estructura en las siguientes fases:"],
                "lista": [
                    "Fase 1: Planificación y coordinación inicial",
                    "Fase 2: Ejecución del servicio/entrega del bien",
                    "Fase 3: Control de calidad y ajustes",
                    "Fase 4: Entrega final y conformidad",
                ],
            },
        ],
    }
