"""Banderas de direccionamiento: indicios de que un proceso ya tiene dueno.

QUE ES Y QUE NO ES

  Esto NO acusa a nadie de corrupcion. Marca senales que conviene mirar antes
  de gastar dinero preparando una propuesta. Un proceso con un solo postor
  puede tener un solo postor porque nadie mas fabrica eso, y un plazo corto
  puede ser una urgencia real. Quien decide es el proveedor, con el dato
  delante; nosotros no emitimos veredictos sobre entidades del Estado.

  De ahi el lenguaje de toda la interfaz: "conviene revisar", no "corrupto".
  Es lo honesto y ademas lo unico defendible si alguna entidad pregunta.

DE DONDE SALEN LOS UMBRALES

  De los datos, no de la intuicion. Se midieron 500 procesos reales antes de
  escribir esto, y la medicion descarto dos banderas que parecian obvias:

  - Adjudicar al 100% del valor referencial resulto ser la NORMA (mediana del
    ratio = 1.000; 34 de 60 procesos al 100%). Marcarlo habria senalado a mas
    de la mitad de las entidades del pais sin fundamento.
  - Las marcas en especificaciones no estan en este feed: 2 menciones en 556
    items, porque las descripciones vienen del catalogo CUBSO y las
    especificaciones tecnicas viven dentro del PDF de las bases.

  El plazo de consultas se compara SIEMPRE contra el de su propio tipo de
  procedimiento, nunca en absoluto. Medido:

      Licitacion Publica Abreviada    min 2   mediana 4    max 9
      Concurso Publico Abreviado      min 2   mediana 4    max 8
      Licitacion Publica              min 8   mediana 10   max 20
      Concurso Publico de Servicios   min 8   mediana 10   max 24

  Dos dias son normales en una Abreviada (el 34% de todas lo tiene) y anomalos
  en una Licitacion Publica, donde el minimo observado es 8. Un umbral unico
  habria marcado un tercio del mercado y no habria significado nada.

  Los umbrales se recalculan sobre nuestra propia base segun crece, en vez de
  quedar clavados: si el patron del mercado cambia, la bandera lo sigue.
"""
import json
import logging

from shared.db import connection

log = logging.getLogger("shared.banderas")

# Cuantas muestras hacen falta de un tipo para fiarse de su percentil. Por
# debajo de esto un par de procesos raros moverian el umbral entero.
MIN_MUESTRAS = 20

# Percentil por debajo del cual el plazo se considera corto PARA SU TIPO.
# 0.10 y no 0.25: se busca lo llamativo, no lo simplemente por debajo de la
# media. Con 0.25 se marcaria uno de cada cuatro procesos y el aviso dejaria de
# leerse, que es la forma habitual de matar una alerta.
PERCENTIL_CORTO = 0.10

# Respaldo cuando un tipo no tiene muestras suficientes. Salen de la medicion
# de arriba: los procedimientos abreviados admiten 2 dias con normalidad, los
# ordinarios no bajan de 8.
UMBRAL_POR_DEFECTO_ABREVIADO = 2
UMBRAL_POR_DEFECTO_ORDINARIO = 8

NIVEL_ALTO, NIVEL_MEDIO, NIVEL_BAJO = 3, 2, 1

