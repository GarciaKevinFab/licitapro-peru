"""Analisis de viabilidad de una licitacion para UNA empresa concreta.

QUE HACIA ESTE ARCHIVO ANTES: NADA

  No tenia un solo importador en todo el proyecto. `planes.analisis_ia` estaba
  en TRUE para Pro (S/99) y Empresa (S/199), el portero `puede_usar_ia` estaba
  escrito y hasta probado, y entre los tres no se ejecutaba ninguno. Se cobraba
  por una casilla de la tabla de planes.

POR QUE SE ANALIZA BAJO PETICION Y NO EN EL SCRAPEO

  El orquestador trae cientos de licitaciones por corrida. Analizarlas todas
  seria pagar por cientos de analisis de los que el cliente mira dos, y ademas
  el analisis depende de la empresa: la misma licitacion da un resultado
  distinto para cada una, asi que no existe un analisis que hacer "una vez".

  El scoring heuristico (`radar_bot/scorer.py`) si corre en cada scrapeo, es
  gratis y ordena la lista. La IA entra cuando alguien abre una ficha y pulsa.

QUE SE QUITO: `analizar_bases_pdf`

  Analizaba el PDF de las bases. Nadie descarga esos PDF: `bases_descargadas`
  no se pone a TRUE en ningun sitio y `BASES_DIR` no lo lee ningun modulo. No
  estaba sin llamar por descuido, era imposible de llamar. Descargar bases del
  SEACE es otra funcion; cuando exista, este analisis vuelve.
"""
import logging

from shared import ia
from shared.db import connection

log = logging.getLogger("radar.analyzer")


# El prompt de sistema no cambia entre llamadas: es lo que se cachea. Lo que
# varia -- la licitacion y la empresa -- viaja en el mensaje del usuario.
SISTEMA = """Eres un especialista en contratacion publica peruana. Evaluas si a \
una empresa concreta le conviene presentarse a un procedimiento de seleccion.

Marco legal aplicable:
- Ley 32069, Ley General de Contrataciones Publicas, vigente desde el 22 de
  abril de 2025, y su reglamento. Sustituye a la Ley 30225.
- El plazo de pago se cuenta en dias habiles desde la CONFORMIDAD, no desde la
  emision de la factura.
- Los procedimientos AS, SIE, CdP y CM tienen menos requisitos y plazos mas
  cortos que LP y CP; para una empresa pequena eso pesa mas que el monto.
- La inscripcion vigente en el RNP es condicion para contratar, y el capitulo
  del RNP debe corresponder al objeto (bienes, servicios, obras, consultoria).

Como evaluas:
- `score_viabilidad` es un entero de 0 a 100.
- El score mide el encaje con ESTA empresa, no la calidad de la licitacion. Una
  licitacion excelente para la que la empresa no acredita experiencia es un
  score bajo, no alto.
- La experiencia acreditada manda sobre la intencion. Si la empresa no tiene
  contratos del rubro, dilo en los riesgos aunque todo lo demas encaje.
- El monto importa por dos motivos opuestos: demasiado alto atrae competencia y
  exige respaldo financiero; demasiado bajo no cubre el costo de preparar el
  expediente.
- Si un dato no esta, no lo inventes: nombralo como informacion que falta.

Escribes en espanol de Peru, directo y sin adornos. Nada de "es importante
senalar" ni listas de obviedades: quien lee decide hoy si prepara un expediente
o no."""


# El rango 0-100 del score se pide en el prompt y no aqui: las salidas
# estructuradas no admiten `minimum` ni `maximum` sobre un entero, y ponerlos
# devuelve un 400 que tumba la llamada entera.
ESQUEMA = {
    "type": "object",
    "properties": {
        "score_viabilidad": {"type": "integer"},
        "resumen": {"type": "string"},
        "requisitos_clave": {"type": "array", "items": {"type": "string"}},
        "riesgos": {"type": "array", "items": {"type": "string"}},
        "fortalezas": {"type": "array", "items": {"type": "string"}},
        "informacion_que_falta": {"type": "array", "items": {"type": "string"}},
        "recomendacion": {"type": "string", "enum": ["licitar", "evaluar", "pasar"]},
        "precio_sugerido_min": {"type": ["number", "null"]},
        "precio_sugerido_max": {"type": ["number", "null"]},
        "competidores_estimados": {"type": "integer"},
        "justificacion_score": {"type": "string"},
    },
    "required": [
        "score_viabilidad", "resumen", "requisitos_clave", "riesgos",
        "fortalezas", "informacion_que_falta", "recomendacion",
        "precio_sugerido_min", "precio_sugerido_max",
        "competidores_estimados", "justificacion_score",
    ],
    "additionalProperties": False,
}


