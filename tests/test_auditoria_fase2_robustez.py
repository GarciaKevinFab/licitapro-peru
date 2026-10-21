"""Pendientes de robustez de la auditoria (fase 2).

LO QUE SE PROTEGE AQUI

  1. Rutas de data/ y templates/ ancladas a la raiz del proyecto, no al cwd.
     Las rutas viejas guardadas en la base como texto relativo se resuelven
     contra la raiz al leerlas.
  2. python-docx (documento_word), la hoja membretada y pdfium fuera del bucle
     de eventos; pdfium siempre dentro del candado.
  3. red_segura conecta a la IP ya validada (sin ventana de DNS rebinding),
     con el nombre en Host y en el SNI.
  4. Migraciones 0002-0008 idempotentes.
  5. Contratos siempre filtrados por empresa o cuenta; renovaciones sin float;
     el webhook de Izipay exige que el importe pagado sea el de la orden.
"""
import ast
import hashlib
import hmac
import json
import os
import threading
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from shared import rutas
from tests.conftest import sin_base

RAIZ = Path(__file__).resolve().parent.parent


# ─── 1. Rutas ancladas a la raiz ─────────────────────────

def test_la_raiz_es_la_del_proyecto():
    assert rutas.RAIZ == RAIZ
    assert (rutas.RAIZ / "shared" / "rutas.py").is_file()


def test_una_ruta_relativa_se_resuelve_contra_la_raiz_y_no_el_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert rutas.en_raiz("data/firmas/x.png") == RAIZ / "data" / "firmas" / "x.png"
    absoluta = tmp_path / "otra" / "x.png"
    assert rutas.en_raiz(str(absoluta)) == absoluta


def test_el_directorio_respeta_la_variable_y_la_ancla(monkeypatch, tmp_path):
    monkeypatch.delenv("DIR_DE_PRUEBA", raising=False)
    assert rutas.directorio("DIR_DE_PRUEBA", "data/x") == RAIZ / "data" / "x"
    monkeypatch.setenv("DIR_DE_PRUEBA", "data/y")
    assert rutas.directorio("DIR_DE_PRUEBA", "data/x") == RAIZ / "data" / "y"
    monkeypatch.setenv("DIR_DE_PRUEBA", str(tmp_path))
    assert rutas.directorio("DIR_DE_PRUEBA", "data/x") == tmp_path


def test_en_produccion_el_resultado_es_el_mismo():
    """Con la raiz en /app, "data/expedientes" sigue siendo /app/data/expedientes."""
    from pathlib import PurePosixPath
    raiz = PurePosixPath("/app")
    assert raiz / "data/expedientes" == PurePosixPath("/app/data/expedientes")


def test_las_constantes_de_disco_son_absolutas():
    from prep_bot import document_gen, zip_builder
    from prep_bot.autofill import proposal
    from shared import anexos_mpt, archivos, llenar_formatos, membrete, pdf_firmable
    from web import propuestas
    from win_bot import conformity_gen, invoice_gen
    for valor in (zip_builder.EXPEDIENTES_DIR, zip_builder.TEMPLATES_DIR,
                  document_gen.TEMPLATES_DIR, proposal.TEMPLATES_DIR,
                  pdf_firmable.SALIDA, llenar_formatos.DIR, anexos_mpt.CACHE,
                  membrete.DIR, archivos.DIRECTORIO, propuestas.PAQUETES,
                  conformity_gen.TEMPLATES_DIR, invoice_gen.TEMPLATES_DIR):
        assert Path(valor).is_absolute(), valor


def test_no_quedan_rutas_de_data_relativas_en_el_codigo():
    """Ningun `Path("data/...")` ni `getenv(X, "data/...")` a pelo."""
    culpables = []
    for archivo in RAIZ.rglob("*.py"):
        partes = archivo.relative_to(RAIZ).parts
        if partes[0] in ("tests", ".claude", ".venv", ".venv-tarea", "graphify-out"):
            continue
        texto = archivo.read_text(encoding="utf-8", errors="replace")
        for marca in ('Path("data/', "Path('data/", 'getenv("', "getenv('"):
            for linea in texto.splitlines():
                if marca in linea and ("\"data/" in linea or "'data/" in linea
                                       or ", \"templates\")" in linea):
                    culpables.append(f"{archivo.name}: {linea.strip()}")
    assert sorted(set(culpables)) == []


