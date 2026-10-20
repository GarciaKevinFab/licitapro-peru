"""Robustez de base de datos, subidas, archivos y conversion (auditoria 20/10/2026).

QUE PROTEGE

  - El pool: que no se creen dos al primer uso, que se pueda cerrar, y que las
    funciones de la base de conocimiento no pidan una segunda conexion
    teniendo ya una (con el pool lleno eso colgaba la aplicacion sin error).
  - Las subidas: tope de tamano antes de leer, bombas de descompresion y
    formatos que no son PNG/JPG, Word con relaciones externas o bomba zip.
  - LibreOffice: un PDF viejo no se entrega como nuevo, el codigo de salida
    cuenta, el proceso se mata y se espera, el perfil es propio y se borra.
  - Ficha RUC / constancia RNP: RUC que no se lee, BAJA y NO HABIDO, fechas
    que no se leen, la Consulta RUC de SUNAT, telefonos con guiones.
  - El formulario de empresa: errores de dato que daban 500 o se guardaban
    vacios en silencio.
  - El borrado de cuenta: que no queden en disco membrete, ficha, constancia,
    formatos, paquetes ni expedientes.

Con datos inventados: ningun RUC, DNI ni nombre de una persona real.
"""
import asyncio
import io
import logging
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from tests.conftest import sin_base


async def _entrar(cliente, usuario):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303


def _png(lado=50, formato="PNG") -> bytes:
    salida = io.BytesIO()
    Image.new("RGB", (lado, lado), "white").save(salida, format=formato)
    return salida.getvalue()


# ─── Pool ────────────────────────────────────────────────

@sin_base
async def test_dos_peticiones_al_primer_uso_comparten_un_solo_pool(monkeypatch):
    """Sin candado, cada `get_pool` concurrente creaba su pool y uno quedaba
    huerfano con sus conexiones abiertas."""
    from shared import db

    original = db._pool
    monkeypatch.setattr(db, "_pool", None)
    creados = []
    crear = db.asyncpg.create_pool

    async def contar(*a, **k):
        creados.append(1)
        return await crear(*a, **k)

    monkeypatch.setattr(db.asyncpg, "create_pool", contar)
    try:
        pools = await asyncio.gather(*(db.get_pool() for _ in range(5)))
        assert len(creados) == 1
        assert all(p is pools[0] for p in pools)
        async with db.connection() as c:
            assert await c.fetchval("SELECT 1") == 1
    finally:
        await db.cerrar_pool()
        # Cerrar dos veces no hace nada.
        await db.cerrar_pool()
        assert db._pool is None
        monkeypatch.setattr(db, "_pool", original)


@sin_base
async def test_sin_conexion_libre_se_rinde_con_error_y_no_se_cuelga(monkeypatch):
    from shared import db

    pool = await db.get_pool()
    monkeypatch.setattr(db, "SEGUNDOS_ESPERA_CONEXION", 0.2)
    ocupadas = [await pool.acquire() for _ in range(pool.get_max_size())]
    try:
        with pytest.raises(TimeoutError):
            async with db.connection():
                pass
    finally:
        for c in ocupadas:
            await pool.release(c)


