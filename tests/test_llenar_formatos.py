"""Llenar la SC y la DJ que manda la entidad, sobre su propio PDF.

Los PDF de prueba se dibujan aqui con reportlab imitando la estructura de los
del GOREMAD (tabla con lineas, etiquetas "Señores :", condiciones al pie, DJ
con lineas punteadas), con datos inventados. Lo llenado se reconoce por la
tinta azul: el texto original queda debajo y el extractor lo mezcla.
"""
import io
from decimal import Decimal
from typing import ClassVar

import pdfplumber
import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from shared.llenar_formatos import (
    TINTA,
    FormatoNoReconocido,
    llenar_declaracion_jurada,
    llenar_solicitud,
)
from tests.conftest import sin_base

ALTO = A4[1]
EMPRESA = {"razon_social": "FERRETERIA EL EJEMPLO S.A.C.", "ruc": "20123456789",
           "representante_legal": "QUISPE MAMANI ROSA ELENA", "dni_representante": "12345678",
           "cargo_representante": "GERENTE GENERAL", "telefono": "987654321",
           "email": "ventas@ejemplo.pe", "direccion": "Av. La Cultura 1234, Cusco"}


def _y(top):
    return ALTO - top


def _sc(cantidades=(54, 540, 108)) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(41, _y(155), "Señores")
    c.drawString(91, _y(155), ":")
    c.drawString(423, _y(155), "R.U.C.")
    c.drawString(41, _y(170), "Dirección")
    c.drawString(91, _y(170), ":")
    c.drawString(41, _y(186), "Teléfono")
    c.drawString(91, _y(186), ":")
    c.drawString(202, _y(186), "Fax")
    # Tabla: columnas y cabecera de dos lineas, como la del GOREMAD.
    xs = [34, 98, 143, 374, 436, 498, 561]
    filas = [323, 354] + [354 + 30 * (i + 1) for i in range(len(cantidades))]
    for y in filas:
        c.line(xs[0], _y(y), xs[-1], _y(y))
    for x in xs:
        c.line(x, _y(filas[0]), x, _y(filas[-1]))
    fin = filas[-1]
    c.line(436, _y(fin + 21), 561, _y(fin + 21))
    for x in (436, 498, 561):
        c.line(x, _y(fin), x, _y(fin + 21))
    c.setFont("Helvetica-Bold", 9)
    c.drawString(43, _y(338), "CANTIDAD")
    c.drawString(388, _y(343), "MARCA")
    c.drawString(450, _y(338), "PRECIO")
    c.drawString(446, _y(349), "UNITARIO")
    c.drawString(512, _y(338), "PRECIO")
    c.drawString(515, _y(349), "TOTAL")
    c.setFont("Helvetica", 9)
    for i, cant in enumerate(cantidades):
        c.drawString(39, _y(354 + 30 * i + 19), f"{cant:.2f}")
        c.drawString(148, _y(354 + 30 * i + 19), f"PRODUCTO {i + 1}")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(452, _y(fin + 14), "TOTAL")
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(41, _y(fin + 60), "- FORMA DE PAGO:")
    c.drawString(41, _y(fin + 73), "- TIPO DE MONEDA:")
    c.drawString(41, _y(fin + 86), "- VALIDEZ DE LA COTIZACIÓN:")
    c.showPage()
    c.drawString(270, _y(140), "Atentamente,")
    c.save()
    return buf.getvalue()


def _dj() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 10)
    c.drawString(56, _y(255), "Yo, " + "." * 60 + " representante legal de la Empresa")
    c.drawString(56, _y(269), "." * 60 + ", identificado con DNI N° " + "." * 30 + ", RUC")
    c.drawString(56, _y(283), "N° " + "." * 30 + " con domicilio fiscal en " + "." * 60)
    c.drawString(56, _y(297), "Cuenta Corriente Interbancaria " + "." * 70)
    c.drawString(420, _y(724), "Fecha:      /      / 2026")
    c.save()
    return buf.getvalue()


def _tinta(pdf: bytes, pagina: int = 0) -> str:
    """Solo lo escrito en azul: lo que pusimos nosotros."""
    objetivo = tuple(round(v, 2) for v in TINTA)
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        p = doc.pages[pagina]
        azul = p.filter(lambda o: o.get("object_type") != "char" or tuple(
            round(v, 2) for v in (o.get("non_stroking_color") or ())) == objetivo)
        return azul.extract_text() or ""


