"""Cotizacion por items: la SC 5884-2026-GOREMAD de punta a punta.

QUE PROTEGE

  El 2026-10-10 se probo el sistema llenando esta solicitud real: nueve
  alimentos, cada uno con marca y precio. Salieron tres fallos que estas
  pruebas fijan:

    1. No habia donde poner marca y precio por item: la propuesta tenia un
       solo monto y el documento economico era una linea de total.
    2. El expediente quedaba bloqueado por partida registral y vigencia de
       poder, que una contratacion menor no pide.
    3. La declaracion jurada era la generica, y el GOREMAD exige la suya, con
       la cuenta interbancaria.

  Los precios son los que el usuario puso de verdad, y el total que tiene que
  salir es el que el calculo a mano dio: S/ 12,115.20.
"""
import io
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfReader

from radar_bot.scrapers.orchestrator import _detalle_cotizacion
from shared import cotizacion
from shared.documentos_menores import concepto_corto, formato_dj, referencia
from tests.conftest import sin_base

FICHA = (Path(__file__).parent / "fixtures" / "goremad_solicitud_21306.html").read_text(
    encoding="utf-8")
DESCRIPCION = _detalle_cotizacion(FICHA)

OBJETO = ("[BIENES] ADQUISICION DE ALIMENTOS NO PERECIBLE - PARA LA GERENCIA REGIONAL "
          "DE GESTION DEL RIESGO DE DESASTRES - MUY IMPORTANTE: EL POSTOR DEBE REVISAR "
          "LAS ESPECIFICACIONES TECNICAS DETALLADAMENTE Y VERIFICAR CORRECTAMENTE LAS CARACT")

# (marca, precio unitario) en el orden de la SC, tal como los paso el usuario.
PRECIOS = [
    ("Marina", "2.50"), ("Filete Marinero", "5.20"), ("Gloria", "4.30"),
    ("A granel", "7.00"), ("Nicolini", "6.00"), ("Quaker", "5.80"),
    ("A granel", "4.80"), ("Cholo", "4.00"), ("Mirasol", "8.50"),
]


def _texto_pdf(contenido: bytes) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(contenido)).pages)


# ─── Sin base ────────────────────────────────────────────

def test_los_items_de_la_ficha_se_leen_con_cantidad_y_unidad():
    items = cotizacion.items_de_texto(DESCRIPCION)
    assert len(items) == 9
    assert items[0] == {"descripcion": "SAL DE MESA", "cantidad": Decimal("54"), "unidad": "KLG"}
    assert items[7]["descripcion"] == "ARROZ EXTRA" and items[7]["cantidad"] == 399
    # Las caracteristicas entre corchetes no se pegan a la descripcion.
    assert items[3]["descripcion"] == "LENTEJA CALIDAD 1 - EXTRA"


def test_un_nombre_con_parentesis_propios_y_una_linea_sin_cantidad():
    items = cotizacion.items_de_texto(
        "PAPEL BOND (A4) (10 MILLAR)\nALGO SIN CANTIDAD\nCABLE (2.5 M) [CALIBRE 12]")
    assert [(i["descripcion"], i["cantidad"], i["unidad"]) for i in items] == [
        ("PAPEL BOND (A4)", Decimal("10"), "MILLAR"),
        ("CABLE", Decimal("2.5"), "M"),
    ]


@pytest.mark.parametrize("crudo, esperado", [
    ("4", Decimal("4")), ("4,50", Decimal("4.50")), (" 8.5 ", Decimal("8.5")),
    ("", None), ("abc", None), ("-1", None), ("NaN", None), ("Infinity", None),
])
def test_los_precios_del_formulario(crudo, esperado):
    assert cotizacion.a_decimal(crudo) == esperado


def test_el_total_de_la_sc_5884_es_el_calculado_a_mano():
    items = cotizacion.items_de_texto(DESCRIPCION)
    for it, (_, precio) in zip(items, PRECIOS):
        it["precio_unitario"] = Decimal(precio)
    assert cotizacion.subtotal(items[8]) == Decimal("459.00")    # 54 L x 8.50
    assert cotizacion.total(items) == Decimal("12115.20")
    assert cotizacion.monto(cotizacion.total(items)) == "12,115.20"


def test_un_item_sin_precio_no_suma_ni_cuenta_como_cero():
    items = [{"cantidad": Decimal(2), "precio_unitario": None},
             {"cantidad": Decimal(3), "precio_unitario": Decimal("1.10")}]
    assert cotizacion.subtotal(items[0]) is None
    assert cotizacion.total(items) == Decimal("3.30")


def test_solo_las_contrataciones_menores_son_cotizacion():
    assert cotizacion.es_cotizacion_menor("cotizacion")
    assert cotizacion.es_cotizacion_menor("CM")
    assert not cotizacion.es_cotizacion_menor("LP")
    assert not cotizacion.es_cotizacion_menor(None)