DESCRIPCIONES = {
    "postor_unico": (
        NIVEL_ALTO,
        "Un solo postor",
        "Se adjudicó con un único participante. Puede ser un rubro donde no hay "
        "más proveedores, o unas bases escritas a la medida de uno."),
    "pocos_postores": (
        NIVEL_BAJO,
        "Muy pocos postores",
        "Participaron dos o tres. Poca competencia no prueba nada por sí sola, "
        "pero conviene mirar quién ganó antes en esta entidad."),
    "plazo_consultas_corto": (
        NIVEL_MEDIO,
        "Plazo de consultas muy corto",
        "La entidad dejó menos días para consultas y observaciones que la "
        "mayoría de procesos de su mismo tipo. Es el plazo con el que se "
        "cuestionan unas bases dirigidas."),
    "entidad_postor_unico_frecuente": (
        NIVEL_MEDIO,
        "Esta entidad suele adjudicar con un solo postor",
        "En sus procesos ya resueltos, la mitad o más se adjudicaron con un "
        "único participante. La media nacional está en el 21%. Puede ser un "
        "rubro sin competencia, o bases que dejan fuera a casi todos."),
    "ganador_recurrente": (
        NIVEL_MEDIO,
        "El mismo proveedor gana siempre aquí",
        "Este proveedor ha ganado buena parte de los procesos adjudicados por "
        "esta entidad. Puede ser el único capaz de cumplir, o una relación ya "
        "establecida."),
}


def describir(codigo: str) -> dict:
    nivel, titulo, detalle = DESCRIPCIONES.get(codigo, (NIVEL_BAJO, codigo, ""))
    return {"codigo": codigo, "nivel": nivel, "titulo": titulo, "detalle": detalle}


# Codigos cortos de procedimiento abreviado. Se comprueban ademas del texto
# porque `licitaciones.tipo` guarda el codigo ("LPA"), no el nombre largo.
CODIGOS_ABREVIADOS = frozenset({"LPA", "CPA", "AS", "SIE", "CdP", "CM"})
# Procedimientos ordinarios, con plazos legales largos.
CODIGOS_ORDINARIOS = frozenset({"LP", "CP"})


def _es_abreviado(tipo: str | None) -> bool:
    t = (tipo or "").strip()
    return t in CODIGOS_ABREVIADOS or "abreviad" in t.lower()


async def umbrales_por_tipo() -> dict[str, int]:
    """Plazo por debajo del cual se considera corto, por tipo de procedimiento.

    Se calcula sobre nuestra propia base para que el umbral siga al mercado. Los
    tipos con pocas muestras quedan fuera y caen al respaldo documentado: un
    percentil sobre cuatro filas no es un umbral, es ruido.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT tipo,
                      COUNT(*) AS n,
                      percentile_cont($1) WITHIN GROUP (ORDER BY plazo_consultas_dias)
                        AS corte
                 FROM licitaciones
                WHERE plazo_consultas_dias IS NOT NULL AND tipo IS NOT NULL
                GROUP BY tipo
               HAVING COUNT(*) >= $2""",
            PERCENTIL_CORTO, MIN_MUESTRAS)
    umbrales = {f["tipo"]: int(f["corte"]) for f in filas if f["corte"] is not None}
    log.info("Umbrales de plazo por tipo: %s", umbrales)
    return umbrales


def _umbral(tipo: str | None, umbrales: dict[str, int]) -> int | None:
    """Umbral del tipo, o None si no sabemos cual es su norma.

    None significa "no marcar". Antes se caia al umbral ordinario de 8 dias
    para cualquier tipo desconocido, y eso marcaba TODO lo que no estuviera
    mapeado: 354 de 375 banderas de plazo salian de ahi. Aplicarle a un
    procedimiento la norma de otro es inventarsela, y aqui la regla es la
    contraria: si no lo sabemos, no lo decimos.
    """
    if tipo and tipo in umbrales:
        return umbrales[tipo]
    if not tipo:
        return None
    if _es_abreviado(tipo):
        return UMBRAL_POR_DEFECTO_ABREVIADO
    if tipo in CODIGOS_ORDINARIOS:
        return UMBRAL_POR_DEFECTO_ORDINARIO
    return None


