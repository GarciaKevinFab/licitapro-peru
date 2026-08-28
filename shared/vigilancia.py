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

# Corridas seguidas sin novedades que se consideran normales. El scrapeo va
# cada hora y hay noches y domingos en que OECE no publica nada: por debajo de
# esto se avisaria del fin de semana, y un aviso que salta cada sabado deja de
# leerse antes del segundo mes.
UMBRAL_CORRIDAS = 12


async def racha_sin_novedades(fuente: str = FUENTE_PRINCIPAL) -> int:
    """Corridas consecutivas mas recientes que no trajeron nada nuevo.

    Se cuenta hacia atras desde la ultima y se para en la primera que si trajo
    algo. Un promedio no serviria: veinte corridas buenas y diez secas dan una
    media tranquilizadora mientras la fuente lleva diez horas muerta.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT registros_nuevos FROM scraping_log
                WHERE fuente = $1 AND fin IS NOT NULL
                ORDER BY fin DESC LIMIT 200""", fuente)

    racha = 0
    for f in filas:
        if (f["registros_nuevos"] or 0) > 0:
            break
        racha += 1
    return racha


async def revisar(fuente: str = FUENTE_PRINCIPAL) -> dict:
    """Estado de la fuente y si toca avisar ahora.

    `avisar` sale True en la corrida que cruza el umbral y despues una vez cada
    24 corridas, o sea aproximadamente una vez al dia con el planificador
    actual.
    """
    racha = await racha_sin_novedades(fuente)
    seca = racha >= UMBRAL_CORRIDAS
    avisar = seca and (racha == UMBRAL_CORRIDAS
                       or (racha - UMBRAL_CORRIDAS) % 24 == 0)
    return {"fuente": fuente, "racha": racha, "seca": seca, "avisar": avisar}


def mensaje(estado: dict) -> str:
    """El texto del aviso. Dice que mirar, no solo que algo va mal."""
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
