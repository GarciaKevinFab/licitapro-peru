"""Pruebas de la sonda de fuentes: que un 404 no se vea como silencio.

QUE PROTEGEN

  Durante 21 pasadas seguidas, cinco fuentes de produccion anotaron
  `0 encontradas, 0 errores` mientras sus URLs devolvian 404 y 500. El parte
  diario las listaba como "Sin nuevas", que es la frase que se usa para una
  fuente SANA en domingo. Nadie miro nada porque no habia nada que mirar.

  La averia no estaba en los scrapers: estaba en que la unica linea que sabia
  del 404 lo tiraba a la basura.

      if resp.status_code != 200:
          continue

  Lo que se prueba aqui es que esa informacion sobrevive hasta el parte. Es
  decir: no se prueba que las fuentes funcionen -- eso depende de terceros --,
  sino que cuando dejen de funcionar SE NOTE.

POR QUE NO TOCAN NI LA RED NI LA BASE

  Una prueba que pide la URL de verdad falla el dia que el portal esta en
  mantenimiento, y entonces se acostumbra uno a ver rojo y a ignorarlo. Lo que
  se comprueba es la REGLA, que es lo que se rompe al editar el codigo.
"""
import pytest

from radar_bot.scrapers.orchestrator import Sonda, _corto, format_scraping_report


class _Respuesta:
    def __init__(self, codigo, bytes_=50_000):
        self.status_code = codigo
        # Por defecto, el tamano de una pagina con tabla de verdad. Las pruebas
        # que miran la averia de "servidor caido" piden uno pequeno a proposito.
        self.content = b"x" * bytes_


class _ClienteFalso:
    """Devuelve codigos preparados, o lanza si el 'codigo' es una excepcion."""

    def __init__(self, *codigos, bytes_=50_000):
        self.codigos = list(codigos)
        self.bytes_ = bytes_

    async def get(self, url):
        codigo = self.codigos.pop(0)
        if isinstance(codigo, Exception):
            raise codigo
        return _Respuesta(codigo, self.bytes_)


# ─── Fuente sana ─────────────────────────────────────────

async def test_una_pasada_normal_no_deja_diagnostico():
    """Todo respondio y salieron filas: la sonda calla.

    Una sonda que habla cuando todo va bien llena el parte de ruido, y a las
    dos semanas nadie lo lee -- que es exactamente el estado del que veniamos.
    """
    sonda = Sonda("prueba")
    assert await sonda.get(_ClienteFalso(200), "https://ejemplo.pe/a") is not None
    assert sonda.diagnostico(encontradas=25) is None
    assert sonda.errores == 0


async def test_una_pagina_grande_sin_filas_apunta_a_los_selectores():
    """Responde 200, pesa lo normal y no extrae nada: el sitio se rediseno.

    Son dos averias distintas y mandan a sitios distintos: una se arregla
    cambiando la direccion y la otra mirando el HTML. Un diagnostico
    equivocado hace perder la tarde igual que no tener ninguno.
    """
    sonda = Sonda("prueba")
    await sonda.get(_ClienteFalso(200), "https://ejemplo.pe/a")
    detalle = sonda.diagnostico(encontradas=0)
    assert detalle.startswith("SIN EXTRAER")
    assert "selectores" in detalle
    assert "bytes" in detalle, "el tamano se informa siempre"


async def test_una_pagina_diminuta_delata_que_la_entidad_esta_caida():
    """El caso real: IIS sirviendo su pagina de bienvenida con 200 OK.

    Paso con el portal de cotizaciones de Madre de Dios: 703 bytes, titulo
    "IIS Windows Server". Mandar a "revisar los selectores" ahi es media hora
    leyendo un HTML que no tiene nada que leer; la averia es de la entidad.
    """
    sonda = Sonda("prueba")
    await sonda.get(_ClienteFalso(200, bytes_=703), "https://ejemplo.pe/a")
    detalle = sonda.diagnostico(encontradas=0)
    assert detalle.startswith("SIN EXTRAER")
    assert "703 bytes" in detalle
    assert "caida" in detalle
    assert "selectores" not in detalle, "manda al sitio equivocado"


