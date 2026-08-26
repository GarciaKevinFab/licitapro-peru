"""Calculos que no tocan nada externo: plazos, banderas y telefonos.

Estas tres cosas comparten un rasgo peligroso: cuando se equivocan NO fallan.
Devuelven una fecha plausible, una bandera de mas o un numero con un digito
cambiado, y nadie se entera hasta que un cliente reclama fuera de plazo o un
aviso llega al telefono de un desconocido. Por eso van cubiertas al detalle.
"""
from datetime import date

import pytest

from shared.banderas import calcular, _umbral, NIVEL_ALTO, NIVEL_MEDIO, NIVEL_BAJO
from shared.plazos_pago import (
    _pascua, dias_de_mora, en_prorroga, es_habil, fecha_limite_pago,
    feriados_de, plazo_legal,
)
from shared.whatsapp import _limpiar_parametro, es_baja, normalizar_numero


# ─── Feriados y dias habiles ─────────────────────────────

@pytest.mark.parametrize("anio, esperado", [
    (2025, date(2025, 4, 20)),
    (2026, date(2026, 4, 5)),
    (2027, date(2027, 3, 28)),
])
def test_pascua_conocida(anio, esperado):
    """Se calcula, no se lista: una lista fija caduca cada 31 de diciembre."""
    assert _pascua(anio) == esperado


def test_2026_tiene_dieciseis_feriados():
    assert len(feriados_de(2026)) == 16


def test_semana_santa_2026():
    f = feriados_de(2026)
    assert f[date(2026, 4, 2)] == "Jueves Santo"
    assert f[date(2026, 4, 3)] == "Viernes Santo"


@pytest.mark.parametrize("mes", [2, 3, 9])
def test_meses_sin_feriados(mes):
    """Febrero, marzo y septiembre no tienen ninguno en Peru."""
    assert not any(d.month == mes for d in feriados_de(2026))


@pytest.mark.parametrize("dia, habil", [
    (date(2026, 8, 26), True),    # miercoles corriente
    (date(2026, 8, 29), False),   # sabado
    (date(2026, 8, 30), False),   # domingo, y Santa Rosa
    (date(2026, 12, 25), False),  # Navidad
    (date(2026, 7, 28), False),   # Fiestas Patrias
])
def test_dias_habiles(dia, habil):
    assert es_habil(dia) is habil


# ─── Plazo legal de pago (Ley 32069) ─────────────────────

def test_bienes_diez_dias_habiles_saltando_feriados():
    """Conformidad el viernes 24-jul-2026: el limite cae el 12 de agosto.

    En medio hay dos Fiestas Patrias, la Batalla de Junin y tres fines de
    semana: 19 dias corridos para 10 habiles. La formula anterior, "+30 dias
    corridos desde la factura", apuntaba al 23 de agosto y habria dejado al
    proveedor esperando mientras la entidad ya estaba en mora desde el 13.
    """
    assert fecha_limite_pago(date(2026, 7, 24), "goods") == date(2026, 8, 12)


def test_servicios_igual_que_bienes():
    assert (fecha_limite_pago(date(2026, 7, 24), "services")
            == fecha_limite_pago(date(2026, 7, 24), "goods"))


def test_obras_no_inventa_fecha():
    """La ley les da reglas propias. Una fecha inventada es peor que ninguna."""
    assert fecha_limite_pago(date(2026, 7, 24), "works") is None
    assert plazo_legal("works")[0] is None


def test_sin_conformidad_no_hay_plazo():
    """No es un hueco: el plazo todavia no empezo a correr."""
    assert fecha_limite_pago(None, "goods") is None


def test_el_limite_siempre_cae_en_dia_habil():
    for dia in range(1, 29):
        limite = fecha_limite_pago(date(2026, 7, dia), "goods")
        assert es_habil(limite), f"conformidad {dia}/7 dio un limite no habil: {limite}"


def test_la_mora_se_cuenta_en_habiles():
    """Del 20 al 26 de agosto de 2026 hay 6 dias corridos y 4 habiles.

    Mezclarlos daria una mora mayor que la real, y con eso se reclama de mas.
    """
    assert dias_de_mora(date(2026, 8, 20), date(2026, 8, 26)) == 4


def test_el_dia_limite_todavia_no_es_mora():
    assert dias_de_mora(date(2026, 8, 26), date(2026, 8, 26)) == 0


def test_prorroga_distingue_retraso_de_incumplimiento():
    """Dentro de los 5 dias de ampliacion la entidad aun puede ampararse.

    Decirle al cliente que reclame ahi le quema credito para cuando de verdad
    tenga razon.
    """
    assert en_prorroga(date(2026, 8, 20), date(2026, 8, 26)) is True
    assert en_prorroga(date(2026, 8, 10), date(2026, 8, 26)) is False