def _items(cantidades=(54, 540, 108), precio="2.50"):
    return [{"cantidad": Decimal(c), "descripcion": f"PRODUCTO {i + 1}", "marca": f"MARCA {i + 1}",
             "precio_unitario": Decimal(precio), "subtotal": Decimal(c) * Decimal(precio)}
            for i, c in enumerate(cantidades)]


def test_la_solicitud_sale_llena_en_su_propio_formato():
    items = _items()
    total = sum(i["subtotal"] for i in items)
    pdf = llenar_solicitud(_sc(), EMPRESA, items,
                           {"forma_pago": "Transferencia CCI", "validez": "30 días calendario"},
                           "MIL SETECIENTOS CINCUENTA Y CINCO CON 00/100 SOLES", total)
    azul = _tinta(pdf)
    assert "FERRETERIA EL EJEMPLO S.A.C." in azul and "20123456789" in azul
    for esperado in ("MARCA 1", "MARCA 3", "135.00", "1,350.00", "270.00", "1,755.00",
                     "TRANSFERENCIA CCI", "SOLES (S/)", "30 DÍAS CALENDARIO"):
        assert esperado in azul, esperado
    # El formato de la entidad no trae importe en letras: no se le anade.
    assert "SON:" not in azul
    # El texto de la entidad sigue ahi, intacto.
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert "PRODUCTO 2" in doc.pages[0].extract_text()
    # Y el bloque del postor va en la ultima pagina, con su linea de firma.
    assert "QUISPE MAMANI ROSA ELENA" in _tinta(pdf, 1)


def _sc_con_firma_de_la_entidad() -> bytes:
    """La SC con la pagina 2 del GOREMAD: "Atentamente," y la linea de
    "Area de Logistica"."""
    from pypdf import PdfReader, PdfWriter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.showPage()
    c.setFont("Helvetica", 8)
    c.drawString(271, _y(150), "Atentamente,")
    c.line(250, _y(203), 345, _y(203))
    c.drawString(263, _y(215), "Area de Logística")
    c.save()
    base = PdfReader(io.BytesIO(_sc()))
    extra = PdfReader(io.BytesIO(buf.getvalue())).pages[1]
    w = PdfWriter()
    w.add_page(base.pages[0])
    pagina2 = base.pages[1]
    pagina2.merge_page(extra)
    w.add_page(pagina2)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_la_firma_va_junto_a_la_del_area_de_logistica():
    """Como se presenta firmada a mano: a la derecha de la firma de la entidad y
    a su altura, sin un bloque de datos del postor que la SC no pide."""
    items = _items()
    pdf = llenar_solicitud(_sc_con_firma_de_la_entidad(), EMPRESA, items, {}, "X",
                           sum(i["subtotal"] for i in items))
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        p = doc.pages[1]
        texto = p.extract_text()
        nombre = next(w for w in p.extract_words() if w["text"] == "QUISPE")
    assert "DATOS DEL POSTOR" not in texto
    assert nombre["x0"] > 345                 # a la derecha de la linea de la entidad
    assert 203 < nombre["top"] < 240          # a su misma altura


def test_la_presentacion_va_junto_a_la_descripcion_y_la_marca_sola_en_su_columna():
    """Como se anota a mano: "x 330 g" al lado de la descripcion; en MARCA, la marca."""
    items = _items()
    items[1]["presentacion"] = "x 330 g"
    pdf = llenar_solicitud(_sc(), EMPRESA, items, {}, "X", sum(i["subtotal"] for i in items))
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        palabras = doc.pages[0].extract_words()
    nota = next(w for w in palabras if w["text"] == "330")
    marca = next(w for w in palabras if w["text"] == "2" and w["x0"] > 374)
    assert 143 < nota["x0"] < 374                      # dentro de DESCRIPCION
    assert abs(nota["top"] - marca["top"]) < 20        # en la fila del item 2