class _Contador:
    """Envuelve `connection` y anota cuantas hay abiertas a la vez."""

    def __init__(self, original):
        self.original, self.abiertas, self.maximo = original, 0, 0

    def __call__(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def envoltura():
            self.abiertas += 1
            self.maximo = max(self.maximo, self.abiertas)
            try:
                async with self.original() as c:
                    yield c
            finally:
                self.abiertas -= 1
        return envoltura()


@sin_base
async def test_buscar_valor_y_kb_usan_una_sola_conexion(empresa, monkeypatch):
    import shared.knowledge_base as kb
    from shared import db

    contador = _Contador(db.connection)
    monkeypatch.setattr(db, "connection", contador)
    monkeypatch.setattr(kb, "connection", contador)

    # La razon social esta en la tabla empresas: el camino que guarda en la KB.
    valor = await kb.buscar_valor(empresa, "legal", "razon_social")
    assert valor and valor.startswith("Empresa ")
    assert contador.maximo == 1

    # Y se guardo en la KB, con la misma conexion.
    async with contador() as c:
        assert await db.kb_get(empresa, "legal", "razon_social", conn=c) == valor
        await db.kb_set(empresa, "legal", "otro", "x", conn=c)
        assert (await db.kb_todos(empresa, conn=c))[("legal", "otro")] == "x"
    assert contador.maximo == 1


@sin_base
async def test_responder_pregunta_no_anida_conexiones(empresa, marca, monkeypatch):
    from shared import db

    lid = f"LIC-ROB-{marca}"
    async with db.connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto, estado)
               VALUES ($1, 'prueba', 'Entidad', 'Objeto', 'convocado')""", lid)
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id) VALUES ($1, $2) RETURNING id",
            lid, empresa)
        preg = await c.fetchval(
            """INSERT INTO preguntas (propuesta_id, empresa_id, pregunta, kb_categoria, kb_clave)
               VALUES ($1, $2, '¿Banco?', 'financiero', 'banco') RETURNING id""", pid, empresa)
    contador = _Contador(db.connection)
    monkeypatch.setattr(db, "connection", contador)
    try:
        await db.responder_pregunta(preg, "Banco de Prueba")
        assert contador.maximo == 1
        monkeypatch.undo()
        assert await db.kb_get(empresa, "financiero", "banco") == "Banco de Prueba"
    finally:
        monkeypatch.undo()
        async with db.connection() as c:
            await c.execute("DELETE FROM propuestas WHERE id=$1", pid)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lid)


# ─── Imagenes ────────────────────────────────────────────

def test_una_bomba_de_descompresion_es_un_error_para_el_usuario(monkeypatch):
    """Antes salia un 500: DecompressionBombError no es OSError."""
    from shared.archivos import ArchivoInvalido, _validar_y_normalizar

    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ArchivoInvalido, match="demasiado grande"):
        _validar_y_normalizar(_png(50), "logo")


def test_solo_png_y_jpg():
    from shared.archivos import ArchivoInvalido, _validar_y_normalizar

    assert _validar_y_normalizar(_png(20, "JPEG"), "logo")[:4] == b"\x89PNG"
    with pytest.raises(ArchivoInvalido):
        _validar_y_normalizar(_png(20, "GIF"), "logo")
    with pytest.raises(ArchivoInvalido):
        _validar_y_normalizar(_png(20, "BMP"), "logo")


@sin_base
async def test_una_imagen_que_falta_en_disco_deja_rastro(empresa, caplog):
    from shared.archivos import rutas_de
    from shared.db import connection

    async with connection() as c:
        await c.execute("UPDATE empresas SET logo_empresa_path=$2 WHERE id=$1",
                        empresa, "data/firmas/no_existe_nunca.png")
    with caplog.at_level(logging.WARNING, logger="shared.archivos"):
        assert await rutas_de(empresa) == {}
    assert "no esta en disco" in caplog.text


# ─── LibreOffice ─────────────────────────────────────────

@pytest.fixture
def _python_como_libreoffice(monkeypatch):
    """`docx_a_pdf` con un "LibreOffice" que es un Python que hace lo que diga
    la prueba. Devuelve una funcion para fijar el script."""
    import shared.pdf_firma as pf

    monkeypatch.setattr(pf, "hay_conversor", lambda: sys.executable)
    vistos = {}

    def fijar(script: str):
        def comando(binario, perfil, destino, ruta_docx):
            vistos["perfil"] = perfil
            salida = str(Path(destino) / (Path(ruta_docx).stem + ".pdf"))
            return [binario, "-c", script, str(perfil), salida]
        monkeypatch.setattr(pf, "_comando", comando)
    fijar.vistos = vistos
    return fijar


@pytest.fixture
def _docx(tmp_path):
    ruta = tmp_path / "carta.docx"
    ruta.write_bytes(b"no importa")
    return ruta


async def test_un_pdf_viejo_no_se_entrega_como_nuevo(_python_como_libreoffice, _docx):
    """El conversor "termina bien" sin escribir nada: antes el PDF de ayer pasaba."""
    from shared.pdf_firma import docx_a_pdf

    viejo = _docx.with_suffix(".pdf")
    viejo.write_bytes(b"%PDF-1.4 de ayer")
    _python_como_libreoffice("pass")
    ruta, mensaje = await docx_a_pdf(str(_docx))
    assert ruta is None and "no generó" in mensaje
    assert not viejo.exists()


async def test_un_codigo_de_salida_distinto_de_cero_es_un_fallo(_python_como_libreoffice, _docx):
    from shared.pdf_firma import docx_a_pdf

    _python_como_libreoffice(
        "import sys; open(sys.argv[2], 'wb').write(b'%PDF-1.4 a medias'); sys.exit(3)")
    ruta, mensaje = await docx_a_pdf(str(_docx))
    assert ruta is None and "falló" in mensaje
    assert not _docx.with_suffix(".pdf").exists()


async def test_conversion_buena_con_perfil_propio_que_se_borra(_python_como_libreoffice, _docx):
    from shared.pdf_firma import docx_a_pdf

    _python_como_libreoffice(
        "import sys, os; os.makedirs(sys.argv[1]); "
        "open(sys.argv[2], 'wb').write(b'%PDF-1.4 nuevo')")
    ruta, _ = await docx_a_pdf(str(_docx))
    assert ruta and Path(ruta).read_bytes().startswith(b"%PDF-")
    perfil = _python_como_libreoffice.vistos["perfil"]
    assert perfil.name.startswith("lo_")
    assert not perfil.exists(), "el perfil de LibreOffice de la llamada quedo en disco"


async def test_dos_conversiones_a_la_vez_usan_perfiles_distintos(
        _python_como_libreoffice, tmp_path, monkeypatch):
    import shared.pdf_firma as pf

    _python_como_libreoffice(
        "import sys, time; time.sleep(0.2); open(sys.argv[2], 'wb').write(b'%PDF-1.4 x')")
    perfiles = []
    comando = pf._comando

    def anotar(binario, perfil, destino, ruta_docx):
        perfiles.append(perfil)
        return comando(binario, perfil, destino, ruta_docx)

    monkeypatch.setattr(pf, "_comando", anotar)
    a, b = tmp_path / "a.docx", tmp_path / "b.docx"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    resultados = await asyncio.gather(pf.docx_a_pdf(str(a)), pf.docx_a_pdf(str(b)))
    assert all(r[0] for r in resultados)
    assert len(set(perfiles)) == 2


async def test_si_tarda_demasiado_se_mata_y_se_espera(_python_como_libreoffice, _docx, monkeypatch):
    import shared.pdf_firma as pf

    monkeypatch.setattr(pf, "SEGUNDOS_TIMEOUT", 0.5)
    procesos = []
    crear = asyncio.create_subprocess_exec

    async def anotar(*a, **k):
        p = await crear(*a, **k)
        procesos.append(p)
        return p

    monkeypatch.setattr(pf.asyncio, "create_subprocess_exec", anotar)
    _python_como_libreoffice("import time; time.sleep(30)")
    ruta, mensaje = await pf.docx_a_pdf(str(_docx))
    assert ruta is None and "tardó" in mensaje
    # Muerto y recogido: sin `wait` quedaba zombi con returncode None.
    assert procesos and procesos[0].returncode is not None


# ─── Hoja membretada ─────────────────────────────────────

def _docx_con_rels(relacion: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Membrete")
    crudo = io.BytesIO()
    doc.save(crudo)
    entrada = zipfile.ZipFile(io.BytesIO(crudo.getvalue()))
    salida = io.BytesIO()
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        for item in entrada.infolist():
            datos = entrada.read(item)
            if item.filename == "word/_rels/document.xml.rels":
                datos = datos.replace(b"</Relationships>", relacion.encode() + b"</Relationships>")
            z.writestr(item, datos)
    return salida.getvalue()


def test_un_word_que_tira_de_internet_se_rechaza():
    from shared import membrete

    imagen_remota = (
        '<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/image" Target="http://169.254.169.254/latest/meta-data" '
        'TargetMode="External"/>')
    with pytest.raises(membrete.MembreteInvalido, match="internet"):
        membrete._validar_docx(_docx_con_rels(imagen_remota))


def test_un_hipervinculo_en_el_pie_si_se_acepta():
    from shared import membrete

    enlace = (
        '<Relationship Id="rId98" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/hyperlink" Target="https://www.ejemplo.pe" TargetMode="External"/>')
    membrete._validar_docx(_docx_con_rels(enlace))


def test_una_bomba_zip_se_rechaza_sin_abrirla(monkeypatch):
    from shared import membrete

    monkeypatch.setattr(membrete, "DESCOMPRIMIDO_MAXIMO", 10_000)
    salida = io.BytesIO()
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:document/>")
        z.writestr("word/media/relleno.bin", b"\0" * 50_000)
    with pytest.raises(membrete.MembreteInvalido, match="demasiado"):
        membrete._validar_docx(salida.getvalue())


async def test_dos_subidas_a_la_vez_no_se_pisan(tmp_path, monkeypatch):
    """El temporal tenia nombre fijo por empresa: dos subidas se pisaban."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    import shared.pdf_firma as pf
    from shared import membrete

    monkeypatch.setattr(membrete, "DIR", tmp_path / "membretes")
    temporales = []

    async def _falso(ruta_docx, carpeta):
        temporales.append(ruta_docx)
        await asyncio.sleep(0.05)
        destino = Path(carpeta) / (Path(ruta_docx).stem + ".pdf")
        c = canvas.Canvas(str(destino), pagesize=A4)
        c.rect(40, A4[1] - 100, 500, 90, fill=1)
        c.save()
        return str(destino), ""

    monkeypatch.setattr(pf, "docx_a_pdf", _falso)
    docx = _docx_con_rels("")
    await asyncio.gather(membrete.guardar(7, docx), membrete.guardar(7, docx))
    assert len(set(temporales)) == 2
    assert sorted(p.name for p in membrete.DIR.iterdir()) == [
        "empresa_7.docx", "empresa_7.json", "empresa_7.pdf"]