def calcular(lic: dict, umbrales: dict[str, int] | None = None) -> tuple[list[str], int]:
    """(codigos, nivel) para una licitacion. Sin datos devuelve ([], 0).

    El nivel es el MAXIMO de las banderas encontradas, no la suma. Sumar
    convertiria dos indicios debiles en uno fuerte, que es exactamente el error
    que hace que estas listas acaben marcandolo todo y dejen de leerse.
    """
    umbrales = umbrales or {}
    codigos: list[str] = []

    postores = lic.get("numero_postores")
    if postores is not None:
        if postores == 1:
            codigos.append("postor_unico")
        elif 2 <= postores <= 3:
            codigos.append("pocos_postores")

    plazo = lic.get("plazo_consultas_dias")
    # Estrictamente MENOR, no menor o igual. El minimo legal de cada tipo es lo
    # que usa casi todo el mundo: en las abreviadas, 2 dias es el suelo y lo
    # tiene un tercio de los procesos. Marcarlo senalaria a un tercio del
    # mercado, que es como se mata una alerta. Solo interesa lo que queda por
    # DEBAJO de lo habitual para su tipo.
    umbral = _umbral(lic.get("tipo"), umbrales)
    if plazo is not None and umbral is not None and plazo < umbral:
        codigos.append("plazo_consultas_corto")

    if not codigos:
        return [], 0
    nivel = max(DESCRIPCIONES.get(c, (NIVEL_BAJO,))[0] for c in codigos)
    return codigos, nivel


async def marcar_ganadores_recurrentes(minimo_procesos: int = 5,
                                       cuota: float = 0.6) -> int:
    """Anade 'ganador_recurrente' donde un proveedor domina a una entidad.

    Necesita historico acumulado, por eso va aparte del calculo por fila: al
    parsear un release todavia no se sabe cuantas veces gano ese proveedor.

    `minimo_procesos` evita el falso positivo tipico: una entidad con dos
    adjudicaciones y el mismo ganador da el 100% y no significa nada.
    """
    # Se cruza por NOMBRE del proveedor y no por su RUC. Medido contra la API:
    # la parte del proveedor llega con additionalIdentifiers en null -- solo la
    # entidad trae PE-RUC. De 2.702 procesos resueltos, 2.664 tienen nombre de
    # ganador y CERO tienen su RUC. Esta consulta cruzaba por RUC y por eso
    # marcaba 0 combinaciones: no fallaba, es que el dato no existe.
    #
    # El nombre es peor llave que un RUC (una tilde o un "S.A.C." de mas y ya
    # no casa), pero fallar por defecto hacia "no marco" es el lado correcto:
    # se pierde alguna coincidencia y no se senala a nadie por error.
    async with connection() as conn:
        filas = await conn.fetch(
            """WITH por_entidad AS (
                   SELECT entidad_ruc, proveedor_ganador,
                          COUNT(*) AS ganados,
                          SUM(COUNT(*)) OVER (PARTITION BY entidad_ruc) AS total
                     FROM licitaciones
                    WHERE proveedor_ganador IS NOT NULL AND entidad_ruc IS NOT NULL
                    GROUP BY entidad_ruc, proveedor_ganador
               )
               SELECT entidad_ruc, proveedor_ganador FROM por_entidad
                WHERE total >= $1 AND ganados::float / total >= $2""",
            minimo_procesos, cuota)

        tocadas = 0
        for f in filas:
            n = await conn.fetchval(
                """UPDATE licitaciones
                      SET banderas = CASE
                              WHEN banderas ? 'ganador_recurrente' THEN banderas
                              ELSE COALESCE(banderas, '[]'::jsonb)
                                   || '["ganador_recurrente"]'::jsonb END,
                          banderas_nivel = GREATEST(banderas_nivel, $3)
                    WHERE entidad_ruc = $1 AND proveedor_ganador = $2
                RETURNING 1""",
                f["entidad_ruc"], f["proveedor_ganador"], NIVEL_MEDIO)
            tocadas += bool(n)
    log.info("Ganadores recurrentes marcados en %s combinaciones", tocadas)
    return tocadas


# Cuantos procesos resueltos hacen falta para juzgar a una entidad. Con menos,
# dos casualidades dan el 100% y no significan nada.
MIN_RESUELTOS_ENTIDAD = 5
# Por encima de que proporcion de postor unico se considera anomala. Medido
# sobre 127 entidades: la media es 0,21 y solo 18 pasan del 0,5. Ese es el
# corte que separa lo llamativo de lo corriente.
CUOTA_POSTOR_UNICO = 0.5


