"""Pruebas de la puerta que separa "cobrar de verdad" de "no cobrar nada".

QUE PROTEGEN

  Pasar a cobrar parece un interruptor -- cambiar IZIPAY_MODO de sandbox a
  produccion -- y no lo es: hacen falta ademas las credenciales que Izipay
  emite tras validar el negocio y la web.

  Con las de prueba y el modo en produccion, las peticiones van al host real
  con llaves que alli no valen: la pasarela rechaza la autenticacion y el
  cliente ve "La pasarela no respondio". Es decir, el estado en el que uno CREE
  que ya factura es exactamente el estado en el que NADIE puede pagar. Y no se
  nota desde dentro: el panel funciona, el sitio funciona, simplemente no entra
  dinero.

  Comprobado en el servidor el 30/08/2026: IZIPAY_MODO=sandbox y la clave
  publica es "34481044:testpublickey_...". El interruptor estaba a un
  descuido de dejar el cobro roto.

POR QUE NINGUNA TOCA LA PASARELA

  Lo que se prueba es la REGLA de configuracion. Llamar a Izipay ataria la
  suite a un tercero y la volveria roja los dias que ellos tengan
  mantenimiento, que es como se ensena a ignorar el rojo.
"""
import pytest

from shared import izipay


def _entorno(monkeypatch, modo, publica="34481044:publickey_ABC",
             api="prodpassword_XYZ", comercio="34481044"):
    monkeypatch.setenv("IZIPAY_MODO", modo)
    monkeypatch.setenv("IZIPAY_PUBLIC_KEY", publica)
    monkeypatch.setenv("IZIPAY_API_KEY", api)
    monkeypatch.setenv("IZIPAY_MERCHANT_CODE", comercio)


# ─── El valor por defecto falla hacia "no cobra" ─────────

@pytest.mark.parametrize("valor", ["", "  ", "productivo", "PROD", "test"])
def test_un_modo_que_no_se_reconoce_no_intenta_cobrar(monkeypatch, valor):
    """Ante la duda, simulado. Nunca "intenta cobrar de verdad"."""
    _entorno(monkeypatch, valor)
    assert izipay.modo() == "simulado"


def test_sin_credenciales_no_se_cobra_aunque_el_modo_lo_pida(monkeypatch):
    _entorno(monkeypatch, "produccion")
    monkeypatch.delenv("IZIPAY_API_KEY")
    assert izipay.modo() == "simulado"


# ─── Produccion con llaves de prueba: se para en seco ────

def test_produccion_con_credenciales_de_prueba_se_niega(monkeypatch):
    """El fallo caro: creer que se factura mientras nadie puede pagar.

    Se levanta el error en vez de caer de vuelta a sandbox. Caer de vuelta
    dejaria la pasarela en pruebas mientras el dueno cree que cobra, que es la
    misma confusion con otra cara.
    """
    _entorno(monkeypatch, "produccion",
             publica="34481044:testpublickey_l9e0WdZu8",
             api="testpassword_abc123")
    with pytest.raises(RuntimeError, match="PRUEBAS"):
        izipay.modo()


def test_el_error_dice_que_hay_que_cambiar_todas_las_claves(monkeypatch):
    """Un mensaje que solo diga "mal configurado" manda a adivinar."""
    _entorno(monkeypatch, "produccion",
             publica="34481044:testpublickey_x", api="testpassword_y")
    with pytest.raises(RuntimeError) as e:
        izipay.modo()
    texto = str(e.value)
    assert "IZIPAY_MODO" in texto
    assert "produccion" in texto
    for pieza in ("comercio", "publica", "API key", "HMAC"):
        assert pieza in texto, f"el mensaje no menciona {pieza}"


def test_sandbox_con_credenciales_de_prueba_es_lo_correcto(monkeypatch):
    """Es la combinacion sana, y hoy la del servidor: no puede dar error."""
    _entorno(monkeypatch, "sandbox",
             publica="34481044:testpublickey_x", api="testpassword_y")
    assert izipay.modo() == "sandbox"


def test_produccion_con_credenciales_de_verdad_cobra(monkeypatch):
    _entorno(monkeypatch, "produccion")
    assert izipay.modo() == "produccion"


# ─── El detector, por separado ───────────────────────────

@pytest.mark.parametrize("publica, api, esperado", [
    ("34481044:testpublickey_x", "testpassword_y", True),
    ("34481044:publickey_x", "prodpassword_y", False),
    ("34481044:TestPublicKey_x", "loquesea", True),      # sin distinguir mayusculas
    ("", "", False),
])
def test_se_reconoce_una_credencial_de_prueba(monkeypatch, publica, api, esperado):
    monkeypatch.setenv("IZIPAY_PUBLIC_KEY", publica)
    monkeypatch.setenv("IZIPAY_API_KEY", api)
    assert izipay.credenciales_de_prueba() is esperado


# ─── El cobro no puede dispararse al importar ────────────

def test_importar_el_renovador_no_cobra_a_nadie():
    """El fallo que llevaba en produccion, y que solo se veia en un log.

    `tools/renovar_suscripciones.py` terminaba con

        sys.exit(asyncio.run(main()))

    a nivel de modulo. `win_bot` lo importa para llamarlo, asi que el IMPORT
    ejecutaba la renovacion entera dentro del bucle del bot y moria con
    "asyncio.run() cannot be called from a running event loop".

    Resultado: la renovacion automatica no ha corrido nunca desde el bot. Hoy
    da igual -- Izipay esta en sandbox y no hay tarjetas guardadas --, pero el
    dia que las haya, nadie renovaria y las cuentas caerian al plan gratuito de
    una en una sin que nada avise.

    Se comprueba leyendo el fichero y no importandolo: importarlo es
    precisamente lo que no debe tener efectos, y una prueba que lo hiciera
    cobraria de verdad si el guardia desapareciera.
    """
    import pathlib
    fuente = (pathlib.Path(__file__).resolve().parent.parent
              / "tools" / "renovar_suscripciones.py").read_text(encoding="utf-8")

    for linea in fuente.splitlines():
        if "asyncio.run(" in linea and not linea.lstrip().startswith("#"):
            assert linea.startswith("    "), (
                "asyncio.run() vuelve a estar a nivel de modulo: importar "
                "tools/renovar_suscripciones cobraria a todos los clientes")
    assert 'if __name__ == "__main__":' in fuente, "falta el guardia de __main__"