async def test_la_firma_guardada_relativa_se_encuentra_desde_otro_cwd(tmp_path, monkeypatch):
    """`obtener_firma` resuelve la ruta de la base contra la raiz, no el cwd."""
    from shared import firma_manager

    relativa = "tests/test_auditoria_fase2_robustez.py"   # existe seguro

    class _Conn:
        async def fetchval(self, *a):
            return relativa

    class _Ctx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(firma_manager, "connection", lambda: _Ctx())
    monkeypatch.chdir(tmp_path)
    assert await firma_manager.obtener_firma(1, "firma") == str(RAIZ / relativa)


def test_el_expediente_viejo_relativo_se_sirve_contra_la_raiz():
    """La descarga resuelve `expediente_zip_path` con en_raiz, no con el cwd."""
    fuente = (RAIZ / "web" / "propuestas.py").read_text(encoding="utf-8")
    assert 'en_raiz(prop["expediente_zip_path"])' in fuente
    fuente = (RAIZ / "shared" / "admin_cuentas.py").read_text(encoding="utf-8")
    assert "p = en_raiz(ruta)" in fuente


# ─── 2. Nada pesado en el bucle de eventos ───────────────

def _llamadas_directas(modulo: Path, nombres: set[str]) -> list[int]:
    """Lineas donde se llama a alguno de `nombres` directamente (no por to_thread)."""
    arbol = ast.parse(modulo.read_text(encoding="utf-8"))
    malas = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            f = nodo.func
            nombre = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if nombre in nombres:
                malas.append(nodo.lineno)
    return malas


@pytest.mark.parametrize("ruta", [
    "prep_bot/document_gen.py", "prep_bot/autofill/annexes.py",
    "prep_bot/autofill/proposal.py",
])
def test_documento_word_no_se_llama_desde_el_bucle(ruta):
    assert _llamadas_directas(RAIZ / ruta, {"documento_word"}) == []


def test_pdf_firmable_no_lee_la_hoja_en_el_bucle():
    fuente = RAIZ / "shared" / "pdf_firmable.py"
    arbol = ast.parse(fuente.read_text(encoding="utf-8"))
    generar = next(n for n in ast.walk(arbol)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "generar_pdf")
    # Dentro de generar_pdf, y fuera de la funcion interna que va al hilo, no
    # hay llamadas directas a hoja.de / hoja.estampar.
    directas = []

    def recorrer(nodo):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, ast.FunctionDef):
                continue
            if (isinstance(hijo, ast.Call) and isinstance(hijo.func, ast.Attribute)
                    and hijo.func.attr in ("de", "estampar")):
                directas.append(hijo.lineno)
            recorrer(hijo)

    recorrer(generar)
    assert directas == []


async def test_el_word_membretado_se_abre_en_otro_hilo(monkeypatch):
    from prep_bot import document_gen
    hilos = []

    def _falso(empresa_id):
        hilos.append(threading.get_ident())
        return "doc"

    monkeypatch.setattr(document_gen, "documento_word", _falso)
    assert await document_gen._documento(7) == "doc"
    assert hilos and hilos[0] != threading.get_ident()


def test_todo_pdfium_va_dentro_del_candado():
    """Cada `pdfium.PdfDocument(...)` del codigo esta dentro de `with CANDADO:`."""
    fuera = []
    for archivo in RAIZ.rglob("*.py"):
        partes = archivo.relative_to(RAIZ).parts
        if partes[0] in ("tests", ".claude", ".venv", ".venv-tarea", "graphify-out"):
            continue
        texto = archivo.read_text(encoding="utf-8", errors="replace")
        if "PdfDocument" not in texto:
            continue
        arbol = ast.parse(texto)
        protegidas = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.With) and any(
                    isinstance(i.context_expr, ast.Name) and i.context_expr.id == "CANDADO"
                    for i in nodo.items):
                protegidas |= {n.lineno for n in ast.walk(nodo) if hasattr(n, "lineno")}
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr == "PdfDocument" and nodo.lineno not in protegidas):
                fuera.append(f"{archivo.name}:{nodo.lineno}")
    assert fuera == []


