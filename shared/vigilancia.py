"""Avisa cuando la fuente de datos se seca.

POR QUE ESTO EXISTE

  De las once fuentes que corre el orquestador, solo `ocds_oece` entrega
  convocatorias VIGENTES. Las demas son historico, estan bloqueadas por
  reCAPTCHA o devuelven cero. Es decir: el producto entero depende de una API
  de un tercero que puede cambiar sin avisar.

  Y el fallo es silencioso. Si OECE cambia el formato, el scraper no revienta:
  devuelve cero filas, el orquestador lo anota como una corrida correcta de
  cero novedades, y el panel sigue mostrando las licitaciones viejas. Nadie ve
  un error. Lo que se ve, semanas despues, es que los clientes dejan de
  renovar porque "ya no salen licitaciones nuevas".

  `scraping_log` guardaba lo necesario para detectarlo desde el principio.
  Nadie lo consultaba: se escribia en cada corrida y solo se leia para pintar
  una cifra en el panel.

POR QUE SE AVISA UNA VEZ Y LUEGO UNA VEZ AL DIA

  Un aviso cada hora durante una caida de tres dias son setenta y dos mensajes
  identicos. A partir del cuarto nadie los lee, y el dia que llegue uno
  distinto tampoco. Se avisa al cruzar el umbral y despues una vez al dia, que
  es la frecuencia a la que un aviso sigue significando algo.

POR QUE NO SE GUARDA "YA AVISE"

  Haria falta una tabla o un archivo de estado, y los dos se desincronizan con
  la realidad: un contenedor que se reinicia pierde lo que tuviera en memoria,
  y una tabla obliga a limpiarla. La racha se calcula del propio
  `scraping_log`, que es la fuente de verdad y ya esta ahi.
"""
import logging

from shared.db import connection

log = logging.getLogger("shared.vigilancia")

# La unica fuente con convocatorias vigentes. Si esta calla, el producto calla.
FUENTE_PRINCIPAL = "ocds_oece"

# Pasadas BUENAS seguidas sin novedades que se consideran normales. Hay noches
# y domingos en que OECE no publica nada: por debajo de esto se avisaria del
# fin de semana, y un aviso que salta cada sabado deja de leerse antes del
# segundo mes. Con el puente cosechando cada 4 horas, 12 pasadas son unos dos
# dias sin una sola convocatoria nueva, que ya no es un fin de semana normal.
UMBRAL_CORRIDAS = 12

# Horas sin UNA SOLA cosecha buena a partir de las cuales se avisa.
#
#   El puente (tools/traer_oece.py) corre cada 4 horas desde una maquina
#   peruana. Seis horas dan margen para que una pasada se retrase o falle una
#   vez sin despertar a nadie, y siguen siendo la misma manana: si el puente
#   muere a las 8, se sabe antes del almuerzo.
UMBRAL_SILENCIO_HORAS = 6

# Hora de Lima a la que se repite el aviso mientras siga el silencio.
#
#   El recordatorio NO va por modulo de horas transcurridas, como si va el de
#   la sequia. Esa cuenta funciona alli porque la racha sube exactamente de una
#   en una por corrida; las horas no: el planificador se retrasa, un
#   contenedor se reinicia, y un "cada 24 horas" calculado asi se salta el
#   aviso justo el dia que hacia falta. Una hora del reloj no se puede saltar
#   sin que pase el dia entero.
HORA_RECORDATORIO = 9