def test_el_concepto_no_se_corta_a_media_palabra():
    corto = concepto_corto(OBJETO)
    assert corto == ("ADQUISICION DE ALIMENTOS NO PERECIBLE - PARA LA GERENCIA REGIONAL "
                     "DE GESTION DEL RIESGO DE DESASTRES")
    largo = concepto_corto("PALABRA " * 60)
    assert largo.endswith("PALABRA…")


def test_la_referencia_es_la_nomenclatura_y_no_el_id_interno():
    prop = {"id": 1, "nomenclatura": "COT-5884-2026-GOREMAD", "objeto": OBJETO}
    assert referencia(prop) == "COT-5884-2026-GOREMAD"


def test_el_goremad_usa_su_formato_de_declaracion_y_los_demas_el_generico():
    assert formato_dj({"nomenclatura": "COT-5884-2026-GOREMAD"}) == "goremad"
    assert formato_dj({"entidad": "Gobierno Regional de Madre de Dios"}) == "goremad"
    assert formato_dj({"nomenclatura": "CM-12-2026-MPT", "entidad": "Municipalidad"}) is None


async def test_la_cotizacion_y_la_dj_salen_con_un_ampersand_en_la_razon_social(
        tmp_path, monkeypatch):
    """'K & A SISTEMAS S.A.C.' es una de las empresas de verdad: un '&' sin
    escapar tumbaba el PDF entero."""
    import shared.pdf_firmable as pf
    from shared.documentos_menores import generar_cotizacion, generar_dj

    async def _sin_imagenes(_):
        return {}
    monkeypatch.setattr(pf, "rutas_de", _sin_imagenes)
    monkeypatch.setattr(pf, "SALIDA", tmp_path)

    emp = {"id": 9, "razon_social": "K & A SISTEMAS S.A.C.", "ruc": "20490765680",
           "direccion": "Jr. Loreto 123", "representante_legal": "ANA <PEREZ>",
           "dni_representante": "40000000", "telefono": "982000000",
           "email": "ventas@ka.pe"}
    prop = {"id": 77, "nomenclatura": "COT-5884-2026-GOREMAD", "objeto": OBJETO,
            "entidad": "Gobierno Regional de Madre de Dios"}
    items = cotizacion.items_de_texto(DESCRIPCION)
    for it, (marca, precio) in zip(items, PRECIOS):
        it.update(marca=marca, precio_unitario=Decimal(precio))
        it["subtotal"] = cotizacion.subtotal(it)

    ruta = await generar_cotizacion(prop, emp, items, cotizacion.CONDICIONES_INICIALES,
                                    "00212345678901234567", con_dnie=True)
    texto = _texto_pdf(Path(ruta).read_bytes())
    assert "K & A SISTEMAS S.A.C." in texto
    assert "COT-5884-2026-GOREMAD" in texto
    assert "ARROZ EXTRA" in texto and "Cholo" in texto
    assert "12,115.20" in texto
    assert "00212345678901234567" in texto

    ruta = await generar_dj(prop, emp, "00212345678901234567", con_dnie=True)
    texto = _texto_pdf(Path(ruta).read_bytes())
    assert "ÚNICA Y OBLIGATORIA" in texto
    assert "ANA <PEREZ>" in texto
    assert "00212345678901234567" in texto
    assert "10. Que, la información adjuntada" in texto


# ─── Con base: el flujo de la ficha ──────────────────────

async def _entrar(cliente, usuario):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303


@pytest.fixture
def _salida_temporal(tmp_path, monkeypatch):
    import prep_bot.zip_builder as zb
    import shared.pdf_firmable as pf
    monkeypatch.setattr(pf, "SALIDA", tmp_path)
    monkeypatch.setattr(zb, "EXPEDIENTES_DIR", str(tmp_path))
    return tmp_path


