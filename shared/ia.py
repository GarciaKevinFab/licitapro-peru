"""El unico sitio que habla con la API de Anthropic.

POR QUE UN MODULO Y NO UN CLIENTE EN CADA SITIO

  Habia dos: `radar_bot/analyzer.py` y `prep_bot/autofill/proposal.py`, cada
  uno con su propia copia del nombre del modelo y ninguno con control de gasto.
  Cuando el modelo cambia hay que acordarse de los dos, y el dia que se olvide
  uno seguira llamando a un modelo retirado hasta que alguien vea el 404.

  Mas importante: el tope de gasto solo funciona si TODO pasa por el mismo
  sitio. Un segundo cliente en otro archivo es un agujero en el tope.

QUIEN PAGA

  La plataforma, con una sola ANTHROPIC_API_KEY. El cliente no configura nada.
  Eso hace que el producto funcione el primer dia, y hace que cada llamada sea
  dinero del dueno: de ahi el tope por plan y el registro de tokens.

EL MODELO Y EL ESFUERZO SE CAMBIAN SIN TOCAR CODIGO

  `LICITAPRO_MODELO_IA` y `LICITAPRO_IA_ESFUERZO` estan en el entorno a
  proposito. El precio por millon de tokens cambia entre modelos, y cuanto
  gastar por analisis es una decision de negocio, no de ingenieria: tiene que
  poder ajustarse mirando la factura, sin un despliegue.

  El esfuerzo sale en 'medium' y no en el 'high' que la API usa por defecto
  porque aqui se analiza el mismo tipo de documento una y otra vez contra un
  esquema de salida fijo. Si los analisis salen pobres, subirlo es una linea
  del .env.

POR QUE SE GUARDA EL ORIGEN DE CADA ANALISIS

  Cuando la API falla se devuelve el heuristico, que no cuesta nada y evita
  dejar la ficha vacia. Pero un cliente del plan Pro que ve un heuristico
  creyendo que es el analisis por el que paga tiene una queja legitima. Por eso
  `origen` viaja hasta la plantilla y se dice en pantalla.
"""
import json
import logging
import os

import anthropic

from shared.config import ANTHROPIC_KEY
from shared.db import connection

log = logging.getLogger("shared.ia")

MODELO = os.getenv("LICITAPRO_MODELO_IA", "claude-opus-5")
ESFUERZO = os.getenv("LICITAPRO_IA_ESFUERZO", "medium")

ORIGEN_IA = "ia"
ORIGEN_HEURISTICO = "heuristico"


def disponible() -> bool:
    """Si hay clave para llamar a la API.

    Se comprueba ANTES de descontar la cuota: sin clave el analisis sale del
    heuristico, y gastar un analisis del tope por devolver el heuristico seria
    cobrarle al cliente lo que no recibio.
    """
    return bool(ANTHROPIC_KEY)


def _cliente() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)


# ─── Cuota ───────────────────────────────────────────────

async def cuota(usuario_id: int) -> dict:
    """Cuantos analisis con IA le quedan a este usuario en el mes en curso.

    Devuelve las tres cifras aunque no pueda usar la IA, porque la ficha las
    pinta para explicar POR QUE no puede: "0 de 0" con el nombre de su plan al
    lado dice mas que un boton desactivado sin motivo.

    El mes es natural (del dia 1 al ultimo) y no una ventana movil de 30 dias:
    es lo que entiende quien lee "60 analisis al mes", y coincide con el
    periodo que factura Anthropic.
    """
    from shared.suscripciones import estado_suscripcion

    susc = await estado_suscripcion(usuario_id)
    por_plan = bool(susc.get("acceso")) and bool(susc.get("analisis_ia"))

    async with connection() as conn:
        tope = await conn.fetchval(
            "SELECT analisis_ia_mes FROM planes WHERE codigo = $1",
            susc.get("plan_codigo"))
        # Solo cuentan los que costaron dinero. Un heuristico devuelto porque
        # la API estaba caida no puede gastar el tope del cliente.
        usados = await conn.fetchval(
            """SELECT COUNT(*) FROM analisis_ia
                WHERE usuario_id = $1 AND origen = $2
                  AND creado_en >= date_trunc('month', CURRENT_DATE)""",
            usuario_id, ORIGEN_IA) or 0

    sin_limite = tope is None
    restantes = None if sin_limite else max(0, tope - usados)
    return {
        "permitido": por_plan and (sin_limite or restantes > 0),
        "por_plan": por_plan,
        "usados": usados,
        "tope": tope,
        "restantes": restantes,
        "plan": susc.get("plan_nombre") or susc.get("plan_codigo"),
    }