def test_cada_precio_va_en_la_fila_de_su_item():
    """El total de la fila 2 tiene que estar a la altura del 540.00 de la fila 2."""
    items = _items(precio="1")
    pdf = llenar_solicitud(_sc(), EMPRESA, items, {}, "X", sum(i["subtotal"] for i in items))
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        palabras = doc.pages[0].extract_words()
    cantidad = next(w for w in palabras if w["text"] == "540.00" and w["x0"] < 60)
    total_fila = next(w for w in palabras if w["text"] == "540.00" and w["x0"] > 498)
    assert abs((cantidad["top"] + cantidad["bottom"]) / 2
               - (total_fila["top"] + total_fila["bottom"]) / 2) < 4


def test_si_los_items_no_cuadran_con_la_solicitud_no_se_llena():
    """Un precio en la fila equivocada es peor que el formato propio."""
    with pytest.raises(FormatoNoReconocido, match="3 ítems y la propuesta 2"):
        llenar_solicitud(_sc(), EMPRESA, _items((54, 540)), {}, "X", Decimal(1))
    with pytest.raises(FormatoNoReconocido, match="no coincide"):
        llenar_solicitud(_sc(), EMPRESA, _items((54, 541, 108)), {}, "X", Decimal(1))


def test_un_pdf_que_no_es_una_solicitud_se_rechaza():
    with pytest.raises(FormatoNoReconocido):
        llenar_solicitud(_dj(), EMPRESA, _items(), {}, "X", Decimal(1))
    with pytest.raises(FormatoNoReconocido):
        llenar_solicitud(b"no es pdf", EMPRESA, _items(), {}, "X", Decimal(1))


def test_la_declaracion_jurada_se_llena_en_sus_lineas_punteadas():
    pdf = llenar_declaracion_jurada(_dj(), EMPRESA, "00212345678901234567", "10", "10")
    azul = _tinta(pdf)
    for esperado in ("QUISPE MAMANI ROSA ELENA", "FERRETERIA EL EJEMPLO S.A.C.", "12345678",
                     "20123456789", "Av. La Cultura 1234, Cusco", "00212345678901234567"):
        assert esperado in azul, esperado
    assert azul.count("10") >= 2   # dia y mes de la fecha


def test_sin_cci_su_linea_queda_para_escribirla_a_mano():
    pdf = llenar_declaracion_jurada(_dj(), EMPRESA, None, "10", "10")
    assert "QUISPE MAMANI ROSA ELENA" in _tinta(pdf)
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert "Interbancaria ......" in doc.pages[0].extract_text()


# ─── La web: subir el formato y descargarlo lleno ────────

async def _entrar(cliente, credenciales):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    r = await cliente.post("/entrar", data={"email": credenciales["email"],
                                            "password": credenciales["password"]})
    assert r.status_code == 303