# ─── Constancia del RNP ──────────────────────────────────

CONSTANCIA = """RUC N° 20123456789
REGISTRO NACIONAL DE PROVEEDORES
CONSTANCIA DE INSCRIPCIÓN
Proveedor de  Bienes
Vigencia : Desde 18/10/2016
PROVEEDOR DE SERVICIOS
Vigencia : Desde 18/10/2016
"""


def test_el_capitulo_se_reconoce_aunque_el_ocr_cambie_mayusculas():
    from shared.constancia_rnp import parsear_texto

    d = parsear_texto(CONSTANCIA)
    assert d["rnp_categoria"] == "Bienes, Servicios"
    assert d["indeterminada"] is True and d["vigencia_leida"] is True


def test_sin_etiqueta_ruc_no_se_toma_cualquier_numero_de_11_digitos():
    from shared.constancia_rnp import parsear_texto

    texto = CONSTANCIA.replace("RUC N° 20123456789", "Código de verificación 20999999999")
    assert parsear_texto(texto)["ruc"] is None
    texto = CONSTANCIA.replace("RUC N° 20123456789", "Nro. de RUC: 20123456789")
    assert parsear_texto(texto)["ruc"] == "20123456789"


def test_sin_fechas_leidas_no_se_declara_vigencia_indeterminada():
    from shared.constancia_rnp import parsear_texto

    texto = CONSTANCIA.replace("Vigencia : Desde 18/10/2016", "Vigencia : ilegible")
    d = parsear_texto(texto)
    assert d["indeterminada"] is False
    assert d["vigencia_leida"] is False
    assert d["rnp_vigencia"] is None