# ─── Llamada ─────────────────────────────────────────────

async def pedir_json(sistema: str, peticion: str, esquema: dict,
                     max_tokens: int = 16000) -> tuple[dict, dict]:
    """Una respuesta que cumple `esquema`, y lo que costo.

    Se usa `output_config.format` en vez de buscar las llaves dentro del texto.
    La version anterior hacia `text.find("{")`, y cuando el modelo escribia una
    frase antes del JSON el parseo fallaba: el except devolvia el heuristico en
    silencio y el cliente pagaba un analisis para recibir el de respaldo.

    El prompt de sistema lleva `cache_control` porque es identico en todas las
    llamadas; lo que cambia -- la licitacion y la empresa -- va despues, en el
    mensaje. Si el sistema no alcanza el minimo que la API exige para cachear,
    no se cachea y no pasa nada: por eso se registra `cache_lectura` en el log,
    para poder comprobarlo con datos en vez de suponerlo.
    """
    cliente = _cliente()
    respuesta = await cliente.messages.create(
        model=MODELO,
        max_tokens=max_tokens,
        system=[{
            "type": "text",
            "text": sistema,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": peticion}],
        output_config={
            "effort": ESFUERZO,
            "format": {"type": "json_schema", "schema": esquema},
        },
    )

    if respuesta.stop_reason == "refusal":
        # Esto llega con HTTP 200 y sin bloque de texto. Sin esta comprobacion
        # el next() de abajo lanza StopIteration, y el fallo aparece disfrazado
        # de error de programacion en vez de lo que es.
        detalle = getattr(respuesta.stop_details, "category", None)
        raise RuntimeError(f"La API declino la peticion ({detalle}).")

    if respuesta.stop_reason == "max_tokens":
        # El JSON viene cortado a la mitad. Sin esta comprobacion el fallo
        # aparecia como un JSONDecodeError -- "Unterminated string" -- que
        # parece un modelo que responde mal cuando en realidad es un techo
        # nuestro demasiado bajo. Se diagnostica muy distinto.
        #
        # OJO al calibrarlo: en los modelos con pensamiento adaptativo, lo que
        # el modelo piensa tambien sale de `max_tokens`. Un techo que basta
        # para la respuesta puede no bastar para respuesta mas razonamiento.
        raise RuntimeError(
            f"La respuesta se corto en el limite de {max_tokens} tokens.")

    uso = respuesta.usage
    texto = next(b.text for b in respuesta.content if b.type == "text")
    log.info("IA %s: entrada=%s salida=%s cache_lectura=%s",
             MODELO, uso.input_tokens, uso.output_tokens,
             getattr(uso, "cache_read_input_tokens", 0))

    return json.loads(texto), {
        "modelo": MODELO,
        "tokens_entrada": uso.input_tokens,
        "tokens_salida": uso.output_tokens,
    }


# ─── Persistencia ────────────────────────────────────────

async def guardar_analisis(usuario_id: int, empresa_id: int, licitacion_id: str,
                           resultado: dict, origen: str,
                           uso: dict | None = None) -> None:
    """Deja el analisis vigente de este usuario para esta licitacion.

    Sobrescribe en vez de acumular: al cliente le sirve el ultimo, y una fila
    por cada vez que abre la ficha convertiria la tabla en un log de visitas.
    """
    uso = uso or {}
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO analisis_ia
                   (usuario_id, empresa_id, licitacion_id, score, recomendacion,
                    resultado, origen, modelo, tokens_entrada, tokens_salida)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               ON CONFLICT (usuario_id, empresa_id, licitacion_id) DO UPDATE SET
                   score = EXCLUDED.score,
                   recomendacion = EXCLUDED.recomendacion,
                   resultado = EXCLUDED.resultado,
                   origen = EXCLUDED.origen,
                   modelo = EXCLUDED.modelo,
                   tokens_entrada = EXCLUDED.tokens_entrada,
                   tokens_salida = EXCLUDED.tokens_salida,
                   creado_en = now()""",
            usuario_id, empresa_id, licitacion_id,
            resultado.get("score_viabilidad"),
            resultado.get("recomendacion"),
            json.dumps(resultado), origen,
            uso.get("modelo"), uso.get("tokens_entrada"), uso.get("tokens_salida"),
        )


async def analisis_guardado(usuario_id: int, licitacion_id: str) -> list[dict]:
    """Los analisis que este usuario ya tiene de esta licitacion, uno por empresa.

    Filtra por usuario_id ademas de por licitacion: el aislamiento no puede
    depender de que quien llame pase el id correcto.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT a.*, e.razon_social
                 FROM analisis_ia a
                 JOIN empresas e ON e.id = a.empresa_id
                WHERE a.usuario_id = $1 AND a.licitacion_id = $2
                ORDER BY a.creado_en DESC""",
            usuario_id, licitacion_id)

    salida = []
    for f in filas:
        d = dict(f)
        # asyncpg devuelve JSONB como texto; la plantilla necesita el dict.
        if isinstance(d.get("resultado"), str):
            try:
                d["resultado"] = json.loads(d["resultado"])
            except ValueError:
                d["resultado"] = {}
        salida.append(d)
    return salida


# Tarifa por millon de tokens, en dolares. Sale de la del modelo por defecto y
# se corrige sin desplegar, porque los precios cambian sin avisar a este
# codigo. Los tokens que se guardan NO caducan; el dinero que representan, si.
USD_POR_MILLON_ENTRADA = float(os.getenv("LICITAPRO_IA_USD_ENTRADA", "5"))
USD_POR_MILLON_SALIDA = float(os.getenv("LICITAPRO_IA_USD_SALIDA", "25"))
# Solo para enseñar la cifra en soles al lado. No es contabilidad.
SOLES_POR_DOLAR = float(os.getenv("LICITAPRO_TIPO_CAMBIO", "3.75"))


def coste_usd(tokens_entrada: int, tokens_salida: int) -> float:
    return ((tokens_entrada or 0) * USD_POR_MILLON_ENTRADA
            + (tokens_salida or 0) * USD_POR_MILLON_SALIDA) / 1_000_000


async def gasto_detallado(meses: int = 6) -> dict:
    """Lo que cuesta la IA, por mes y por cliente.

    POR QUE ESTO EXISTE

      El tope por plan acota el peor caso, pero el peor caso no es lo que se
      paga: lo que se paga es lo que la gente usa de verdad. Sin esta vista, la
      unica forma de saberlo era la factura de Anthropic a fin de mes, cuando
      ya no se puede hacer nada, y sin manera de repartirla por plan.

      La pregunta que responde es de negocio, no tecnica: si el plan Pro a
      S/99 deja margen despues de pagar sus analisis, y si hay alguna cuenta
      que se sale de la curva.

    POR QUE SE AGRUPA POR PLAN Y NO SOLO EL TOTAL

      Un total dice cuanto se gasta; no dice que plan lo genera. Si Empresa
      consume tres veces lo que aporta de mas sobre Pro, eso no se ve en el
      total y decide el precio de la lista.
    """
    async with connection() as conn:
        por_mes = await conn.fetch(
            """SELECT date_trunc('month', creado_en)::date AS mes,
                      COUNT(*)                             AS analisis,
                      COUNT(DISTINCT usuario_id)           AS usuarios,
                      COALESCE(SUM(tokens_entrada), 0)     AS entrada,
                      COALESCE(SUM(tokens_salida), 0)      AS salida
                 FROM analisis_ia
                WHERE origen = $1
                  AND creado_en >= date_trunc('month', CURRENT_DATE)
                                   - make_interval(months => $2)
                GROUP BY 1 ORDER BY 1 DESC""",
            ORIGEN_IA, meses)

        # Por plan, solo el mes en curso: es el periodo sobre el que todavia se
        # puede actuar.
        por_plan = await conn.fetch(
            """SELECT COALESCE(p.nombre, 'sin plan')  AS plan,
                      p.precio_mensual,
                      p.analisis_ia_mes               AS tope,
                      COUNT(a.*)                      AS analisis,
                      COUNT(DISTINCT a.usuario_id)    AS usuarios,
                      COALESCE(SUM(a.tokens_entrada), 0) AS entrada,
                      COALESCE(SUM(a.tokens_salida), 0)  AS salida
                 FROM analisis_ia a
                 LEFT JOIN suscripciones s ON s.usuario_id = a.usuario_id
                 LEFT JOIN planes p        ON p.codigo = s.plan_codigo
                WHERE a.origen = $1
                  AND a.creado_en >= date_trunc('month', CURRENT_DATE)
                GROUP BY 1, 2, 3 ORDER BY 7 DESC""",
            ORIGEN_IA)

        # Las cuentas que mas gastan. Es donde aparece el uso anomalo antes de
        # que llegue la factura.
        top = await conn.fetch(
            """SELECT u.email, u.nombre,
                      COALESCE(p.nombre, 'sin plan')     AS plan,
                      COUNT(a.*)                         AS analisis,
                      COALESCE(SUM(a.tokens_entrada), 0) AS entrada,
                      COALESCE(SUM(a.tokens_salida), 0)  AS salida
                 FROM analisis_ia a
                 JOIN usuarios u           ON u.id = a.usuario_id
                 LEFT JOIN suscripciones s ON s.usuario_id = a.usuario_id
                 LEFT JOIN planes p        ON p.codigo = s.plan_codigo
                WHERE a.origen = $1
                  AND a.creado_en >= date_trunc('month', CURRENT_DATE)
                GROUP BY 1,2,3 ORDER BY 6 DESC LIMIT 15""",
            ORIGEN_IA)

        # Cuantas veces se devolvio el heuristico. Si sube, algo va mal con la
        # API o con la clave, y el cliente de pago se esta llevando el respaldo
        # sin que nadie lo note.
        heuristicos = await conn.fetchval(
            """SELECT COUNT(*) FROM analisis_ia
                WHERE origen = $1
                  AND creado_en >= date_trunc('month', CURRENT_DATE)""",
            ORIGEN_HEURISTICO) or 0

    def con_coste(filas):
        salida = []
        for f in filas:
            d = dict(f)
            d["usd"] = coste_usd(d["entrada"], d["salida"])
            d["soles"] = d["usd"] * SOLES_POR_DOLAR
            # `planes.precio_mensual` es NUMERIC, y asyncpg lo devuelve como
            # Decimal. La plantilla lo divide contra el coste, que es float, y
            # Python no mezcla los dos tipos: reventaba al pintar la pagina.
            # Se convierte aqui y no en la plantilla porque el tipo es cosa de
            # la consulta, no del HTML.
            if d.get("precio_mensual") is not None:
                d["precio_mensual"] = float(d["precio_mensual"])
            salida.append(d)
        return salida

    return {
        "por_mes": con_coste(por_mes),
        "por_plan": con_coste(por_plan),
        "top": con_coste(top),
        "heuristicos_del_mes": heuristicos,
        "modelo": MODELO,
        "tarifa": {"entrada": USD_POR_MILLON_ENTRADA,
                   "salida": USD_POR_MILLON_SALIDA,
                   "cambio": SOLES_POR_DOLAR},
    }


async def gasto_del_mes() -> dict:
    """Cuanto se ha gastado en IA este mes. Para el dueno de la plataforma.

    No se convierte a dinero aqui: el precio por token depende del modelo y
    cambia sin avisar a este codigo. Se dan los tokens, que es el dato que no
    caduca, y la conversion se hace contra la tarifa del dia.
    """
    async with connection() as conn:
        return dict(await conn.fetchrow(
            """SELECT COUNT(*)                         AS analisis,
                      COUNT(DISTINCT usuario_id)       AS usuarios,
                      COALESCE(SUM(tokens_entrada), 0) AS tokens_entrada,
                      COALESCE(SUM(tokens_salida), 0)  AS tokens_salida
                 FROM analisis_ia
                WHERE origen = $1
                  AND creado_en >= date_trunc('month', CURRENT_DATE)""",
            ORIGEN_IA))