# ─── 3. red_segura: se conecta a la IP validada ──────────

DOM = ("regionmadrededios.gob.pe",)
PUBLICA = "200.48.225.130"


async def test_la_peticion_va_a_la_ip_validada_con_el_nombre_en_host_y_sni(monkeypatch):
    from radar_bot.scrapers import red_segura
    respuestas = iter([[PUBLICA], ["169.254.169.254"]])   # el rebinding

    async def _resolver(host, puerto):
        return next(respuestas)
    monkeypatch.setattr(red_segura, "_resolver", _resolver)
    vistas = []

    async def manejador(req):
        vistas.append(req)
        return httpx.Response(200, content=b"%PDF ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(manejador)) as c:
        # https y un host con certificado valido: el autofirmado tiene su
        # propio cliente (y saldria a la red de verdad).
        c.cookies.set("sesion", "abc", domain="www.regionmadrededios.gob.pe")
        cuerpo = await red_segura.bajar(
            c, "https://www.regionmadrededios.gob.pe/x.pdf", DOM, 1000)
    assert cuerpo == b"%PDF ok"
    (req,) = vistas
    assert req.url.host == PUBLICA                     # no se volvio a resolver
    assert req.headers["host"] == "www.regionmadrededios.gob.pe"
    assert req.extensions["sni_hostname"] == "www.regionmadrededios.gob.pe"
    assert "sesion=abc" in req.headers.get("cookie", "")


async def test_cada_salto_resuelve_y_fija_su_ip(monkeypatch):
    from radar_bot.scrapers import red_segura
    resueltos = []

    async def _resolver(host, puerto):
        resueltos.append(host)
        return [PUBLICA]
    monkeypatch.setattr(red_segura, "_resolver", _resolver)

    async def manejador(req):
        if req.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200, content=b"%PDF b")

    async with httpx.AsyncClient(transport=httpx.MockTransport(manejador)) as c:
        assert await red_segura.bajar(
            c, "http://cotizaciones.regionmadrededios.gob.pe/a", DOM, 1000) == b"%PDF b"
    assert resueltos == ["cotizaciones.regionmadrededios.gob.pe"] * 2


def test_una_ipv6_va_entre_corchetes():
    from radar_bot.scrapers import red_segura
    c = httpx.AsyncClient()
    req = red_segura.fijar_ip(c, httpx.URL("https://www.gob.pe/x"), "2800:200::1")
    assert str(req.url).startswith("https://[2800:200::1]/x")
    assert req.headers["host"] == "www.gob.pe"


def test_el_host_autofirmado_por_https_usa_su_propio_cliente():
    from radar_bot.scrapers import red_segura
    c = httpx.AsyncClient(headers={"User-Agent": "x"})
    propio = red_segura._transporte_autofirmado(
        c, httpx.URL("https://cotizaciones.regionmadrededios.gob.pe/x"))
    assert propio is not None and propio.headers["user-agent"] == "x"
    assert red_segura._transporte_autofirmado(
        c, httpx.URL("https://www.gob.pe/x")) is None
    assert red_segura._transporte_autofirmado(
        c, httpx.URL("http://cotizaciones.regionmadrededios.gob.pe/x")) is None


# ─── 4. Migraciones idempotentes ─────────────────────────