# ─── Ficha RUC y Consulta RUC ────────────────────────────

def test_un_telefono_con_guiones_propios_no_se_pierde():
    from shared.ficha_ruc import _numero_telefono

    assert _numero_telefono("84 - 959-249-224") == "959249224"
    assert _numero_telefono("82 - 959249224") == "959249224"
    assert _numero_telefono("959-249-224") == "959249224"
    assert _numero_telefono("-") is None


CONSULTA = """Consulta RUC
Resultado de la Búsqueda
Número de RUC:
20612345678 - SOLUCIONES DEL EJEMPLO S.A.C.
Tipo Contribuyente:
SOCIEDAD ANONIMA CERRADA
Nombre Comercial:
-
Fecha de Inscripción: 01/02/2024 Fecha de Inicio de Actividades: 01/02/2024
Estado del Contribuyente:
ACTIVO
Condición del Contribuyente:
HABIDO
Domicilio Fiscal:
AV. LOS ROBLES NRO. 123 BAR. EL EJEMPLO MADRE DE DIOS - TAMBOPATA -
TAMBOPATA
Sistema Emisión de Comprobante: MANUAL/COMPUTARIZADO Actividad Comercio Exterior: SIN ACTIVIDAD
Sistema Contabilidad: MANUAL/COMPUTARIZADO
Actividad(es) Económica(s):
Principal - 4741 - VENTA AL POR MENOR DE ORDENADORES, EQUIPO PERIFÉRICO Y
PROGRAMAS DE INFORMÁTICA
Secundaria 1 - 4651 - VENTA AL POR MAYOR DE ORDENADORES
Comprobantes de Pago c/aut. de impresión (F. 806 u 816):
FACTURA BOLETA DE VENTA
Padrones:
NINGUNO
"""


