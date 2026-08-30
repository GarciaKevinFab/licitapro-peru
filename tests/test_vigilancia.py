"""Pruebas del vigilante de la fuente de datos.

QUE PROTEGEN, Y POR QUE IMPORTA MAS QUE OTRAS SUITES

  Este modulo es el unico que avisa de que el producto dejo de funcionar. Si se
  rompe, no se nota: no hay excepcion, no hay pantalla en rojo, no hay cliente
  que llame. Simplemente los avisos dejan de salir y todo parece tranquilo.
  Un vigilante roto es peor que no tener vigilante, porque ademas da por
  cubierto un riesgo que sigue abierto.

  Ya paso una vez: la fuente principal estuvo doce corridas caida con el 403
  anotado en una tabla que no miraba nadie.

LAS DOS AVERIAS SON DISTINTAS Y NO SE PUEDEN CONFUNDIR

  MUDA   No entra ningun dato. Hoy significa que el puente peruano se apago.
  SECA   Entran datos pero ninguno nuevo. Significa que OECE cambio algo.

  Mandan a sitios distintos -- una a mirar una PC, la otra a mirar una API --,
  asi que un aviso con el diagnostico equivocado hace perder la tarde.

LA MAYORIA NO TOCA LA BASE A PROPOSITO

  Lo que se prueba aqui es la REGLA de cuando avisar. Atarla a PostgreSQL la
  haria saltarse en cualquier maquina sin base, que es justo donde a nadie le
  consta que dejo de correr.
"""
from shared import vigilancia
from tests.conftest import sin_base


def _fingir(monkeypatch, *, racha: int, horas, hora_del_dia: int = 15):
    """Sustituye las tres consultas por valores fijos."""
    async def _racha(fuente=vigilancia.FUENTE_PRINCIPAL):
        return racha

    async def _horas(fuente=vigilancia.FUENTE_PRINCIPAL):
        return horas

    async def _hora():
        return hora_del_dia

    monkeypatch.setattr(vigilancia, "racha_sin_novedades", _racha)
    monkeypatch.setattr(vigilancia, "horas_sin_cosecha", _horas)
    monkeypatch.setattr(vigilancia, "_hora_de_lima", _hora)


# ─── Fuente sana ─────────────────────────────────────────

async def test_fuente_sana_no_avisa(monkeypatch):
    """Cosecha reciente y novedades: silencio absoluto.

    Un vigilante que avisa cuando todo va bien se silencia en una semana, y
    entonces tampoco avisa cuando algo va mal.
    """
    _fingir(monkeypatch, racha=2, horas=1.4)
    estado = await vigilancia.revisar()
    assert estado["avisar"] is False
    assert estado["motivo"] is None
    assert estado["muda"] is False


async def test_sin_novedades_pero_cosechando_no_es_averia(monkeypatch):
    """800 releases leidos y ninguno nuevo a las 3 de la manana es normal."""
    _fingir(monkeypatch, racha=3, horas=0.5)
    estado = await vigilancia.revisar()
    assert estado["avisar"] is False


# ─── El puente se apago ──────────────────────────────────

async def test_al_cruzar_el_umbral_avisa_de_silencio(monkeypatch):
    """La averia que hasta ahora no detectaba nadie."""
    _fingir(monkeypatch, racha=3, horas=vigilancia.UMBRAL_SILENCIO_HORAS + 0.5)
    estado = await vigilancia.revisar()
    assert estado["muda"] is True
    assert estado["motivo"] == "silencio"
    assert estado["avisar"] is True


async def test_justo_por_debajo_del_umbral_aun_no_avisa(monkeypatch):
    """El puente va cada 4 horas: a las 5 todavia no hay motivo de alarma."""
    _fingir(monkeypatch, racha=3, horas=vigilancia.UMBRAL_SILENCIO_HORAS - 1)
    estado = await vigilancia.revisar()
    assert estado["muda"] is False
    assert estado["avisar"] is False


async def test_el_silencio_largo_no_repite_el_aviso_cada_hora(monkeypatch):
    """Una caida de fin de semana no puede ser cuarenta mensajes iguales."""
    _fingir(monkeypatch, racha=3, horas=31, hora_del_dia=15)
    estado = await vigilancia.revisar()
    assert estado["muda"] is True
    assert estado["avisar"] is False