# ─── Fuente caida ────────────────────────────────────────

@pytest.mark.parametrize("codigo", [404, 500, 302, 403])
async def test_ninguna_url_responde_es_una_caida(codigo):
    """Con todas las URLs muertas se dice CAIDA y se dice el codigo.

    El codigo importa: un 403 es un bloqueo por origen -- lo que ya obligo a
    montar el puente -- y un 404 es una pagina que se movio. Sin el numero,
    las dos se investigan igual y una de las dos se investiga en balde.
    """
    sonda = Sonda("prueba")
    assert await sonda.get(_ClienteFalso(codigo), "https://ejemplo.pe/a") is None
    detalle = sonda.diagnostico(encontradas=0)
    assert detalle.startswith("CAIDA")
    assert str(codigo) in detalle
    assert sonda.errores == 1


async def test_una_pagina_grande_no_puede_tapar_a_una_caida():
    """El error que se cometio al escribir esto, congelado.

    La primera version resumia los tamanos en UNO SOLO -- el mayor -- y con
    dos paginas vivas, Madre de Dios caida en 703 bytes y Junin sana en 55 KB,
    el maximo era 55 KB: el aviso volvia a decir "revisar los selectores" y
    escondia justo la averia que habia que ver.

    Cualquier cifra agregada tiene ese defecto. Se informa por URL, y la caida
    va primero porque el parte recorta el detalle.
    """
    sonda = Sonda("prueba")
    # El doble devuelve el mismo tamano a todas, asi que se usa uno por URL
    # para dar a cada una el suyo.
    await Sonda.get(sonda, _ClienteFalso(200, bytes_=703), "http://caida.gob.pe/")
    await Sonda.get(sonda, _ClienteFalso(200, bytes_=55_708), "http://sana.gob.pe/x")

    detalle = sonda.diagnostico(encontradas=0)
    assert "703 bytes" in detalle, "la caida no puede desaparecer del aviso"
    assert "55708 bytes" in detalle
    # Y la caida se nombra ANTES que la otra: el parte corta por longitud.
    assert detalle.index("caida.gob.pe") < detalle.index("sana.gob.pe")


async def test_una_url_caida_de_varias_no_tumba_la_fuente():
    """La sonda devuelve None y quien llama sigue con la siguiente URL.

    Por eso `get` devuelve None en vez de lanzar: una fuente con tres
    direcciones de las que una murio tiene que seguir leyendo las otras dos.
    """
    cliente = _ClienteFalso(404, 200)
    sonda = Sonda("prueba")
    assert await sonda.get(cliente, "https://ejemplo.pe/vieja") is None
    assert await sonda.get(cliente, "https://ejemplo.pe/buena") is not None

    detalle = sonda.diagnostico(encontradas=12)
    assert detalle.startswith("PARCIAL")
    assert "404" in detalle


async def test_un_fallo_de_red_queda_registrado_con_su_nombre():
    """Un timeout o un DNS caido no son 200 ni 404: tambien tienen que constar."""
    sonda = Sonda("prueba")
    cliente = _ClienteFalso(ConnectionError("sin ruta"))
    assert await sonda.get(cliente, "https://ejemplo.pe/a") is None
    assert "ConnectionError" in sonda.diagnostico(encontradas=0)


def test_la_url_se_acorta_para_que_quepa_en_un_movil():
    assert _corto("https://www.sbs.gob.pe/") == "www.sbs.gob.pe"
    assert _corto("https://a.pe/uno/dos/tres") == "a.pe/.../tres"


# ─── El parte lo cuenta ──────────────────────────────────

def test_el_parte_no_llama_sin_nuevas_a_una_fuente_caida():
    """"Sin nuevas" se reserva para las sanas.

    Es LA frase del problema original: suena a domingo tranquilo y consigue
    que nadie mire. Una fuente caida tiene que leerse distinta de un fin de
    semana sin convocatorias.
    """
    parte = format_scraping_report({
        "timestamp": "2026-08-30T08:00:00",
        "total_nuevas": 0,
        "por_fuente": {"gore_portals": 0, "ocds_oece": 0},
        "errores": [],
        "diagnosticos": {"gore_portals": "CAIDA -- a.pe/x: HTTP 404"},
    })
    assert "gore_portals: CAIDA" in parte or "GOREs Regionales: CAIDA" in parte
    assert "HTTP 404" in parte
    # La sana si conserva su "Sin nuevas".
    assert "Sin nuevas" in parte