async def _propuesta_con_items(empresa, marca):
    from shared import cotizacion
    from shared.db import connection
    async with connection() as c:
        lic = await c.fetchval(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto,
                   descripcion)
               VALUES ($1, 'gore_portals', 'cotizacion', $2, 'Entidad de prueba',
                   'COMPRA DE PRUEBA', $3) RETURNING id""",
            f"prueba_{marca}", f"COT-{marca}",
            "PRODUCTO 1 (54 KLG)\nPRODUCTO 2 (540 UNIDAD)\nPRODUCTO 3 (108 KLG)")
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'iniciado') RETURNING id", lic, empresa)
    await cotizacion.sembrar(pid)
    items = await cotizacion.items_de(pid)
    await cotizacion.guardar(pid, {it["id"]: (f"MARCA {i}", Decimal("2.50"))
                                   for i, it in enumerate(items, 1)}, {})
    return pid, lic


async def _limpiar(pid, lic):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM propuestas WHERE id=$1", pid)
        await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


@sin_base
@pytest.mark.asyncio
async def test_subir_la_sc_y_descargarla_llena(usuario, empresa, cliente, marca, tmp_path,
                                              monkeypatch):
    from shared import llenar_formatos

    monkeypatch.setattr(llenar_formatos, "DIR", tmp_path)
    pid, lic = await _propuesta_con_items(empresa, marca)
    try:
        await _entrar(cliente, usuario)
        r = await cliente.post(f"/propuestas/{pid}/formato", data={"tipo": "sc"},
                               files={"archivo": ("sc.pdf", _sc(), "application/pdf")})
        assert r.status_code == 303 and "aviso=" in r.headers["location"]
        r = await cliente.get(f"/propuestas/{pid}/cotizacion")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert "MARCA 2" in _tinta(r.content)
        # Con ?propio=1 sale el nuestro, que no lleva la tinta azul del formato.
        r = await cliente.get(f"/propuestas/{pid}/cotizacion?propio=1")
        assert r.status_code == 200 and "MARCA 2" not in _tinta(r.content)
    finally:
        await _limpiar(pid, lic)


@sin_base
@pytest.mark.asyncio
async def test_no_se_sube_un_formato_a_la_propuesta_de_otro(empresa, cliente, marca, tmp_path,
                                                           monkeypatch):
    from shared import llenar_formatos
    from shared.db import borrar_cuenta, crear_usuario
    from shared.seguridad import hashear_password

    monkeypatch.setattr(llenar_formatos, "DIR", tmp_path)
    pid, lic = await _propuesta_con_items(empresa, marca)
    email = f"intruso-{marca}@ejemplo.pe"
    intruso = await crear_usuario(email, hashear_password("ClaveDePrueba123!"), "Intruso")
    try:
        await _entrar(cliente, {"email": email, "password": "ClaveDePrueba123!"})
        r = await cliente.post(f"/propuestas/{pid}/formato", data={"tipo": "sc"},
                               files={"archivo": ("sc.pdf", _sc(), "application/pdf")})
        assert r.status_code == 303 and "no+es+tuya" in r.headers["location"]
        assert not llenar_formatos.guardados(pid)["sc"]
    finally:
        await borrar_cuenta(intruso["id"])
        await _limpiar(pid, lic)


# ─── Automatico: los formatos los baja el puente del portal ──

FICHA_GOREMAD = """
<ul class="list-group">
 <li class="list-group-item"><div class="row"><div class="col">
   <small class="d-block text-muted">SOLICITUD DE COTIZACIÓN</small>
   <div class="text-truncate"><span>PDF</span><span>SC N° 9999-2026-GOREMAD.pdf</span></div>
 </div><div class="col-auto">
   <a class="btn" href="http://portal.ejemplo/solicitud/1/descargar" target="_blank">x</a>
 </div></div></li>
 <li class="list-group-item"><div class="row"><div class="col">
   <small class="d-block text-muted">DECLARACION JURADA</small>
   <div class="text-truncate"><span>PDF</span><span>DJ 2026.pdf</span></div>
 </div><div class="col-auto">
   <a class="btn" href="http://portal.ejemplo/storage/abc.pdf" download="DJ 2026.pdf">x</a>
 </div></div></li>
 <li class="list-group-item"><div class="row"><div class="col">
   <small class="d-block text-muted">ESPECIFICACIONES TECNICAS</small>
   <div class="text-truncate"><span>PDF</span><span>3404.pdf</span></div>
 </div><div class="col-auto">
   <a class="btn" href="http://portal.ejemplo/storage/def.pdf" download="3404.pdf">x</a>
 </div></div></li>
 <li class="list-group-item"><a href="http://portal.ejemplo/solicitud/1/enviar">Enviar</a></li>