@pytest.mark.parametrize("numero", ["0002", "0003", "0004", "0005", "0007", "0008"])
def test_las_migraciones_viejas_no_crean_a_pelo(numero):
    (archivo,) = (RAIZ / "migrations" / "versions").glob(f"{numero}_*.py")
    texto = archivo.read_text(encoding="utf-8")
    subida = texto[texto.index("def upgrade"):texto.index("def downgrade")]
    for op in ("create_table", "add_column", "create_index",
               "create_unique_constraint", "create_foreign_key"):
        assert f"op.{op}(" not in subida, (numero, op)


def test_la_0006_quita_la_restriccion_solo_si_existe():
    (archivo,) = (RAIZ / "migrations" / "versions").glob("0006_*.py")
    texto = archivo.read_text(encoding="utf-8")
    subida = texto[texto.index("def upgrade"):texto.index("def downgrade")]
    assert "DROP CONSTRAINT IF EXISTS" in subida
    assert "op.drop_constraint" not in subida


def _url_sync() -> str:
    return (f"postgresql+psycopg2://{os.getenv('POSTGRES_USER', 'licitapro')}:"
            f"{os.getenv('POSTGRES_PASSWORD', '')}@{os.getenv('POSTGRES_HOST', 'localhost')}:"
            f"{os.getenv('POSTGRES_PORT', '5434')}/{os.getenv('POSTGRES_DB', 'licitapro')}")


@sin_base
def test_las_operaciones_idempotentes_se_pueden_repetir():
    """Todo dentro de una transaccion que se deshace: el esquema no cambia."""
    sa = pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg2")
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from migrations import idempotente as idem

    tabla = f"tmp_idem_{uuid.uuid4().hex[:8]}"
    motor = sa.create_engine(_url_sync())
    try:
        with motor.connect() as conn:
            trans = conn.begin()
            try:
                with Operations.context(MigrationContext.configure(conn)):
                    for _ in range(2):
                        idem.create_table(tabla, sa.Column("id", sa.Integer, primary_key=True))
                        idem.add_column(tabla, sa.Column("a", sa.Text))
                        idem.add_column(tabla, sa.Column("b", sa.Integer))
                        idem.create_index(f"idx_{tabla}_a", tabla, ["a"])
                        idem.create_unique_constraint(f"uq_{tabla}_b", tabla, ["b"])
                    assert idem.hay_tabla(tabla)
                    assert idem.hay_columna(tabla, "a") and not idem.hay_columna(tabla, "z")
                    assert idem.hay_indice(f"idx_{tabla}_a")
                    assert idem.hay_restriccion(tabla, f"uq_{tabla}_b")
            finally:
                trans.rollback()
    finally:
        motor.dispose()


# ─── 5. Contratos, renovaciones e Izipay ─────────────────

async def test_los_contratos_activos_exigen_filtro():
    from shared.db import get_contratos_activos
    with pytest.raises(ValueError):
        await get_contratos_activos()


@sin_base
async def test_los_contratos_de_una_cuenta_son_solo_suyos(usuario):
    from shared.db import get_contratos_activos
    assert list(await get_contratos_activos(usuario_id=usuario["id"])) == []


async def test_la_renovacion_cobra_el_monto_sin_float(monkeypatch):
    import importlib
    renovar = importlib.import_module("tools.renovar_suscripciones")
    cobrados = []

    async def _pendientes():
        return [{"token_tarjeta": b"x", "email": "a@b.pe", "usuario_id": 1,
                 "monto": Decimal("99.90"), "intentos_fallidos": 0}]

    async def _cobrar(token, orden, monto):
        cobrados.append(monto)
        return {"ok": True, "transaction_id": "T", "detalle": None}

    async def _nada(*a, **k):
        return None

    monkeypatch.setattr(renovar, "renovaciones_pendientes", _pendientes)
    monkeypatch.setattr(renovar, "descifrar", lambda v: "tok")
    monkeypatch.setattr(renovar, "registrar_intento", _nada)
    monkeypatch.setattr(renovar, "confirmar_pago", _nada)
    monkeypatch.setattr(renovar.izipay, "cobrar_con_token", _cobrar)
    assert await renovar.main() == 0
    assert cobrados == [Decimal("99.90")] and isinstance(cobrados[0], Decimal)