def test_la_consulta_ruc_de_sunat_tambien_se_lee():
    """Hasta hoy se rechazaba con "no parece una Ficha RUC" y se cargaba a mano."""
    from shared.ficha_ruc import parsear_texto

    d = parsear_texto(CONSULTA)
    assert d["origen"] == "consulta"
    assert d["ruc"] == "20612345678"
    assert d["razon_social"] == "SOLUCIONES DEL EJEMPLO S.A.C."
    assert d["departamento"] == "Madre de Dios"
    assert d["direccion"].startswith("AV. LOS ROBLES NRO. 123")
    assert d["estado"] == "ACTIVO" and d["condicion"] == "HABIDO"
    assert d["rubros"] == [
        "venta al por menor de ordenadores, equipo periférico y programas de informática",
        "venta al por mayor de ordenadores"]
    assert d["representante_legal"] is None and d["nombre_comercial"] is None


def test_la_consulta_con_estado_de_baja_trae_solo_el_estado():
    from shared.ficha_ruc import parsear_texto

    texto = CONSULTA.replace("ACTIVO\n", "BAJA DE OFICIO Fecha de Baja: 01/01/2025\n")
    assert parsear_texto(texto)["estado"] == "BAJA DE OFICIO"


# ─── La ficha de la empresa (rutas) ──────────────────────