</ul>"""


def test_el_cosechador_reconoce_la_sc_la_dj_y_las_eett_de_la_ficha():
    from radar_bot.scrapers.orchestrator import _documentos_cotizacion
    docs = _documentos_cotizacion(FICHA_GOREMAD)
    assert [(d["tipo"], d["nombre"]) for d in docs] == [
        ("sc", "SC N° 9999-2026-GOREMAD.pdf"), ("dj", "DJ 2026.pdf"), ("eett", "3404.pdf")]
    # "Enviar cotizacion" no es un documento.
    assert all("/enviar" not in d["url"] for d in docs)


def _png(tmp_path, nombre, color):
    from PIL import Image
    ruta = tmp_path / nombre
    Image.new("RGB", (200, 80), color).save(ruta)
    return str(ruta)


def _dos_paginas(texto: str = "ANEXO") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for n in (1, 2):
        c.drawString(72, _y(100), f"{texto} pagina {n}")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_un_anexo_se_firma_y_se_sella_en_cada_hoja(tmp_path):
    """La ficha RUC, el RNP y las EETT se mandan con firma y sello en cada
    pagina, como en papel. Sin imagenes el PDF vuelve intacto (para el DNIe)."""
    from shared.llenar_formatos import sellar_paginas
    imagenes = {"firma": _png(tmp_path, "firma.png", "black"),
                "sello": _png(tmp_path, "sello.png", "blue")}
    sellado = sellar_paginas(_dos_paginas(), imagenes)
    with pdfplumber.open(io.BytesIO(sellado)) as doc:
        assert len(doc.pages) == 2
        for pg in doc.pages:
            assert len(pg.images) == 2
            # En la esquina inferior derecha, donde cabe sin tapar el texto.
            assert all(im["x0"] > pg.width / 2 and im["top"] > pg.height * 0.8
                       for im in pg.images)
        assert "ANEXO pagina 2" in doc.pages[1].extract_text()
    limpio = _dos_paginas()
    assert sellar_paginas(limpio, {}) is limpio


def test_el_sello_no_tapa_lo_que_hay_en_la_esquina(tmp_path):
    """La ficha RUC lleva el QR de SUNAT abajo a la derecha y un pie de
    pagina: el sello va a un hueco libre, sin cruzarse con ninguno de los dos."""
    from shared.llenar_formatos import sellar_paginas
    imagenes = {"firma": _png(tmp_path, "firma.png", "black"),
                "sello": _png(tmp_path, "sello.png", "blue")}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(72, _y(100), "Reporte de prueba")
    c.rect(A4[0] - 150, 40, 110, 110, fill=1)          # un "QR" en la esquina
    c.drawString(A4[0] - 200, 20, "Central de consultas")  # pie de pagina
    c.save()
    with pdfplumber.open(io.BytesIO(buf.getvalue())) as doc:
        qr = doc.pages[0].rects[0]
        pie = next(w for w in doc.pages[0].extract_words() if w["text"] == "Central")
    pdf = sellar_paginas(buf.getvalue(), imagenes)

    def se_cruzan(a, b):
        return a["x0"] < b["x1"] and b["x0"] < a["x1"] and a["top"] < b["bottom"]             and b["top"] < a["bottom"]

    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        pg = doc.pages[0]
        assert len(pg.images) == 2
        assert not any(se_cruzan(im, qr) or se_cruzan(im, pie) for im in pg.images)
        assert "Central de consultas" in pg.extract_text()


def test_las_eett_escaneadas_se_achican_para_guardarlas():
    """Un escaneo de varios MB se guarda re-escaneado; uno chico, tal cual."""
    from PIL import Image

    from shared.escaneos import comprimir
    chico = _dos_paginas()
    assert comprimir(chico) == chico
    import random
    random.seed(1)
    paginas = [Image.frombytes("RGB", (1240, 1754), random.randbytes(1240 * 1754 * 3))
               for _ in range(3)]
    buf = io.BytesIO()
    paginas[0].save(buf, "PDF", save_all=True, append_images=paginas[1:], resolution=150)
    grande = buf.getvalue()
    assert len(grande) > 2_500_000
    chico = comprimir(grande)
    assert len(chico) < len(grande) / 3
    with pdfplumber.open(io.BytesIO(chico)) as doc:
        assert len(doc.pages) == 3
        assert abs(doc.pages[0].width - A4[0]) < 2 and abs(doc.pages[0].height - A4[1]) < 2


@sin_base
@pytest.mark.asyncio
async def test_el_puente_baja_las_eett_y_las_guarda_achicadas(marca, monkeypatch):
    from radar_bot.scrapers import orchestrator
    from shared import escaneos
    from shared.db import connection, contenido_documento

    class Respuesta:
        def __init__(self, contenido):
            self.status_code, self.content = 200, contenido

    class Cliente:
        pedidas: ClassVar[list] = []

        async def get(self, url):
            self.pedidas.append(url)
            return Respuesta(_dos_paginas("EETT" if "eett" in url else "SC"))

    monkeypatch.setattr(escaneos, "OBJETIVO", 10)   # todo pesa de mas: se comprime
    lic = f"prueba_{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto)
               VALUES ($1, 'gore_portals', 'cotizacion', $2, 'E', 'O')""", lic, f"COT-{marca}")
    try:
        docs = [{"tipo": "sc", "categoria": None, "nombre": "sc.pdf",
                 "url": "http://portal.ejemplo/solicitud/1/descargar"},
                {"tipo": "eett", "categoria": None, "nombre": "3404.pdf",
                 "url": "http://portal.ejemplo/storage/eett.pdf"},
                {"tipo": "otro", "categoria": None, "nombre": "x.pdf",
                 "url": "http://portal.ejemplo/storage/otro.pdf"}]
        await orchestrator._guardar_documentos(Cliente(), lic, docs)
        assert len(Cliente.pedidas) == 2      # "otro" queda solo con su enlace
        eett = await contenido_documento(lic, "eett")
        assert eett and eett.startswith(b"%PDF")
        with pdfplumber.open(io.BytesIO(bytes(eett))) as doc:
            assert len(doc.pages) == 2 and doc.pages[0].images   # re-escaneada
        # La segunda pasada no vuelve a bajar lo que ya esta.
        await orchestrator._guardar_documentos(Cliente(), lic, docs)
        assert len(Cliente.pedidas) == 2
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