# ─── Banderas de direccionamiento ────────────────────────

UMBRALES = {"LPA": 2, "CPA": 2, "LP": 8, "CP": 8}


@pytest.mark.parametrize("postores, codigos, nivel", [
    (1, ["postor_unico"], NIVEL_ALTO),
    (2, ["pocos_postores"], NIVEL_BAJO),
    (3, ["pocos_postores"], NIVEL_BAJO),
    (4, [], 0),
    (None, [], 0),
])
def test_banderas_por_numero_de_postores(postores, codigos, nivel):
    c, n = calcular({"tipo": "LPA", "numero_postores": postores}, UMBRALES)
    assert c == codigos and n == nivel


def test_plazo_corto_es_relativo_a_su_tipo():
    """4 dias son anomalos en una Licitacion Publica y normales en una Abreviada.

    Un umbral absoluto marcaba el 34% del mercado y no significaba nada.
    """
    assert calcular({"tipo": "LP", "plazo_consultas_dias": 4},
                    UMBRALES)[0] == ["plazo_consultas_corto"]
    assert calcular({"tipo": "LPA", "plazo_consultas_dias": 4}, UMBRALES)[0] == []


def test_el_minimo_legal_de_cada_tipo_no_se_marca():
    """2 dias es el suelo de las Abreviadas y lo tiene un tercio de ellas."""
    assert calcular({"tipo": "LPA", "plazo_consultas_dias": 2}, UMBRALES)[0] == []
    assert calcular({"tipo": "LP", "plazo_consultas_dias": 8}, UMBRALES)[0] == []


@pytest.mark.parametrize("tipo", ["ADS", "AMC", "ASEL", "RE", "CONV", "CI", None])
def test_tipos_sin_norma_conocida_no_se_marcan(tipo):
    """Aplicarle a un procedimiento la norma de otro es inventarsela.

    Esto llego a fallar: 354 de 375 banderas de plazo salian de tipos que no
    estaban mapeados y caian al umbral de los procedimientos ordinarios.
    """
    assert _umbral(tipo, {}) is None
    assert calcular({"tipo": tipo, "plazo_consultas_dias": 1}, UMBRALES)[0] == []


def test_el_nivel_es_el_maximo_no_la_suma():
    """Sumar convertiria dos indicios debiles en uno fuerte, y entonces la lista
    acaba marcandolo todo y deja de leerse."""
    _, nivel = calcular(
        {"tipo": "LP", "numero_postores": 2, "plazo_consultas_dias": 4}, UMBRALES)
    assert nivel == NIVEL_MEDIO


def test_sin_datos_no_inventa_banderas():
    assert calcular({"tipo": "LPA"}, UMBRALES) == ([], 0)


# ─── Telefonos de WhatsApp ───────────────────────────────

@pytest.mark.parametrize("crudo, esperado", [
    ("987654321", "+51987654321"),
    ("+51 987 654 321", "+51987654321"),
    ("51987654321", "+51987654321"),
    ("0987654321", "+51987654321"),
    ("(987) 654-321", "+51987654321"),
    ("14155552671", "+14155552671"),   # otro pais, se respeta
])
def test_numeros_validos(crudo, esperado):
    assert normalizar_numero(crudo) == esperado


@pytest.mark.parametrize("crudo", [
    "012345678",        # fijo peruano: WhatsApp es de moviles
    "005151987654321",  # prefijo tecleado dos veces
    "5198765432",       # empieza por 51 con largo incorrecto
    "123", "", None,
])
def test_numeros_rechazados(crudo):
    """Preferimos rechazar a mandarle el aviso a un desconocido."""
    assert normalizar_numero(crudo) is None


@pytest.mark.parametrize("texto", ["BAJA", "stop", "Cancelar", "dar de baja"])
def test_pide_la_baja(texto):
    assert es_baja(texto) is True


@pytest.mark.parametrize("texto", [
    "no me llego la licitacion", "hola", "no", "quiero mas info",
])
def test_no_pide_la_baja(texto):
    """Se compara la frase entera: dar de baja a quien escribe "no me llego" lo
    deja sin servicio sin que se entere."""
    assert es_baja(texto) is False


def test_parametro_de_plantilla_sin_saltos_de_linea():
    """Meta rechaza el mensaje entero si un parametro los lleva. Eso no falla al
    escribirlo: falla al enviarlo, en produccion."""
    limpio = _limpiar_parametro("Municipalidad\n\tServicio  de   limpieza")
    assert "\n" not in limpio and "\t" not in limpio
    assert "  " not in limpio
