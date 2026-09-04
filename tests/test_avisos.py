"""Pruebas de como se REDACTA un aviso antes de mandarlo.

QUE PROTEGEN

  Los avisos van a Telegram con `parse_mode="HTML"`, y el texto de una
  licitacion -- el objeto, el nombre de la entidad -- lo escribe una entidad
  del Estado en su portal, no nosotros.

  Si ese texto trae un "&" suelto o un "<", la API de Telegram devuelve 400 y
  NO ENTREGA EL MENSAJE. No falla una linea: se pierde el aviso entero de ese
  usuario. En el correo el efecto es mas suave y igual de malo: el cliente ve
  el resumen cortado por la mitad.

  Y "SERVICIOS GENERALES A & B S.A.C." es un nombre de empresa perfectamente
  normal en Peru. Hoy no hay ninguno -- comprobado, 0 de 842 filas --, asi que
  esto no arregla nada visible: fija el dia que llegue el primero.

POR QUE MERECE PRUEBA UN FALLO QUE AUN NO HA PASADO

  Porque su sintoma seria "no me llegan las alertas", que es el sintoma de
  otras cinco averias. Se buscaria en el reparto, en la suscripcion y en los
  filtros antes de mirar un ampersand.
"""

import pytest

from shared import fechas
from shared.notificaciones import _esc, _resumen_html, _resumen_texto


def _licitacion(**cambios) -> dict:
    base = {
        "id": "x1", "nomenclatura": "AS-SM-1-2026", "entidad": "Municipalidad",
        "objeto": "Servicio de internet", "monto_referencial": 1500.0,
        "fecha_cierre": fechas.fija(2026, 9, 2, 16, 0), "fuente": "gore_portals",
        "tipo": "cotizacion", "departamento": "Madre de Dios",
        "score_viabilidad": 43.0,
    }
    base.update(cambios)
    return base


@pytest.mark.parametrize("crudo, esperado", [
    ("A & B S.A.C.", "A &amp; B S.A.C."),
    ("Obra <Tres Islas>", "Obra &lt;Tres Islas&gt;"),
    ("sin nada raro", "sin nada raro"),
    (None, ""),
    ("", ""),
])
def test_se_escapa_lo_que_escribe_la_entidad(crudo, esperado):
    assert _esc(crudo) == esperado


def test_las_comillas_se_dejan_como_estan():
    """Van en el cuerpo del mensaje, no dentro de un atributo.

    Convertirlas a &quot; solo ensuciaria lo que lee el cliente, y los objetos
    de las convocatorias vienen llenos de comillas: OBRA: "MEJORAMIENTO...".
    """
    assert _esc('OBRA: "MEJORAMIENTO"') == 'OBRA: "MEJORAMIENTO"'


def test_el_resumen_de_correo_no_deja_pasar_un_ampersand_crudo():
    html = _resumen_html([_licitacion(entidad="SERVICIOS A & B S.A.C.")])
    assert "&amp;" in html
    assert "A & B" not in html


def test_el_resumen_de_telegram_tampoco():
    """Aqui es mas grave: un "<" crudo hace que la API rechace el mensaje."""
    texto = _resumen_texto([_licitacion(objeto="Suministro <urgente> de equipos",
                                        nomenclatura=None)])
    assert "&lt;urgente&gt;" in texto
    assert "<urgente>" not in texto


def test_las_etiquetas_nuestras_siguen_siendo_etiquetas():
    """Escapar el dato no puede escapar el formato.

    Si se escapara el HTML entero, el cliente veria "<b>" escrito en el
    mensaje -- que es la forma tonta de arreglar esto y romperlo igual.
    """
    html = _resumen_html([_licitacion()])
    assert "<li>" in html and "<b>" in html


def test_el_listado_del_bot_tambien_escapa():
    """El mismo dato entra por otra puerta: /hoy y /buscar en el bot."""
    from radar_bot.main import format_licitacion_alert

    texto, _ = format_licitacion_alert(_licitacion(entidad="A & B S.A.C."))
    assert "&amp;" in texto
    assert "A & B" not in texto
    # Y el formato propio sobrevive.
    assert "<b>" in texto