def _dj_con_firma() -> bytes:
    from pypdf import PdfReader, PdfWriter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 10)
    c.drawString(207, _y(750), "." * 60)
    c.drawString(207, _y(764), "Firma, nombre y/o sello del Representante Legal")
    c.save()
    base = PdfReader(io.BytesIO(_dj())).pages[0]
    base.merge_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
    w = PdfWriter()
    w.add_page(base)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_con_firma_escaneada_se_estampan_la_firma_y_el_sello(tmp_path):
    imagenes = {"firma": _png(tmp_path, "firma.png", "black"),
                "sello": _png(tmp_path, "sello.png", "blue")}
    items = _items()
    pdf = llenar_solicitud(_sc(), EMPRESA, items, {}, "X", sum(i["subtotal"] for i in items),
                           imagenes)
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert len(doc.pages[-1].images) == 2
    dj = llenar_declaracion_jurada(_dj_con_firma(), EMPRESA, None, "10", "10", imagenes)
    with pdfplumber.open(io.BytesIO(dj)) as doc:
        assert len(doc.pages[0].images) == 2
    # Sin imagenes (para firmar con DNIe) el documento sale limpio.
    limpio = llenar_solicitud(_sc(), EMPRESA, items, {}, "X", Decimal(1))
    with pdfplumber.open(io.BytesIO(limpio)) as doc:
        assert not doc.pages[-1].images


@sin_base
@pytest.mark.asyncio
async def test_sin_subir_nada_sale_el_formato_que_bajo_el_puente(usuario, empresa, cliente, marca,
                                                                tmp_path, monkeypatch):
    from shared import llenar_formatos
    from shared.db import guardar_documento

    monkeypatch.setattr(llenar_formatos, "DIR", tmp_path)
    pid, lic = await _propuesta_con_items(empresa, marca)
    try:
        await guardar_documento(lic, "sc", "SOLICITUD DE COTIZACIÓN", "sc.pdf",
                                "http://portal.ejemplo/solicitud/1/descargar", _sc())
        await guardar_documento(lic, "eett", "ESPECIFICACIONES TECNICAS", "3404.pdf",
                                "http://portal.ejemplo/storage/def.pdf", None)
        await _entrar(cliente, usuario)
        pagina = await cliente.get(f"/propuestas/{pid}")
        assert "del portal" in pagina.text and "3404.pdf" in pagina.text
        r = await cliente.get(f"/propuestas/{pid}/cotizacion")
        assert r.status_code == 200 and "MARCA 2" in _tinta(r.content)
        assert await llenar_formatos.disponibles(pid, lic) == {"sc": "portal", "dj": None,
                                                                "eett": None}
        # Lo subido a mano manda sobre lo del portal.
        llenar_formatos.guardar(pid, "sc", _sc())
        assert (await llenar_formatos.formato(pid, lic, "sc"))[1] == "subido"
    finally:
        await _limpiar(pid, lic)