async def marcar_entidades_con_mal_historial() -> int:
    """Marca licitaciones ABIERTAS por el historial de quien las convoca.

    Esto existe porque las otras banderas llegaban tarde. `postor_unico` y
    `pocos_postores` solo se saben cuando el proceso ya se adjudico, o sea
    cuando ya no puedes presentarte: avisaban de algo que no podias usar.

    Lo que si sirve ANTES de postular es con quien te estas metiendo. Si una
    entidad resolvio la mitad de sus procesos con un solo postor, conviene
    saberlo antes de gastar dias preparando una propuesta para ella.

    Sigue sin ser una acusacion: puede ser un rubro donde no hay competencia.
    Es un dato que el proveedor no tenia y ahora tiene.
    """
    async with connection() as conn:
        n = await conn.fetch(
            """WITH historial AS (
                   SELECT entidad_ruc,
                          COUNT(*) FILTER (WHERE numero_postores IS NOT NULL) AS resueltos,
                          COUNT(*) FILTER (WHERE numero_postores = 1) AS con_unico
                     FROM licitaciones
                    WHERE entidad_ruc IS NOT NULL
                    GROUP BY entidad_ruc
               ),
               senaladas AS (
                   SELECT entidad_ruc FROM historial
                    WHERE resueltos >= $1
                      AND con_unico::float / resueltos >= $2
               )
               UPDATE licitaciones l
                  SET banderas = CASE
                          WHEN l.banderas ? 'entidad_postor_unico_frecuente' THEN l.banderas
                          ELSE COALESCE(l.banderas, '[]'::jsonb)
                               || '["entidad_postor_unico_frecuente"]'::jsonb END,
                      banderas_nivel = GREATEST(l.banderas_nivel, $3)
                 FROM senaladas s
                WHERE l.entidad_ruc = s.entidad_ruc
                  AND l.fecha_cierre > NOW()
             RETURNING 1""",
            MIN_RESUELTOS_ENTIDAD, CUOTA_POSTOR_UNICO, NIVEL_MEDIO)
    log.info("Licitaciones abiertas marcadas por historial de la entidad: %s", len(n))
    return len(n)


async def recalcular_todo() -> dict:
    """Recalcula las banderas de toda la tabla. Idempotente.

    Se usa al desplegar y tras un cambio de umbrales. Recorre por lotes en vez
    de traerlo todo a memoria: la tabla crece con cada scrapeo y un dia no
    cabria.
    """
    umbrales = await umbrales_por_tipo()
    parte = {"revisadas": 0, "marcadas": 0}
    ultimo = ""

    while True:
        async with connection() as conn:
            filas = await conn.fetch(
                """SELECT id, tipo, numero_postores, plazo_consultas_dias
                     FROM licitaciones
                    WHERE id > $1
                    ORDER BY id LIMIT 1000""",
                ultimo)
        if not filas:
            break
        ultimo = filas[-1]["id"]

        cambios = []
        for f in filas:
            codigos, nivel = calcular(dict(f), umbrales)
            cambios.append((f["id"], json.dumps(codigos), nivel))
        parte["revisadas"] += len(filas)
        parte["marcadas"] += sum(1 for _, c, n in cambios if n)

        async with connection() as conn:
            await conn.executemany(
                "UPDATE licitaciones SET banderas=$2::jsonb, banderas_nivel=$3 WHERE id=$1",
                cambios)

    await marcar_ganadores_recurrentes()
    # Va al final, despues de que las banderas por fila esten puestas: se apoya
    # en `numero_postores` de los procesos ya resueltos para juzgar a la entidad.
    parte["abiertas_por_entidad"] = await marcar_entidades_con_mal_historial()
    log.info("Recalculo de banderas: %s", parte)
    return parte
