"""Pruebas del vigilante que corre en la PC del puente.

QUE PROTEGEN, Y POR QUE ESTE MODULO SE ROMPE CALLADO

  Un vigilante roto no da error: simplemente deja de avisar. Y el dia que se
  nota es el dia que hacia falta. Por eso lo que se fija aqui es la REGLA de
  cuando hablar, que es lo unico que se puede romper editando el codigo.

LAS DOS FORMAS DE INUTILIZARLO SON OPUESTAS

  CALLARSE   No avisar de una caida real. Obvio y grave.
  GRITAR     Avisar cuando no pasa nada. Menos obvio y igual de grave: son 288
             comprobaciones al dia, y a la tercera falsa alarma el chat se
             silencia. Entonces tampoco avisa de las de verdad, y encima queda
             la sensacion de que el riesgo esta cubierto.

  Ya paso con el vigia de GitHub: dos incidencias falsas en siete horas por
  creerse el primer tropiezo de red.

NINGUNA TOCA LA RED

  Preguntar de verdad ataria la suite al estado del sitio, y la pondria roja
  los dias que Cloudflare tosa. Eso ensena a ignorar el rojo, que es como se
  pierde una suite.
"""
from datetime import timedelta

import pytest

from shared import fechas
from tools.vigia_puente import decidir, evaluar

AHORA = fechas.fija(2026, 8, 31, 10, 0, 0)
SANO = {"estado": "sano", "desde": "2026-08-31T09:00:00", "motivo": "ok"}
CAIDO = {"estado": "caido", "desde": "2026-08-31T09:30:00", "motivo": "x"}


# ─── Que cuenta como sano ────────────────────────────────

def test_solo_es_sano_si_responden_la_web_y_la_base():
    assert evaluar(200, {"estado": "ok", "base": "ok"}) == (True, "ok")


@pytest.mark.parametrize("codigo, datos, pista", [
    (0,   None,                                     "no responde en absoluto"),
    (502, None,                                     "codigo 502"),
    # 503 es "la aplicacion vive y no alcanza la base": tiene mensaje propio
    # porque manda a Supabase, no al VPS.
    (503, {"estado": "degradado", "base": "caida"}, "base de datos"),
    (200, None,                                     "no es el JSON"),
    (200, {"estado": "ok", "base": "caida"},        "base"),
    (200, {"estado": "degradado", "base": "ok"},    "se declara"),
])
def test_cada_averia_se_nombra_distinta(codigo, datos, pista):
    """Sin conexion, 502 y base caida llevan a sitios distintos.

    Un aviso que las mezcle en "no funciona" hace perder la primera media hora
    mirando donde no es.
    """
    sano, motivo = evaluar(codigo, datos)
    assert sano is False
    assert pista in motivo


def test_una_web_viva_con_la_base_caida_no_pasa_por_sana():
    """El caso traicionero: HTTP 200 y el producto inservible.

    La aplicacion contesta, el sitio "carga", y no hay ni una licitacion que
    mostrar. Sin esta comprobacion el vigilante lo daria por bueno.
    """
    sano, motivo = evaluar(200, {"estado": "ok", "base": "sin conexion"})
    assert sano is False
    assert "base" in motivo


# ─── Cuando se habla, y cuando no ────────────────────────

def test_lo_normal_es_callarse():
    """Sano y ya estaba sano: ni un mensaje. Es el 99,9% de las pasadas."""
    estado, mensaje = decidir(SANO, True, "ok", AHORA)
    assert mensaje is None
    assert estado["estado"] == "sano"


def test_caido_y_ya_estaba_caido_no_repite_el_aviso():
    """Una caida de dos horas son 24 comprobaciones, no 24 mensajes."""
    _, mensaje = decidir(CAIDO, False, "sigue sin responder", AHORA)
    assert mensaje is None


def test_al_caerse_se_avisa_y_se_dice_por_que():
    estado, mensaje = decidir(SANO, False, "no responde en absoluto", AHORA)
    assert estado["estado"] == "caido"
    assert "no responde en absoluto" in mensaje
    # Y se dice desde donde se miro: hay otro vigilante mirando desde EEUU, y
    # dos avisos del mismo corte parecen dos averias si no se distinguen.
    assert "PERU" in mensaje.upper()


def test_al_volver_se_avisa_y_se_dice_cuanto_duro():
    """Media hora caido: el mensaje lo dice, sin que nadie reste horas a mano."""
    estado, mensaje = decidir(CAIDO, True, "ok", AHORA)
    assert estado["estado"] == "sano"
    assert "30 minutos" in mensaje


def test_la_caida_conserva_su_hora_de_inicio():
    """Si cada pasada reescribiera `desde`, la caida duraria siempre 5 minutos.

    Y entonces el aviso de vuelta mentiria justo en el dato que sirve para
    saber si hubo que dar explicaciones a alguien.
    """
    estado, _ = decidir(CAIDO, False, "sigue caido", AHORA)
    assert estado["desde"] == CAIDO["desde"]


# ─── La primera vez ──────────────────────────────────────

def test_la_primera_ejecucion_con_todo_bien_no_dice_nada():
    """Instalarlo no puede empezar anunciando una recuperacion inventada.

    El primer mensaje que manda un vigilante nuevo no puede ser falso: es
    exactamente lo que ensena a no creerselo.
    """
    estado, mensaje = decidir(None, True, "ok", AHORA)
    assert mensaje is None
    assert estado["estado"] == "sano"


def test_la_primera_ejecucion_con_el_sitio_caido_si_avisa():
    """Si se instala en mitad de una caida, hay que enterarse igual."""
    _, mensaje = decidir(None, False, "no responde en absoluto", AHORA)
    assert mensaje is not None
    assert "no responde" in mensaje


def test_un_estado_corrupto_no_calla_al_vigilante():
    """Sin `desde` legible se avisa igual, solo que sin la duracion.

    Perder el dato es aceptable; perder el aviso, no.
    """
    _, mensaje = decidir({"estado": "caido"}, True, "ok", AHORA)
    assert mensaje is not None
    assert "minutos" not in mensaje


def test_la_duracion_no_sale_negativa_si_el_reloj_va_hacia_atras():
    """Un ajuste de hora no puede producir "estuvo caido -60 minutos"."""
    futuro = {"estado": "caido",
              "desde": (AHORA + timedelta(hours=1)).isoformat()}
    _, mensaje = decidir(futuro, True, "ok", AHORA)
    assert "-" not in mensaje
    assert "0 minutos" in mensaje