@sin_base
@pytest.mark.asyncio
async def test_la_misma_dj_se_guarda_una_vez_y_la_purga_no_se_la_quita_a_otra(marca):
    """El GOREMAD adjunta la misma DJ a cada solicitud: se guarda una sola vez."""
    import hashlib

    from shared.db import (
        connection,
        contenido_documento,
        guardar_documento,
        purgar_documentos_viejos,
    )
    dj = _dj()
    sha = hashlib.sha256(dj).hexdigest()
    vieja, abierta = f"djv_{marca}", f"dja_{marca}"
    async with connection() as c:
        for lic, dias in ((vieja, -40), (abierta, 2)):
            await c.execute(
                """INSERT INTO licitaciones (id, fuente, tipo, entidad, objeto, fecha_cierre)
                   VALUES ($1, 'gore_portals', 'cotizacion', 'E', 'O',
                           NOW() + make_interval(days => $2))""", lic, dias)
    try:
        await guardar_documento(vieja, "dj", None, None, f"http://x/{vieja}.pdf", dj)
        await guardar_documento(abierta, "dj", None, None, f"http://x/{abierta}.pdf", dj)
        async with connection() as c:
            assert await c.fetchval(
                "SELECT COUNT(*) FROM documento_contenidos WHERE sha256=$1", sha) == 1
        await purgar_documentos_viejos()
        assert await contenido_documento(vieja, "dj") is None
        assert await contenido_documento(abierta, "dj") == dj
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM licitaciones WHERE id = ANY($1::text[])", [vieja, abierta])


