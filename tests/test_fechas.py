"""shared/fechas.py: que la hora salga de Lima y no del reloj del contenedor.

    python -m pytest tests/test_fechas.py -q

LA QUE IMPORTA

  test_no_depende_del_reloj_del_sistema. Es la razon de ser del modulo: se
  cambia la TZ del proceso a UTC -- que es como arranca cualquier contenedor sin
  configurar -- y se comprueba que `ahora()` y `hoy()` siguen dando la hora de
  Lima. Con `datetime.now()` y `date.today()`, que es lo que habia en 93 sitios,
  esta prueba falla: entre las 19:00 y medianoche de Lima devolvian el dia
  siguiente, y en un sistema de plazos de licitacion eso adelanta fechas de
  cierre y caduca suscripciones antes de tiempo.

ESTE FICHERO USA datetime.now() Y datetime(...) A PROPOSITO

  Es el unico del proyecto que lo hace, y por eso lleva `# noqa` en esas
  lineas. Una prueba que comprobara `fechas.ahora()` contra `fechas.ahora()`
  no comprobaria nada: hace falta el reloj del sistema como termino de
  contraste, que es justo de lo que el modulo viene a independizarnos.
"""
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import fechas


@pytest.fixture
def reloj_en_utc():
    """Pone el proceso en UTC, como un contenedor recien levantado sin TZ."""
    previo = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    yield
    if previo is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previo
    if hasattr(time, "tzset"):
        time.tzset()


def _lima_de_verdad() -> datetime:
    """La hora de Lima calculada por otro camino, para contrastar."""
    return datetime.now(timezone.utc).astimezone(fechas.LIMA).replace(tzinfo=None)


def test_ahora_da_la_hora_de_lima():
    diferencia = abs((fechas.ahora() - _lima_de_verdad()).total_seconds())
    assert diferencia < 5, f"se desvia {diferencia}s de la hora de Lima"


def test_hoy_da_la_fecha_de_lima():
    assert fechas.hoy() == _lima_de_verdad().date()
    assert isinstance(fechas.hoy(), date)


@pytest.mark.skipif(not hasattr(time, "tzset"),
                    reason="tzset no existe en Windows; esta prueba corre en el CI")
def test_no_depende_del_reloj_del_sistema(reloj_en_utc):
    """Con el proceso en UTC, la hora de Lima tiene que seguir siendo la de Lima.

    Es justo el escenario que rompia el codigo anterior.
    """
    assert fechas.hoy() == _lima_de_verdad().date()
    diferencia = abs((fechas.ahora() - _lima_de_verdad()).total_seconds())
    assert diferencia < 5

    # Y que la prueba no pase por casualidad: con el proceso en UTC el reloj
    # del sistema va cinco horas por delante de Lima, asi que los dos valores
    # TIENEN que discrepar. Sin esta comprobacion, la prueba seguiria en verde
    # aunque la fixture no hubiera cambiado nada.
    desfase = datetime.now() - fechas.ahora()  # noqa: DTZ005
    assert abs(desfase - timedelta(hours=5)) < timedelta(seconds=5), (
        f"el reloj del sistema deberia ir 5h por delante, va {desfase}")


def test_todo_sale_naive():
    """El esquema guarda hora de pared en columnas sin zona.

    Un datetime con tzinfo aqui provocaria TypeError al compararlo con
    cualquier fecha leida de la base, que llega naive.
    """
    assert fechas.ahora().tzinfo is None
    assert fechas.fija(2026, 9, 3).tzinfo is None
    assert fechas.desde_marca(1_700_000_000).tzinfo is None
    assert fechas.desde_texto("03/09/2026", "%d/%m/%Y").tzinfo is None


def test_se_compara_con_lo_que_devuelve_la_base():
    """La comprobacion de verdad: comparar y restar sin que salte TypeError."""
    de_la_base = datetime(2026, 9, 3, 12, 0)  # noqa: DTZ001
    assert fechas.ahora() > de_la_base
    assert (fechas.ahora() - de_la_base).days >= 0
    assert fechas.hoy() >= de_la_base.date()


def test_fija_es_la_fecha_que_se_escribe():
    """Una fecha concreta significa lo mismo corra donde corra la bateria.

    Se contrasta contra el constructor de la libreria estandar a proposito: si
    `fija` empezara a desplazar la hora, esto lo cazaria.
    """
    assert fechas.fija(2026, 9, 3) == datetime(2026, 9, 3, 0, 0)  # noqa: DTZ001
    assert fechas.fija(2026, 9, 3, 14, 30) == datetime(2026, 9, 3, 14, 30)  # noqa: DTZ001


def test_desde_texto_conserva_lo_escrito_y_los_errores():
    assert fechas.desde_texto("03/09/2026 14:30", "%d/%m/%Y %H:%M") == \
        datetime(2026, 9, 3, 14, 30)  # noqa: DTZ001
    # Quien llama sigue tratando el fallo como con strptime.
    with pytest.raises(ValueError):
        fechas.desde_texto("no es una fecha", "%d/%m/%Y")


def test_desde_marca_no_cambia_segun_donde_corra():
    """La misma marca de tiempo, siempre la misma hora de pared."""
    esperado = datetime.fromtimestamp(1_700_000_000, timezone.utc).astimezone(
        fechas.LIMA).replace(tzinfo=None)
    assert fechas.desde_marca(1_700_000_000) == esperado


def test_la_zona_es_la_del_pais_y_no_un_numero_a_mano():
    """ZoneInfo y no un desfase fijo: el horario de verano lo decide un pais."""
    assert str(fechas.LIMA) == "America/Lima"