def test_el_detalle_va_escapado_para_que_telegram_no_lo_rechace():
    """Una URL con & rompe el parse_mode HTML y el mensaje NO llega.

    Y un parte que no llega se ve exactamente igual que un dia sin averias,
    que es la forma mas tonta de reintroducir el fallo que esto arregla.
    """
    parte = format_scraping_report({
        "timestamp": "2026-08-30T08:00:00",
        "total_nuevas": 0,
        "por_fuente": {"sbs": 0},
        "errores": [],
        "diagnosticos": {"sbs": "CAIDA -- a.pe/x?a=1&b=<2>: HTTP 404"},
    })
    assert "&amp;" in parte and "&lt;2&gt;" in parte
    assert "?a=1&b=<2>" not in parte


# ─── El parser no puede ser un requisito duro ────────────

def test_la_sopa_funciona_sin_lxml(monkeypatch):
    """Si lxml no esta, se usa el parser de la biblioteca estandar.

    POR QUE IMPORTA

      Este modulo lo importa tambien el puente, que corre en una PC de casa
      con Python 3.14. lxml no publica binario para cada version nueva de
      Python enseguida, y cuando falta, pip intenta COMPILARLO y muere pidiendo
      Microsoft Visual C++ 14.0 -- el mismo problema que ya obligo a subir
      asyncpg a la 0.31.

      Sin esta caida elegante, una dependencia que aqui no aporta nada dejaria
      el puente sin instalar, y con el puente caido no entra ni una licitacion.
    """
    from radar_bot.scrapers import orchestrator

    real = orchestrator.BeautifulSoup
    intentos = []

    def _falso(texto, parser):
        intentos.append(parser)
        if parser == "lxml":
            raise Exception("FeatureNotFound: lxml no instalado")
        return real(texto, parser)

    monkeypatch.setattr(orchestrator, "BeautifulSoup", _falso)

    sopa = orchestrator._sopa("<table><tr><td>uno</td><td>dos</td></tr></table>")
    assert intentos == ["lxml", "html.parser"], "primero lxml, y solo si falla el otro"
    assert [c.get_text() for c in sopa.find_all("td")] == ["uno", "dos"]


def test_la_sopa_prefiere_lxml_cuando_esta():
    """Que el servidor y el puente parseen igual mientras se pueda.

    Si el puente cayera al parser estandar teniendo lxml, una pagina mal
    cerrada podria leerse distinto en cada maquina, y esa diferencia se
    persigue durante horas.
    """
    from radar_bot.scrapers import orchestrator

    usados = []
    real = orchestrator.BeautifulSoup
    monkeypatch_parser = lambda t, p: (usados.append(p), real(t, p))[1]  # noqa: E731
    orchestrator.BeautifulSoup = monkeypatch_parser
    try:
        orchestrator._sopa("<table><tr><td>x</td></tr></table>")
    finally:
        orchestrator.BeautifulSoup = real
    assert usados == ["lxml"]


def test_el_parte_no_llama_error_a_lo_que_cosecha_el_puente():
    """OECE falla desde el VPS a proposito: la cosecha la hace el puente.

    El primer parte que un humano vio decia "OCDS OECE (principal): Error"
    con la fuente perfectamente cosechada 40 minutos antes. Un parte que
    llama Error a lo esperado ensena a ignorar la palabra Error.
    """
    parte = format_scraping_report({
        "timestamp": "2026-08-31T12:00:00",
        "total_nuevas": 1,
        "por_fuente": {"ocds_oece": -2, "gob_pe": 1},
        "errores": [],
        "diagnosticos": {},
    })
    assert "Error" not in parte
    assert "puente" in parte