async def analizar(usuario_id: int, empresa_id: int, licitacion: dict) -> dict:
    """Analiza y guarda. Devuelve {resultado, origen, aviso}.

    Nunca lanza por un fallo de la API: si la llamada se cae se guarda el
    heuristico marcado como tal y se dice en el aviso. Dejar la ficha vacia
    porque Anthropic tuvo un mal minuto es peor producto que un analisis
    modesto y honesto sobre su procedencia.

    El tope NO se comprueba aqui, lo comprueba quien llama antes de gastar:
    esta funcion tambien produce el respaldo heuristico, que es gratis y no
    debe consumir cuota.
    """
    if not ia.disponible():
        resultado = heuristico(licitacion)
        await ia.guardar_analisis(usuario_id, empresa_id, licitacion["id"],
                                  resultado, ia.ORIGEN_HEURISTICO)
        return {"resultado": resultado, "origen": ia.ORIGEN_HEURISTICO,
                "aviso": "Sin ANTHROPIC_API_KEY configurada: analisis heuristico."}

    contexto = await _contexto_empresa(empresa_id)
    peticion = _peticion(licitacion, contexto)

    try:
        resultado, uso = await ia.pedir_json(SISTEMA, peticion, ESQUEMA)
    except Exception as e:
        # exc_info a proposito: un except mudo aqui tapo durante semanas un
        # error de esquema, y el analisis nunca llegaba a guardarse.
        log.exception("Analisis IA de %s fallo: %s", licitacion.get("id"), e)
        resultado = heuristico(licitacion)
        await ia.guardar_analisis(usuario_id, empresa_id, licitacion["id"],
                                  resultado, ia.ORIGEN_HEURISTICO)
        return {"resultado": resultado, "origen": ia.ORIGEN_HEURISTICO,
                "aviso": "El analisis con IA no respondio; se muestra el "
                         "heuristico. No se ha descontado de tu cuota."}

    await ia.guardar_analisis(usuario_id, empresa_id, licitacion["id"],
                              resultado, ia.ORIGEN_IA, uso)
    log.info("Analisis IA %s empresa=%s score=%s",
             licitacion["id"], empresa_id, resultado.get("score_viabilidad"))
    return {"resultado": resultado, "origen": ia.ORIGEN_IA, "aviso": ""}


def _peticion(lic: dict, contexto: str) -> str:
    monto = lic.get("monto_referencial")
    return f"""## LICITACION
- Identificador: {lic.get('nomenclatura') or lic['id']}
- Entidad: {lic['entidad']}
- Objeto: {lic['objeto']}
- Monto referencial: {f"S/ {monto:,.2f}" if monto else 'no publicado'}
- Tipo de procedimiento: {lic.get('tipo') or 'no publicado'}
- Departamento: {lic.get('departamento') or 'no publicado'}
- Cierre de ofertas: {lic.get('fecha_cierre') or 'no publicado'}
- Fuente: {lic.get('fuente')}

## EMPRESA
{contexto}

Evalua la viabilidad para esta empresa."""


async def _contexto_empresa(empresa_id: int) -> str:
    """Los datos de la empresa que cambian la respuesta, y solo esos."""
    async with connection() as conn:
        empresa = await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", empresa_id)
        experiencias = await conn.fetch(
            """SELECT objeto_contrato, monto, entidad_contratante
                 FROM experiencia WHERE empresa_id=$1
                ORDER BY monto DESC NULLS LAST LIMIT 5""", empresa_id)
        equipo = await conn.fetch(
            """SELECT nombre_completo, titulo_profesional, especialidad,
                      anos_experiencia
                 FROM equipo_tecnico WHERE empresa_id=$1 AND disponible=TRUE""",
            empresa_id)

    if not empresa:
        return "Empresa no registrada."

    ctx = (f"- Razon social: {empresa['razon_social']}\n"
           f"- RUC: {empresa['ruc']}\n"
           f"- Rubros: {', '.join(empresa['rubros'] or []) or 'sin declarar'}\n")

    if experiencias:
        ctx += "- Experiencia acreditada:\n"
        for e in experiencias:
            monto = f"S/ {e['monto']:,.0f}" if e["monto"] else "monto no registrado"
            ctx += (f"  * {e['objeto_contrato'][:90]} "
                    f"({e['entidad_contratante']}) — {monto}\n")
    else:
        # Se dice explicitamente en vez de omitir la seccion: "no acredita
        # experiencia" es un dato que debe pesar en el score, y una seccion
        # ausente el modelo puede leerla como un olvido nuestro.
        ctx += "- Experiencia acreditada: ninguna registrada.\n"

    if equipo:
        ctx += "- Equipo tecnico disponible:\n"
        for m in equipo:
            ctx += (f"  * {m['nombre_completo']} — {m['titulo_profesional']}, "
                    f"{m['especialidad']} ({m['anos_experiencia']} anos)\n")
    else:
        ctx += "- Equipo tecnico disponible: ninguno registrado.\n"

    return ctx


# ─── Respaldo sin IA ─────────────────────────────────────

def heuristico(lic: dict) -> dict:
    """Analisis por reglas. Gratis, pobre, y honesto sobre ambas cosas.

    Existe para que la ficha no se quede vacia cuando la API falla o no hay
    clave. Se marca como 'heuristico' en la base y se dice en pantalla.
    """
    score = 50
    riesgos, fortalezas = [], []

    monto = lic.get("monto_referencial") or 0
    if 10000 <= monto <= 500000:
        score += 10
        fortalezas.append("Monto en un rango manejable")
    elif monto > 500000:
        score -= 10
        riesgos.append("Monto alto: mas competencia y mas respaldo exigido")

    tipo = lic.get("tipo") or ""
    if tipo in ("AS", "SIE", "CdP", "CM"):
        score += 10
        fortalezas.append(f"Procedimiento simplificado ({tipo})")
    elif tipo in ("LP", "CP"):
        score -= 5
        riesgos.append(f"Procedimiento complejo ({tipo})")

    return {
        "score_viabilidad": max(0, min(100, score)),
        "resumen": f"Analisis por reglas. {(lic.get('objeto') or '')[:120]}",
        "requisitos_clave": ["Revisar las bases para los requisitos concretos"],
        "riesgos": riesgos or ["Sin analisis con IA disponible"],
        "fortalezas": fortalezas,
        "informacion_que_falta": ["El analisis con IA no llego a ejecutarse"],
        "recomendacion": "evaluar",
        "precio_sugerido_min": None,
        "precio_sugerido_max": None,
        "competidores_estimados": 3,
        "justificacion_score": "Reglas fijas sobre monto y tipo de "
                               "procedimiento. No mira a la empresa.",
    }
