"""La hora de Lima, en un solo sitio.

POR QUE EXISTE

  El proyecto llamaba a `datetime.now()` y `date.today()` en 93 sitios. Las dos
  devuelven la hora del RELOJ DEL SISTEMA, asi que lo que guarda la aplicacion
  depende de como este configurado el contenedor y no de una decision escrita.

  Hoy funciona: el contenedor corre en -05 y `date.today()` da la fecha de Lima.
  Pero eso es una coincidencia afortunada, no una garantia -- el dia que alguien
  levante esto en otra maquina, en un runner de CI o en un contenedor sin `TZ`,
  el reloj pasa a UTC y entre las 19:00 y medianoche de Lima el sistema cree que
  ya es el dia siguiente. En un producto que vive de plazos de licitacion y
  vencimientos de suscripcion eso no falla ruidosamente: adelanta un dia las
  fechas de cierre y caduca las cuentas antes de tiempo.

  Aqui la zona esta escrita. El reloj del sistema deja de importar.

POR QUE DEVUELVE FECHAS **NAIVE** Y NO CON ZONA

  Podria parecer que lo correcto es devolver datetimes con `tzinfo`. En este
  esquema seria un error: de las columnas de fecha, 43 son `timestamp` SIN zona
  y solo 3 son `timestamptz`. La convencion -- ya establecida y visible en
  shared/db.py y shared/banderas.py, que comparan contra
  `NOW() AT TIME ZONE 'America/Lima'` -- es guardar la HORA DE PARED de Lima en
  columnas sin zona.

  Meter datetimes con zona en ese esquema tiene dos consecuencias feas, y
  ninguna avisa cuando se escribe:

    1. Comparar uno con zona contra uno sin zona lanza TypeError en Python. Y
       la mitad de las fechas del sistema salen de la base, o sea sin zona.
    2. Al escribir en una columna `timestamp` sin zona, la conversion depende
       del driver: se guarda la hora local, o la UTC, o se pierde el desfase.

  Asi que estas funciones calculan en Lima y devuelven naive: la zona se aplica
  donde importa -- al leer el reloj -- y el valor sale en el formato que el
  esquema ya espera.

  Si algun dia el esquema pasa a `timestamptz`, este es el unico fichero que hay
  que cambiar.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

# La zona del negocio. LicitaPro es peruano de arriba abajo: SEACE, IGV, soles.
#
# ZoneInfo y no un desfase fijo de -5 horas, a proposito. Peru no aplica horario
# de verano desde 1994, asi que hoy son equivalentes, pero un desfase escrito a
# mano es una decision del gobierno peruano copiada dentro del codigo: el dia
# que cambie, esto sigue funcionando y el numero fijo no.
LIMA = ZoneInfo("America/Lima")


def ahora() -> datetime:
    """La hora de pared de Lima, sin `tzinfo`. Sustituye a `datetime.now()`.

    El `.replace(tzinfo=None)` no tira informacion: la zona ya se aplico al
    leer el reloj. Lo que hace es entregar el valor en la forma que espera el
    esquema -- ver la cabecera del modulo.
    """
    return datetime.now(LIMA).replace(tzinfo=None)


def hoy() -> date:
    """La fecha de hoy en Lima. Sustituye a `date.today()`.

    Es la que mas veces aparecia repartida por el codigo (45 de los 93 usos) y
    la que mas caro sale: una fecha de cierre o un vencimiento calculados con un
    dia de mas no revientan, solo dan un resultado equivocado.
    """
    return ahora().date()


def marca_tiempo() -> float:
    """Segundos desde la epoca.

    No depende de zona ninguna -- la epoca es un instante absoluto --, pero vive
    aqui para que todo lo que sea "que hora es" salga del mismo sitio.
    """
    return datetime.now(LIMA).timestamp()


def desde_marca(segundos: float) -> datetime:
    """Convierte segundos desde la epoca a hora de pared de Lima.

    Sustituye a `datetime.fromtimestamp()`, que interpreta la marca en la zona
    del sistema: la misma marca daba una hora distinta segun donde corriera.
    """
    return datetime.fromtimestamp(segundos, LIMA).replace(tzinfo=None)


def desde_texto(texto: str, formato: str) -> datetime:
    """Interpreta una fecha escrita, entendida como hora de Lima.

    Sustituye a `datetime.strptime()`. Las fuentes de OSCE y SEACE publican
    fechas sin zona porque son horas peruanas; esto lo deja dicho en vez de
    dado por supuesto. Lanza ValueError igual que strptime cuando el texto no
    encaja, para que quien llama siga tratando el error como siempre.
    """
    return datetime.strptime(texto, formato).replace(tzinfo=LIMA).replace(tzinfo=None)


def fija(anio: int, mes: int, dia: int, hora: int = 0, minuto: int = 0,
         segundo: int = 0) -> datetime:
    """Una fecha concreta como hora de pared de Lima.

    Sustituye al constructor `datetime(...)` a secas. Se usa sobre todo en las
    pruebas, donde una fecha fija tiene que significar lo mismo corra donde
    corra la bateria -- incluido un runner de CI en UTC.
    """
    return datetime(anio, mes, dia, hora, minuto, segundo,
                    tzinfo=LIMA).replace(tzinfo=None)