def test_las_pasarelas_formatean_el_decimal_exacto():
    from shared import culqi, izipay
    assert izipay.importe(Decimal("99.90")) == "99.90"
    assert izipay.importe(Decimal("0.285")) == "0.29"
    assert culqi.a_centimos(Decimal("99.90")) == 9990
    assert culqi.a_centimos(Decimal("0.29")) == 29


@pytest.mark.parametrize("datos, esperado", [
    ({"amount": "99.00", "currency": "PEN"}, (Decimal("99.00"), "PEN")),
    ({"response": {"amount": "99"}}, (Decimal("99.00"), None)),
    ({"response": {"order": [{"amount": "1.00", "currency": "PEN"}]}},
     (Decimal("1.00"), "PEN")),
    ({"orderDetails": {"orderTotalAmount": 9900, "orderCurrency": "PEN"}},
     (Decimal("99.00"), "PEN")),
    ({"code": "00"}, (None, None)),
    ({"amount": "abc"}, (None, None)),
])
def test_el_importe_pagado_se_lee_del_aviso(datos, esperado):
    from web.suscripcion import importe_pagado
    assert importe_pagado(datos) == esperado


def _firmado(cuerpo: dict, clave: str) -> tuple[bytes, dict]:
    crudo = json.dumps(cuerpo).encode()
    firma = hmac.new(clave.encode(), crudo, hashlib.sha256).hexdigest()
    return crudo, {"x-izipay-signature": firma, "content-type": "application/json"}


async def _estado_del_pago(orden: str) -> str:
    from shared.db import connection
    async with connection() as c:
        return await c.fetchval(
            "SELECT estado FROM pagos_suscripcion WHERE izipay_order_number=$1", orden)


@sin_base
async def test_el_webhook_no_aplica_un_importe_distinto_al_de_la_orden(
        cliente, usuario, monkeypatch):
    from shared.suscripciones import registrar_intento
    clave = "clave-de-prueba-" + uuid.uuid4().hex
    monkeypatch.setenv("IZIPAY_HMAC_KEY", clave)
    orden = "LPF2" + uuid.uuid4().hex[:12].upper()
    assert await registrar_intento(usuario["id"], Decimal("99.00"), orden)

    for cuerpo in ({"orderNumber": orden, "code": "00", "amount": "1.00"},
                   {"orderNumber": orden, "code": "00"},                    # sin importe
                   {"orderNumber": orden, "code": "00", "amount": "99.00",
                    "currency": "USD"}):
        crudo, cabeceras = _firmado(cuerpo, clave)
        r = await cliente.post("/webhooks/izipay", content=crudo, headers=cabeceras)
        assert r.status_code == 200 and r.json()["aplicado"] is False, cuerpo
        assert await _estado_del_pago(orden) == "pendiente"

    crudo, cabeceras = _firmado({"orderNumber": orden, "code": "00", "amount": "99.00",
                                 "currency": "PEN"}, clave)
    r = await cliente.post("/webhooks/izipay", content=crudo, headers=cabeceras)
    assert r.status_code == 200 and r.json()["aplicado"] is True
    assert await _estado_del_pago(orden) == "pagado"


@sin_base
async def test_el_webhook_acepta_el_importe_en_centimos(cliente, usuario, monkeypatch):
    from shared.suscripciones import registrar_intento
    clave = "clave-de-prueba-" + uuid.uuid4().hex
    monkeypatch.setenv("IZIPAY_HMAC_KEY", clave)
    orden = "LPF2" + uuid.uuid4().hex[:12].upper()
    assert await registrar_intento(usuario["id"], Decimal("49.00"), orden)
    crudo, cabeceras = _firmado({"orderNumber": orden, "status": "PAID",
                                 "orderDetails": {"orderTotalAmount": 4900,
                                                  "orderCurrency": "PEN"}}, clave)
    r = await cliente.post("/webhooks/izipay", content=crudo, headers=cabeceras)
    assert r.json()["aplicado"] is True