async def racha_sin_novedades(fuente: str = FUENTE_PRINCIPAL) -> int:
    """Corridas consecutivas mas recientes que no trajeron nada nuevo.

    Se cuenta hacia atras desde la ultima y se para en la primera que si trajo
    algo. Un promedio no serviria: veinte corridas buenas y diez secas dan una
    media tranquilizadora mientras la fuente lleva diez horas muerta.

    SOLO CUENTAN LAS PASADAS QUE LLEGARON A LEER ALGO

      Sin este filtro la cuenta estaba envenenada. El VPS intenta el scrapeo
      cada hora y OECE le devuelve 403: eso deja una fila con
      `encontrados = 0, nuevos = 0`, indistinguible de una pasada sana que no
      trajo novedades. Con una corrida fallida por hora, la racha cruzaba las
      12 sola cada medio dia y el aviso de "la fuente se seco" habria saltado
      practicamente a diario, apuntando ademas a la averia equivocada.

      Una pasada que no pudo entrar no es una pasada sin noticias: es que no
      hubo pasada. Eso lo mide `horas_sin_cosecha`, que es otra averia.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT registros_nuevos FROM scraping_log
                WHERE fuente = $1 AND fin IS NOT NULL
                  AND registros_encontrados > 0
                ORDER BY fin DESC LIMIT 200""", fuente)

    racha = 0
    for f in filas:
        if (f["registros_nuevos"] or 0) > 0:
            break
        racha += 1
    return racha