@sin_base
async def test_la_sc_5884_de_postular_a_expediente(cliente, usuario, empresa, marca,
                                                   _salida_temporal):
    from shared.db import connection, kb_set

    lic = f"prueba_cot_{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad,
                                         entidad_tipo, objeto, descripcion, fecha_cierre)
               VALUES ($1, 'gore_portals', 'cotizacion', 'COT-5884-2026-GOREMAD',
                       'Gobierno Regional de Madre de Dios', 'gore', $2, $3,
                       NOW() + INTERVAL '3 days')""",
            lic, OBJETO, DESCRIPCION)
        # Lo que una cotizacion si pide: representante, DNI y domicilio.
        await c.execute(
            """UPDATE empresas SET representante_legal='SINDY ESPIRITU',
                      dni_representante='20427318', direccion='Av. Madre de Dios 1087'
                WHERE id=$1""", empresa)
    await kb_set(empresa, "financiero", "cci", "00212345678901234567", "prueba")
    try:
        await _entrar(cliente, usuario)
        r = await cliente.post("/postular", data={"licitacion_id": lic, "empresa_id": empresa})
        assert r.status_code == 303
        pid = int(r.headers["location"].rsplit("/", 1)[1])

        async with connection() as c:
            items = await c.fetch(
                "SELECT id FROM propuesta_items WHERE propuesta_id=$1 ORDER BY orden", pid)
            preguntas = {q["campo_requerido"] for q in await c.fetch(
                "SELECT campo_requerido FROM preguntas WHERE propuesta_id=$1", pid)}
        assert len(items) == 9
        # Lo que una contratacion menor no pide, no se pregunta.
        assert not preguntas & {"partida_registral", "vigencia_poder"}

        ficha = await cliente.get(f"/propuestas/{pid}")
        assert ficha.status_code == 200
        assert "Cotización por ítems" in ficha.text and "ARROZ EXTRA" in ficha.text
        assert "9 sin precio" in ficha.text
        assert "Partida registral SUNARP" not in ficha.text
        # Sin el rango de mercado que comparaba con obras de S/ 96 mil.
        assert "Cuartil bajo" not in ficha.text

        # Un precio que no es numero no guarda nada.
        malo = {f"precio_{items[0]['id']}": "dos soles"}
        r = await cliente.post(f"/propuestas/{pid}/items", data=malo)
        assert "error=" in r.headers["location"]

        datos = {"plazo_entrega": "03 días calendario", "validez": "30 días calendario",
                 "garantia": "", "forma_pago": "Transferencia (CCI)"}
        for it, (m, p) in zip(items, PRECIOS):
            datos[f"marca_{it['id']}"] = m
            datos[f"precio_{it['id']}"] = p
        r = await cliente.post(f"/propuestas/{pid}/items", data=datos)
        assert "aviso=" in r.headers["location"] and "12%2C115.20" in r.headers["location"]
        async with connection() as c:
            total = await c.fetchval("SELECT precio_ofertado FROM propuestas WHERE id=$1", pid)
        assert round(total, 2) == 12115.20

        pdf = await cliente.get(f"/propuestas/{pid}/cotizacion?modo=dnie")
        assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
        texto = _texto_pdf(pdf.content)
        assert "12,115.20" in texto and "Mirasol" in texto and "03 días calendario" in texto

        dj = await cliente.get(f"/propuestas/{pid}/declaracion-jurada?modo=dnie")
        texto = _texto_pdf(dj.content)
        assert "ÚNICA Y OBLIGATORIA" in texto and "00212345678901234567" in texto

        r = await cliente.post(f"/propuestas/{pid}/expediente")
        assert "aviso=Expediente" in r.headers["location"], r.headers["location"]
        async with connection() as c:
            ruta = await c.fetchval(
                "SELECT expediente_zip_path FROM propuestas WHERE id=$1", pid)
        nombres = set(zipfile.ZipFile(ruta).namelist())
        assert {"05_Cotizacion.pdf", "02_Declaracion_Jurada.pdf",
                "08_Pacto_Integridad.docx"} <= nombres
        assert "03_Experiencia_Postor.docx" not in nombres
        assert "05_Propuesta_Economica.docx" not in nombres
        indice = zipfile.ZipFile(ruta).read("00_INDICE.txt").decode()
        assert "SEACE" not in indice
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lic)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


@sin_base
async def test_los_items_de_otra_propuesta_no_se_tocan(cliente, usuario, empresa, marca):
    """El id del item llega del formulario: no puede alcanzar a otra propuesta."""
    from shared.db import connection

    lic = f"prueba_cot2_{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, tipo, entidad, objeto, descripcion)
               VALUES ($1, 'gore_portals', 'cotizacion', 'ENTIDAD', 'Compra de prueba', $2)""",
            lic, "SAL DE MESA (54 KLG)")
        mia = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'iniciado') RETURNING id", lic, empresa)
    try:
        await cotizacion.sembrar(mia)
        ajeno = (await cotizacion.items_de(mia))[0]["id"]
        # Otra propuesta del mismo usuario intenta escribir en ese item.
        async with connection() as c:
            otra = await c.fetchval(
                "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
                "VALUES ($1, $2, 'iniciado') RETURNING id", lic, empresa)
        await cotizacion.guardar(otra, {ajeno: ("Marca intrusa", Decimal("1"))}, {})
        assert (await cotizacion.items_de(mia))[0]["marca"] is None

        # Y sembrar dos veces no duplica: la segunda no hace nada.
        assert await cotizacion.sembrar(mia) == 0
        assert len(await cotizacion.items_de(mia)) == 1
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lic)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)