@sin_base
@pytest.mark.asyncio
async def test_la_purga_suelta_los_pdf_de_solicitudes_cerradas_sin_propuesta(marca):
    from shared.db import (
        connection,
        contenido_documento,
        guardar_documento,
        purgar_documentos_viejos,
    )
    lic = f"vieja_{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, tipo, entidad, objeto, fecha_cierre)
               VALUES ($1, 'gore_portals', 'cotizacion', 'E', 'O', NOW() - INTERVAL '40 days')""",
            lic)
    try:
        await guardar_documento(lic, "sc", None, None, "http://x/1/descargar", _sc())
        assert await purgar_documentos_viejos() >= 1
        assert await contenido_documento(lic, "sc") is None
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


@sin_base
@pytest.mark.asyncio
async def test_las_eett_se_sueltan_a_los_3_dias_y_la_sc_a_los_30(marca):
    """Las EETT pesan 1-2 MB: se sueltan pronto. La SC y la DJ, poco: a los 30
    dias. Lo que tiene propuesta se queda siempre."""
    from shared.db import (
        connection,
        contenido_documento,
        guardar_documento,
        purgar_documentos_viejos,
    )
    sin, con = f"p5_{marca}", f"c5_{marca}"
    async with connection() as c:
        for lic in (sin, con):
            await c.execute(
                """INSERT INTO licitaciones (id, fuente, tipo, entidad, objeto, fecha_cierre)
                   VALUES ($1, 'mpt_tambopata', 'cotizacion', 'E', 'O',
                           (NOW() AT TIME ZONE 'America/Lima') - INTERVAL '5 days')""", lic)
        emp = await c.fetchval("SELECT id FROM empresas LIMIT 1")
    try:
        for lic in (sin, con):
            await guardar_documento(lic, "sc", None, None, f"http://x/{lic}/sc.pdf", _sc())
            await guardar_documento(lic, "eett", None, None, f"http://x/{lic}/tdr.pdf",
                                    _dos_paginas(lic))
        if emp:
            async with connection() as c:
                await c.execute("INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
                                "VALUES ($1, $2, 'iniciado')", con, emp)
        await purgar_documentos_viejos()
        assert await contenido_documento(sin, "eett") is None
        assert await contenido_documento(sin, "sc") is not None
        if emp:
            assert await contenido_documento(con, "eett") is not None
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE licitacion_id = ANY($1::text[])",
                            [sin, con])
            await c.execute("DELETE FROM licitaciones WHERE id = ANY($1::text[])", [sin, con])


# ─── Envio por el portal: datos para copiar y ZIP de adjuntos ─

@sin_base
@pytest.mark.asyncio
async def test_el_envio_por_el_portal_deja_datos_y_adjuntos_listos(
        usuario, empresa, cliente, marca, tmp_path, monkeypatch):
    import zipfile

    from shared import constancia_rnp, ficha_ruc, llenar_formatos
    from shared.db import connection, guardar_documento

    monkeypatch.setattr(llenar_formatos, "DIR", tmp_path / "formatos")
    monkeypatch.setattr(constancia_rnp, "DIR_PDF", tmp_path / "rnp")
    pid, lic = await _propuesta_con_items(empresa, marca)
    try:
        async with connection() as c:
            await c.execute("UPDATE licitaciones SET url=$2 WHERE id=$1", lic,
                            "http://portal.ejemplo/solicitud/77")
        await guardar_documento(lic, "sc", None, None,
                                "http://portal.ejemplo/solicitud/77/descargar", _sc())
        await guardar_documento(lic, "dj", None, None, "http://portal.ejemplo/storage/dj.pdf",
                                _dj())
        constancia_rnp.guardar_pdf(empresa, _dos_paginas("RNP"))
        await _entrar(cliente, usuario)

        pagina = (await cliente.get(f"/propuestas/{pid}")).text
        assert "http://portal.ejemplo/solicitud/77/enviar" in pagina
        assert 'data-copiar="Jurídica"' in pagina   # el RUC de prueba empieza por 20

        r = await cliente.get(f"/propuestas/{pid}/paquete")
        assert r.status_code == 200, r.headers.get("location")
        assert r.headers["content-type"] == "application/zip"
        zipeado = zipfile.ZipFile(io.BytesIO(r.content))
        nombres = zipeado.namelist()
        # Sin ficha RUC ni EETT guardadas, van los que hay: cotizacion, DJ y RNP.
        assert nombres == ["01_Cotizacion.pdf", "02_Declaracion_Jurada_de_Postor.pdf",
                           "03_Registro_Nacional_de_Proveedores_y_Consulta_RUC.pdf"]
        assert "MARCA 2" in _tinta(zipeado.read(nombres[0]))
        assert len(PdfReader(io.BytesIO(zipeado.read(nombres[2]))).pages) == 2
        assert "no publicadas aún" in pagina

        # Con la ficha RUC subida y las EETT bajadas por el puente van los cuatro
        # que pide el portal: la consulta RUC detras del RNP, en el mismo PDF, y
        # las EETT como ficha tecnica. Los adjuntos llevan firma y sello en CADA hoja.
        monkeypatch.setattr(ficha_ruc, "DIR_PDF", tmp_path / "ficha")
        ficha_ruc.guardar_pdf(empresa, _dos_paginas())
        await guardar_documento(lic, "eett", "ESPECIFICACIONES TECNICAS", "3404.pdf",
                                "http://portal.ejemplo/storage/eett.pdf", _dos_paginas())
        async with connection() as c:
            await c.execute(
                "UPDATE empresas SET firma_representante_path=$2, sello_empresa_path=$3 "
                "WHERE id=$1", empresa, _png(tmp_path, "f.png", "black"),
                _png(tmp_path, "s.png", "blue"))
        r = await cliente.get(f"/propuestas/{pid}/paquete")
        assert r.status_code == 200, r.headers.get("location")
        zipeado = zipfile.ZipFile(io.BytesIO(r.content))
        nombres = zipeado.namelist()
        assert [n[:3] for n in nombres] == ["01_", "02_", "03_", "04_"]
        assert nombres[3] == "04_Ficha_Tecnica.pdf"
        assert len(PdfReader(io.BytesIO(zipeado.read(nombres[2]))).pages) == 4
        r = await cliente.get(f"/propuestas/{pid}/anexo/eett")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        with pdfplumber.open(io.BytesIO(r.content)) as doc:
            assert [len(pg.images) for pg in doc.pages] == [2, 2]
        pagina = (await cliente.get(f"/propuestas/{pid}")).text
        assert f"/propuestas/{pid}/anexo/rnp" in pagina and "listas" in pagina
        assert f"/propuestas/{pid}/anexo/ficha" not in pagina
    finally:
        await _limpiar(pid, lic)


@sin_base
@pytest.mark.asyncio
async def test_sin_precios_no_se_arma_el_paquete(usuario, empresa, cliente, marca):
    from shared.db import connection
    pid, lic = await _propuesta_con_items(empresa, marca)
    try:
        async with connection() as c:
            await c.execute(
                "UPDATE propuesta_items SET precio_unitario=NULL WHERE propuesta_id=$1", pid)
        await _entrar(cliente, usuario)
        r = await cliente.get(f"/propuestas/{pid}/paquete")
        assert r.status_code == 303 and "error=" in r.headers["location"]
    finally:
        await _limpiar(pid, lic)