async def horas_sin_cosecha(fuente: str = FUENTE_PRINCIPAL) -> float | None:
    """Horas desde la ultima pasada que de verdad trajo datos. None si nunca hubo.

    QUE CUENTA COMO "DE VERDAD TRAJO DATOS"

      `registros_encontrados > 0`, no `registros_nuevos > 0`. Una pasada buena
      puede parsear 800 releases y no dar ninguno nuevo porque nadie publico
      nada en esas cuatro horas: eso es la fuente sana, no una fuente muda.
      Lo que distingue una pasada viva de una muerta es si llego a leer algo.

      Y hace falta filtrar, porque en `scraping_log` conviven DOS escritores:

        - El VPS, cada hora, que anota `encontrados = 0, errores = 1` porque
          OECE le devuelve 403.
        - El puente peruano, cada 4 horas, que es el unico que cosecha.

      Sin el filtro, las corridas fallidas del VPS mantendrian el reloj a cero
      para siempre y este vigilante no se dispararia nunca: parecerian actividad
      reciente cuando son justo lo contrario.

    POR QUE LA RESTA SE HACE EN SQL Y NO EN PYTHON

      La sesion trabaja en hora de Lima y las marcas de `scraping_log` son
      naive. Restarlas contra un `datetime.now()` de Python es exactamente el
      desfase de cinco horas que ya mordio a este proyecto una vez. `NOW()` y
      `fin` viven en la misma zona y en el mismo reloj: se restan alli.
    """
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT EXTRACT(EPOCH FROM (NOW() - MAX(fin))) / 3600.0 AS horas
                 FROM scraping_log
                WHERE fuente = $1
                  AND fin IS NOT NULL
                  AND registros_encontrados > 0""", fuente)

    horas = fila["horas"] if fila else None
    return float(horas) if horas is not None else None


async def _hora_de_lima() -> int:
    """La hora en Lima, convertida explicitamente.

    LA SESION DE LA BASE ESTA EN UTC, NO EN LIMA

      Este comentario decia lo contrario -- que `server_settings` dejaba la
      sesion en hora de Lima -- y era falso. Comprobado contra la base:
      `SHOW timezone` responde UTC, y `EXTRACT(HOUR FROM NOW())` devolvia 7
      con Lima en las 2.

      Con eso, el recordatorio diario del silencio sonaba a las 9 UTC, o sea
      a las CUATRO de la manana. Un aviso que llega a esa hora no se lee
      cuando llega y se lee tarde, que es la mitad del problema que este
      recordatorio existe para resolver.

      No afecta a `horas_sin_cosecha`: alli se restan dos marcas del MISMO
      reloj, y por eso la resta se hace en SQL en vez de contra un
      `datetime.now()` de Python.
    """
    async with connection() as conn:
        return int(await conn.fetchval(
            "SELECT EXTRACT(HOUR FROM (NOW() AT TIME ZONE 'America/Lima'))"))


async def revisar(fuente: str = FUENTE_PRINCIPAL) -> dict:
    """Estado de la fuente y si toca avisar ahora.

    Vigila DOS averias distintas, y la diferencia importa:

      MUDA    Hace horas que no entra un solo dato. Hoy el unico camino a OECE
              es el puente, que depende de una PC encendida en Peru: si esa PC
              se apaga un viernes, esto es lo unico que se entera.

      SECA    Las pasadas ocurren y son correctas, pero no traen nada nuevo.
              Apunta a que OECE cambio el formato o el nombre de un campo.

    LA MUDEZ TAPA A LA SEQUIA, Y NO AL REVES

      `racha_sin_novedades` cuenta CORRIDAS. Si el puente deja de correr, deja
      de haber corridas nuevas que contar: la racha se congela y el aviso de
      sequia no salta nunca. Es decir, el vigilante que ya existia dependia de
      lo mismo que vigila.

      Por eso cuando hay silencio se informa del silencio: es el diagnostico
      correcto y ademas es el que explica por que la otra cuenta esta quieta.
    """
    racha = await racha_sin_novedades(fuente)
    horas = await horas_sin_cosecha(fuente)

    # Sin ninguna cosecha en la tabla no se puede medir silencio: seria un aviso
    # permanente en una base recien creada. Se trata como "aun no hay dato".
    muda = horas is not None and horas >= UMBRAL_SILENCIO_HORAS
    seca = racha >= UMBRAL_CORRIDAS

    if muda:
        # Se avisa al cruzar el umbral y despues una vez al dia a la misma
        # hora. La ventana del cruce es de dos horas porque la comprobacion va
        # colgada del scrapeo horario: si una corrida se retrasa, con una sola
        # hora de margen el cruce se perderia y habria que esperar al
        # recordatorio. Repetir el aviso una vez es barato; perderlo, no.
        cruce = UMBRAL_SILENCIO_HORAS <= horas < UMBRAL_SILENCIO_HORAS + 2
        avisar = cruce or (await _hora_de_lima()) == HORA_RECORDATORIO
        motivo = "silencio"
    elif seca:
        avisar = (racha == UMBRAL_CORRIDAS
                  or (racha - UMBRAL_CORRIDAS) % 24 == 0)
        motivo = "sequia"
    else:
        avisar = False
        motivo = None

    return {"fuente": fuente, "racha": racha, "seca": seca,
            "horas_silencio": horas, "muda": muda,
            "motivo": motivo, "avisar": avisar}


def mensaje(estado: dict) -> str:
    """El texto del aviso. Dice que mirar, no solo que algo va mal."""
    if estado.get("motivo") == "silencio":
        horas = estado["horas_silencio"] or 0
        return (
            f"❗ <b>Hace {horas:.0f} horas que no entra una sola "
            f"licitacion.</b>\n\n"
            f"No es que OECE no publique: es que nadie esta cosechando. Hoy el "
            f"unico camino que funciona es el puente, y depende de que la PC "
            f"peruana este encendida y con internet.\n\n"
            f"Que mirar, en este orden:\n"
            f"1. La PC del puente: encendida, con red y sin suspender.\n"
            f"2. Programador de tareas: la tarea de <code>traer_oece</code> "
            f"en verde y con hora reciente.\n"
            f"3. Las ultimas lineas de <code>data/traer_oece.log</code>.\n\n"
            f"Mientras siga asi, el panel de todos los clientes se queda con "
            f"lo viejo."
        )

    return (
        f"⚠️ <b>{estado['fuente']} lleva {estado['racha']} corridas sin traer "
        f"nada nuevo.</b>\n\n"
        f"Es la unica fuente con convocatorias vigentes, asi que mientras siga "
        f"asi el panel de todos los clientes se queda con lo viejo.\n\n"
        f"El scraper no da error: devuelve cero. Suele significar que OECE "
        f"cambio el formato de su API o el nombre de algun campo.\n\n"
        f"Comprobar a mano:\n"
        f"<code>contratacionesabiertas.oece.gob.pe/api/v1/releases</code>"
    )
