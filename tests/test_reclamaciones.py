"""Libro de Reclamaciones: lo que la Ley 29571 obliga a que no se pierda.

POR QUE ESTA SUITE EXISTE

  Un reclamo perdido no da error. La persona rellena el formulario, ve algo que
  parece una confirmacion y se va tranquila; el dia que acude a INDECOPI resulta
  que no hay hoja. Eso es exactamente lo que la norma busca impedir, y es un
  fallo que solo se descubre cuando ya es un problema legal.

LAS TRES COSAS QUE SE FIJAN

  1. Que la pagina sea PUBLICA y que el portero de suscripcion no la corte. El
     caso mas probable es alguien a quien acabamos de cortar el servicio.
  2. Que el plazo se cuente en dias HABILES. Contarlo natural nos pondria en
     falta antes de tiempo.
  3. Que el correo NO sea condicion para registrar. El SMTP esta caido ahora
     mismo; si el alta dependiera de el, hoy no se podria reclamar.
"""
import re

import pytest

from shared import fechas
from shared.suscripciones import ruta_libre
from tests.conftest import sin_base
from web.reclamaciones import DIAS_HABILES_RESPUESTA, _codigo, limite_respuesta

# ─── El portero no puede cortar el Libro ─────────────────

def test_reclamar_no_exige_estar_al_dia_de_pago():
    """Quien reclama suele ser justo a quien le cortamos algo.

    Si el portero de suscripcion redirigiera esta ruta a /suscripcion, la unica
    via de reclamacion quedaria detras del cobro que se reclama.
    """
    assert ruta_libre("/reclamaciones") is True


def test_una_ruta_que_solo_comparte_prefijo_no_queda_libre():
    assert ruta_libre("/reclamacionesx") is False


# ─── El plazo, en dias habiles ───────────────────────────

def test_el_plazo_salta_los_fines_de_semana():
    """15 dias habiles desde un lunes caen tres semanas despues, no dos.

    Contarlos naturales daria una fecha ANTERIOR a la legal y nos pondriamos en
    falta sin haberlo hecho.
    """
    lunes = fechas.fija(2026, 9, 7)          # lunes
    limite = limite_respuesta(lunes, 15)
    assert limite.weekday() < 5, "la fecha limite cae en fin de semana"
    assert (limite - lunes).days == 21    # 15 habiles = 21 naturales


@pytest.mark.parametrize("dia", range(7))
def test_el_limite_nunca_cae_en_sabado_ni_domingo(dia):
    """Se empiece el dia que se empiece."""
    desde = fechas.fija(2026, 9, 7 + dia)
    assert limite_respuesta(desde).weekday() < 5


def test_el_plazo_por_defecto_es_el_de_la_norma():
    assert DIAS_HABILES_RESPUESTA == 15


# ─── El correlativo ──────────────────────────────────────

def test_el_codigo_se_ensena_completo_y_ordenable():
    """Con ese numero se acude a INDECOPI: no se abrevia ni se trunca."""
    assert _codigo(1) == "LR-000001"
    assert _codigo(123456) == "LR-123456"
    # Y ordena bien como texto, que es como se va a listar.
    assert _codigo(2) > _codigo(1)


# ─── La pagina ───────────────────────────────────────────

@sin_base
async def test_el_libro_se_ve_sin_cuenta(cliente):
    r = await cliente.get("/reclamaciones")
    assert r.status_code == 200
    t = r.text
    # Los campos que la ley exige recoger.
    for campo in ('name="tipo"', 'name="nombre"', 'name="documento_numero"',
                  'name="email"', 'name="detalle"', 'name="pedido"',
                  'name="monto_reclamado"', 'name="es_menor_edad"'):
        assert campo in t, campo
    # Y lo que hay que decirle a quien reclama.
    assert "29571" in t
    assert "15 días hábiles" in t
    assert "INDECOPI" in t.upper()


@sin_base
async def test_una_hoja_incompleta_no_se_registra(cliente):
    """Y se le dice por que, sin perder lo que ya habia escrito."""
    r = await cliente.post("/reclamaciones", data={
        "tipo": "reclamo", "nombre": "Ana Torres", "documento_numero": "12345678",
        "email": "ana@ejemplo.pe", "detalle": "corto", "pedido": "que lo arreglen"})
    assert r.status_code == 400
    # Se busca dentro del bloque de error y por una palabra sin acentos: el
    # texto exacto con tildes se compara distinto segun la codificacion de la
    # consola, y una prueba que falla por eso no dice nada del producto.
    assert re.search(r'class="mensaje mal">[^<]*detalle', r.text), "sin aviso"
    assert "Ana Torres" in r.text, "se perdio lo que ya habia escrito"


@sin_base
async def test_el_menor_de_edad_necesita_apoderado(cliente):
    r = await cliente.post("/reclamaciones", data={
        "tipo": "queja", "nombre": "Ana Torres", "documento_numero": "12345678",
        "email": "ana@ejemplo.pe", "es_menor_edad": "1",
        "detalle": "La atencion fue muy lenta y nadie respondio",
        "pedido": "Que me contesten"})
    assert r.status_code == 400
    assert re.search(r'class="mensaje mal">[^<]*apoderado', r.text)


@sin_base
async def test_se_registra_aunque_el_correo_no_salga(cliente, monkeypatch):
    """EL FALLO QUE ESTA PRUEBA IMPIDE

    El SMTP esta caido en produccion ahora mismo. Si el envio de la copia
    tumbara el alta, hoy no se podria reclamar en absoluto -- y perder un
    reclamo por un fallo de correo es justo lo que la norma prohibe.

    La hoja se guarda primero y el correo se intenta despues, con la excepcion
    capturada. Aqui se fuerza el fallo y se comprueba que el numero sale igual.
    """
    import sys
    import types

    async def revienta(*a, **k):
        raise RuntimeError("SMTP caido, como en produccion")

    # Se sustituye el MODULO entero, no un atributo suyo: `monkeypatch.setattr`
    # con una ruta de texto tiene que importarlo primero, y ahi ya fallaria en
    # una maquina sin aiosmtplib. Asi la prueba vale en las dos.
    falso = types.ModuleType("shared.email_sender")
    falso.enviar_email = revienta
    monkeypatch.setitem(sys.modules, "shared.email_sender", falso)

    r = await cliente.post("/reclamaciones", data={
        "tipo": "reclamo", "nombre": "Ana Torres", "documento_tipo": "DNI",
        "documento_numero": "12345678", "email": "ana@ejemplo.pe",
        "detalle": "El aviso de una licitacion no me llego a tiempo",
        "pedido": "Que revisen por que no salio el correo"})
    assert r.status_code == 200
    assert re.search(r"LR-\d{6}", r.text), "no se ensena el numero de hoja"