async def test_el_silencio_largo_recuerda_una_vez_al_dia(monkeypatch):
    """Pero tampoco puede callarse para siempre: a la hora fijada, recuerda."""
    _fingir(monkeypatch, racha=3, horas=31,
            hora_del_dia=vigilancia.HORA_RECORDATORIO)
    estado = await vigilancia.revisar()
    assert estado["avisar"] is True
    assert estado["motivo"] == "silencio"


# ─── Precedencia ─────────────────────────────────────────

async def test_la_mudez_tapa_a_la_sequia(monkeypatch):
    """Con el puente parado, "cambio el formato de la API" es un diagnostico falso.

    Y ademas la racha esta congelada precisamente PORQUE no hay corridas: el
    numero que sostiene el aviso de sequia dejo de actualizarse.
    """
    _fingir(monkeypatch, racha=vigilancia.UMBRAL_CORRIDAS + 40, horas=20,
            hora_del_dia=vigilancia.HORA_RECORDATORIO)
    estado = await vigilancia.revisar()
    assert estado["motivo"] == "silencio"
    assert "puente" in vigilancia.mensaje(estado)


async def test_sequia_con_la_fuente_viva(monkeypatch):
    """Cosechando al dia pero sin una sola novedad: eso si es la API."""
    _fingir(monkeypatch, racha=vigilancia.UMBRAL_CORRIDAS, horas=1.0)
    estado = await vigilancia.revisar()
    assert estado["motivo"] == "sequia"
    assert estado["avisar"] is True
    assert "formato" in vigilancia.mensaje(estado)


# ─── Instalacion nueva ───────────────────────────────────

async def test_una_base_sin_cosechas_no_es_una_averia(monkeypatch):
    """Sin ninguna pasada buena no hay reloj que medir.

    Sin esto, una base recien migrada avisaria en la primera corrida y para
    siempre, que es la forma mas rapida de que se apague el vigilante.
    """
    _fingir(monkeypatch, racha=0, horas=None)
    estado = await vigilancia.revisar()
    assert estado["muda"] is False
    assert estado["avisar"] is False


# ─── El filtro contra la base de verdad ──────────────────

@sin_base
async def test_las_corridas_bloqueadas_no_cuentan_como_pasadas(marca):
    """Una corrida con 403 no es "una pasada sin novedades": no hubo pasada.

    Esta es la prueba de un fallo real. El VPS intenta el scrapeo cada hora y
    OECE le devuelve 403, lo que deja una fila con encontrados=0 y nuevos=0 --
    identica a una pasada sana sin novedades. Sin filtrar, la racha cruzaba el
    umbral sola cada medio dia y el aviso de "la fuente se seco" habria saltado
    a diario, apuntando ademas a la averia equivocada.
    """
    from shared.db import connection

    fuente = f"prueba-vigilancia-{marca}"
    async with connection() as c:
        try:
            # Una pasada buena, y despues tres bloqueos como los del VPS.
            await c.execute(
                """INSERT INTO scraping_log
                       (fuente, fin, registros_encontrados, registros_nuevos, errores)
                   VALUES ($1, NOW() - INTERVAL '3 hours', 800, 5, 0)""", fuente)
            # El intervalo va como PARAMETRO, no interpolado. Un f-string aqui
            # deja la consulta en la lista de "revisar a mano" de
            # tools/auditar_sql.py para siempre: el auditor no puede PREPARAR
            # lo que no esta completo, asi que se la salta en vez de validarla.
            for horas in (2, 1, 0):
                await c.execute(
                    """INSERT INTO scraping_log
                           (fuente, fin, registros_encontrados, registros_nuevos, errores)
                       VALUES ($1, NOW() - make_interval(hours => $2), 0, 0, 1)""",
                    fuente, horas)

            # Los tres bloqueos no cuentan: la ultima pasada REAL trajo 5.
            assert await vigilancia.racha_sin_novedades(fuente) == 0

            # Y el reloj se mide desde la cosecha, no desde el ultimo intento.
            horas = await vigilancia.horas_sin_cosecha(fuente)
            assert horas is not None
            assert 2.9 < horas < 3.1
        finally:
            await c.execute("DELETE FROM scraping_log WHERE fuente = $1", fuente)