@sin_base
async def test_una_constancia_sin_ruc_leido_no_se_acepta(usuario, empresa, cliente, monkeypatch):
    import shared.constancia_rnp as rnp

    monkeypatch.setattr(rnp, "DIR_PDF", Path("/no/se/debe/escribir"))
    monkeypatch.setattr(rnp, "leer_constancia_rnp", lambda _b: rnp.parsear_texto(
        CONSTANCIA.replace("RUC N° 20123456789", "")))
    await _entrar(cliente, usuario)
    r = await cliente.post("/empresas/leer-documentos", data={"empresa_id": str(empresa)},
                           files={"constancia": ("c.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert r.status_code == 200
    assert "No se pudo leer el RUC" in r.text


@sin_base
async def test_la_ficha_no_se_guarda_si_la_constancia_no_vale(
        usuario, empresa, cliente, monkeypatch, tmp_path):
    """Antes la ficha se escribia en disco antes de mirar la constancia."""
    import shared.constancia_rnp as rnp
    import shared.ficha_ruc as ficha
    from shared.db import connection

    async with connection() as c:
        ruc = await c.fetchval("SELECT ruc FROM empresas WHERE id=$1", empresa)
    monkeypatch.setattr(ficha, "DIR_PDF", tmp_path / "ficha")
    monkeypatch.setattr(rnp, "DIR_PDF", tmp_path / "rnp")
    monkeypatch.setattr(ficha, "leer_ficha_ruc", lambda _b: {
        "ruc": ruc, "razon_social": "X", "estado": "ACTIVO", "condicion": "HABIDO"})
    monkeypatch.setattr(rnp, "leer_constancia_rnp", lambda _b: rnp.parsear_texto(CONSTANCIA))
    await _entrar(cliente, usuario)
    r = await cliente.post(
        "/empresas/leer-documentos", data={"empresa_id": str(empresa)},
        files={"ficha": ("f.pdf", b"%PDF-1.4 f", "application/pdf"),
               "constancia": ("c.pdf", b"%PDF-1.4 c", "application/pdf")})
    assert "No se usó" in r.text
    assert not ficha.ruta_pdf(empresa).exists()
    assert not rnp.ruta_pdf(empresa).exists()


@sin_base
@pytest.mark.parametrize("estado, condicion, motivo", [
    ("BAJA DEFINITIVA", "HABIDO", "baja"),
    ("ACTIVO", "NO HABIDO", "NO HABIDO"),
])
async def test_un_ruc_de_baja_o_no_habido_no_se_usa(
        usuario, cliente, monkeypatch, estado, condicion, motivo):
    import shared.ficha_ruc as ficha

    monkeypatch.setattr(ficha, "leer_ficha_ruc", lambda _b: {
        "ruc": "20999999991", "razon_social": "X", "estado": estado, "condicion": condicion})
    await _entrar(cliente, usuario)
    r = await cliente.post("/empresas/leer-documentos", data={"empresa_id": "0"},
                           files={"ficha": ("f.pdf", b"%PDF-1.4 f", "application/pdf")})
    assert r.status_code == 200
    assert motivo in r.text and "No se usó" in r.text


@sin_base
async def test_un_rnp_vencido_se_avisa(usuario, empresa, cliente, monkeypatch, tmp_path):
    import shared.constancia_rnp as rnp
    from shared.db import connection

    async with connection() as c:
        ruc = await c.fetchval("SELECT ruc FROM empresas WHERE id=$1", empresa)
    monkeypatch.setattr(rnp, "DIR_PDF", tmp_path)
    monkeypatch.setattr(rnp, "leer_constancia_rnp", lambda _b: {
        "ruc": ruc, "rnp_numero": ruc, "rnp_categoria": "Bienes",
        "rnp_vigencia": date(2020, 1, 31), "indeterminada": False, "vigencia_leida": True})
    await _entrar(cliente, usuario)
    r = await cliente.post("/empresas/leer-documentos", data={"empresa_id": str(empresa)},
                           files={"constancia": ("c.pdf", b"%PDF-1.4 c", "application/pdf")})
    assert "venció el 31/01/2020" in r.text


@sin_base
async def test_una_imagen_demasiado_grande_se_corta_antes_de_leerla(
        usuario, empresa, cliente, monkeypatch):
    from shared import archivos

    monkeypatch.setattr("web.empresas.MAX_BYTES_IMAGEN", 1000)
    llamado = []
    monkeypatch.setattr("web.empresas.guardar_imagen",
                        lambda *a: llamado.append(a))
    await _entrar(cliente, usuario)
    r = await cliente.post(f"/empresas/{empresa}/imagen", data={"tipo": "logo"},
                           files={"archivo": ("l.png", b"x" * 5000, "image/png")})
    assert r.status_code == 303
    assert "error=" in r.headers["location"] and not llamado
    assert archivos.MAX_BYTES > 1000  # el de verdad no se toco


@sin_base
async def test_un_ruc_repetido_al_editar_es_un_mensaje_y_no_un_500(
        usuario, empresa, cliente, marca):
    from shared.db import connection

    async with connection() as c:
        otra = await c.fetchval(
            """INSERT INTO empresas (razon_social, ruc, usuario_id, activa)
               VALUES ($1, $2, $3, TRUE) RETURNING id""",
            f"Otra {marca} SAC", "21" + marca[:9], usuario["id"])
    await _entrar(cliente, usuario)
    r = await cliente.post("/empresas/guardar", data={
        "empresa_id": str(otra), "razon_social": "Otra", "ruc": "20" + marca[:9]})
    assert r.status_code == 303
    assert "Ya+hay+una+empresa+registrada" in r.headers["location"]


@sin_base
async def test_un_departamento_que_no_existe_se_rechaza(usuario, empresa, cliente):
    from shared.db import connection

    await _entrar(cliente, usuario)
    r = await cliente.post("/empresas/guardar", data={
        "empresa_id": str(empresa), "razon_social": "X", "departamento": "Narnia"})
    assert "departamento" in r.headers["location"]
    r = await cliente.post("/empresas/guardar", data={
        "empresa_id": str(empresa), "razon_social": "X", "departamento": "MADRE DE DIOS"})
    assert "error=" not in r.headers["location"]
    async with connection() as c:
        assert await c.fetchval(
            "SELECT departamento FROM empresas WHERE id=$1", empresa) == "Madre de Dios"


@sin_base
async def test_experiencia_con_datos_que_no_se_entienden_no_se_guarda(usuario, empresa, cliente):
    from shared.db import connection

    await _entrar(cliente, usuario)
    base = {"entidad_contratante": "Municipalidad de Prueba", "objeto_contrato": "Obra"}
    for malo in ({"monto": "mucho"}, {"fecha_inicio": "2026-13-45"},
                 {"fecha_inicio": "2026-05-01", "fecha_fin": "2026-01-01"}):
        r = await cliente.post(f"/empresas/{empresa}/experiencia", data={**base, **malo})
        assert r.status_code == 303 and "error=" in r.headers["location"], malo
    async with connection() as c:
        assert await c.fetchval(
            "SELECT COUNT(*) FROM experiencia WHERE empresa_id=$1", empresa) == 0

    # Un entero suelto SI es un monto aqui.
    r = await cliente.post(f"/empresas/{empresa}/experiencia", data={**base, "monto": "15000"})
    assert "error=" not in r.headers["location"]
    async with connection() as c:
        assert await c.fetchval(
            "SELECT monto FROM experiencia WHERE empresa_id=$1", empresa) == pytest.approx(15000)


@sin_base
async def test_anos_de_experiencia_mal_escritos_vuelven_al_formulario(usuario, empresa, cliente):
    await _entrar(cliente, usuario)
    r = await cliente.post(f"/empresas/{empresa}/equipo",
                           data={"nombre_completo": "Ing. Ejemplo", "anos_experiencia": "cinco"})
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


# ─── Borrar la cuenta borra sus archivos ─────────────────

@sin_base
@pytest.mark.parametrize("como", ["usuario", "panel"])
async def test_borrar_la_cuenta_no_deja_documentos_en_disco(marca, tmp_path, monkeypatch, como):
    import shared.constancia_rnp as rnp
    import shared.ficha_ruc as ficha
    import shared.llenar_formatos as formatos
    from shared import membrete, pdf_firmable
    from shared.admin_cuentas import borrar_cuenta_completa
    from shared.db import borrar_cuenta, connection, crear_usuario
    from shared.seguridad import hashear_password
    from web import propuestas

    monkeypatch.setattr(membrete, "DIR", tmp_path / "membretes")
    monkeypatch.setattr(ficha, "DIR_PDF", tmp_path / "ficha")
    monkeypatch.setattr(rnp, "DIR_PDF", tmp_path / "rnp")
    monkeypatch.setattr(formatos, "DIR", tmp_path / "formatos")
    monkeypatch.setattr(pdf_firmable, "SALIDA", tmp_path / "expedientes")
    monkeypatch.setattr(propuestas, "PAQUETES", tmp_path / "paquetes")

    fila = await crear_usuario(f"archivos-{marca}@ejemplo.pe", hashear_password("ClaveDePrueba123!"))
    lid = f"LIC-ARCH-{marca}"
    async with connection() as c:
        eid = await c.fetchval(
            "INSERT INTO empresas (razon_social, ruc, usuario_id) VALUES ($1, $2, $3) RETURNING id",
            f"Archivos {marca}", "20" + marca[:9], fila["id"])
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto, estado)
               VALUES ($1, 'prueba', 'Entidad', 'Objeto', 'convocado')""", lid)
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id) VALUES ($1, $2) RETURNING id",
            lid, eid)
        zip_expediente = tmp_path / "expedientes" / f"Expediente_CP-{pid}-2026_{eid}.zip"
        await c.execute("UPDATE propuestas SET expediente_zip_path=$2 WHERE id=$1",
                        pid, str(zip_expediente))

    def poner(ruta: Path):
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(b"dato personal")
        return ruta

    de_la_cuenta = [
        *(poner(r) for r in membrete._rutas(eid)),
        poner(ficha.ruta_pdf(eid)), poner(rnp.ruta_pdf(eid)),
        poner(formatos.ruta(pid, "sc")),
        poner(tmp_path / "paquetes" / f"propuesta_{pid}.zip"),
        poner(tmp_path / "expedientes" / f"cotizacion-{pid}.pdf"),
        poner(tmp_path / "expedientes" / f"declaracion-jurada-formato-{pid}.pdf"),
        poner(tmp_path / "expedientes" / f"anexo-{pid}-sc.pdf"),
        poner(tmp_path / "expedientes" / f"envio-{pid}.zip"),
        poner(zip_expediente),
    ]
    # De otras propuestas, con numeros parecidos: no se tocan.
    ajenos = [
        poner(tmp_path / "expedientes" / f"cotizacion-{pid}0.pdf"),
        poner(tmp_path / "expedientes" / f"Expediente_CP-{pid}-2026_20999999999.zip"),
        poner(tmp_path / "paquetes" / f"propuesta_{pid}0.zip"),
    ]

    try:
        if como == "usuario":
            resumen = await borrar_cuenta(fila["id"])
        else:
            resumen = await borrar_cuenta_completa(fila["id"])
        assert resumen["borrada"] is True
        quedan = [str(p) for p in de_la_cuenta if p.exists()]
        assert not quedan, f"quedaron en disco: {quedan}"
        assert all(p.exists() for p in ajenos)
        assert resumen["archivos"] >= len(de_la_cuenta)
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lid)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lid)
        await borrar_cuenta(fila["id"])
