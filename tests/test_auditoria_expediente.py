"""El expediente y el paquete de envio nunca salen incompletos en silencio.

Cubre lo que encontro la auditoria del 20/10/2026 en el armado:

  - La validacion fallaba ABIERTA: si el validador se caia, el ZIP se armaba
    sin DNI ni domicilio.
  - El ZIP omitia en silencio lo que no se genero y lo listaba con [x].
  - El nombre `Expediente_<nomenclatura>_<ruc>.zip` chocaba entre usuarios sin
    RUC, y las descargas sueltas se escribian a disco con un nombre fijo.
  - Doble clic en "postular" creaba dos propuestas; un precio "nan" o "1E+30"
    terminaba en un 500.
  - La carta salia con "[REPRESENTANTE]" y la fecha con "October".
"""
import io
import os
import zipfile
from datetime import date
from decimal import Decimal
from urllib.parse import unquote_plus

import pytest

from shared.incompleto import DocumentoIncompleto
from tests.conftest import sin_base

# ─── Sin base: piezas puras ──────────────────────────────


def test_el_nombre_de_descarga_no_rompe_la_cabecera():
    from web.propuestas import nombre_descarga

    nombre = nombre_descarga('Envio_COT-12/2026 "GORE"\r\nX-Inyectada: sí', "zip")
    assert nombre.endswith(".zip")
    assert '"' not in nombre and "\r" not in nombre and "\n" not in nombre
    assert nombre.isascii()
    assert nombre_descarga("", "pdf") == "documento.pdf"


def test_la_fecha_va_en_espanol_sin_depender_del_locale():
    from prep_bot.document_gen import fecha_larga
    assert fecha_larga(date(2026, 10, 20)) == "20 de octubre de 2026"
    assert fecha_larga(date(2026, 1, 5)) == "05 de enero de 2026"


def test_la_referencia_es_la_de_la_licitacion_y_no_el_id_de_la_propuesta():
    from prep_bot.document_gen import referencia
    # En el expediente el dict es la fila de la PROPUESTA: su `id` es el de la
    # propuesta. Sin nomenclatura va el id de la licitacion, nunca "None".
    fila = {"id": 77, "nomenclatura": None, "lic_id": "gore_123", "licitacion_id": "gore_123"}
    assert referencia(fila) == "gore_123"
    assert referencia({**fila, "nomenclatura": "COT-5884-2026-GOREMAD"}) == \
        "COT-5884-2026-GOREMAD"


def test_los_datos_de_la_empresa_mandan_sobre_la_knowledge_base():
    from prep_bot.document_gen import datos_legales
    empresa = {"representante_legal": "ANA NUEVA", "dni_representante": "",
               "direccion": "Jr. Loreto 123"}
    kb = {"legal.representante_legal": "PEDRO VIEJO", "legal.dni_representante": "40000000"}
    legales = datos_legales(empresa, kb)
    assert legales["representante"] == "ANA NUEVA"     # la tabla manda
    assert legales["dni"] == "40000000"                # la KB cubre el hueco
    assert legales["domicilio"] == "Jr. Loreto 123"


def test_sin_representante_no_hay_documento_con_marcador():
    from prep_bot.document_gen import datos_legales, exigir
    legales = datos_legales({"razon_social": "X SAC"}, {})
    with pytest.raises(DocumentoIncompleto) as e:
        exigir("la carta de presentación", legales, "representante", "dni", "domicilio")
    assert "el representante legal" in e.value.mensaje()
    assert "el DNI del representante" in e.value.faltantes


def test_el_zip_se_escribe_entero_o_no_se_toca(tmp_path):
    """Un fallo a mitad no deja un ZIP truncado donde estaba el bueno."""
    from prep_bot.zip_builder import _escribir_zip

    destino = tmp_path / "expediente_propuesta_1.zip"
    _escribir_zip(str(destino), [("01_Carta.docx", b"bueno")], "indice", [])
    with pytest.raises(TypeError):
        _escribir_zip(str(destino), [("01_Carta.docx", b"x"), ("02_Roto", object())],
                      "indice", [])
    assert zipfile.ZipFile(destino).read("01_Carta.docx") == b"bueno"
    assert sorted(os.listdir(tmp_path)) == ["expediente_propuesta_1.zip"]   # sin temporales

    _escribir_zip(str(destino), [("01_Carta.docx", b"x")], "indice", ["Falta la ficha"])
    z = zipfile.ZipFile(destino)
    assert "Falta la ficha" in z.read("00_FALTANTES.txt").decode()


def test_el_nombre_del_expediente_lleva_el_id_de_la_propuesta():
    from prep_bot.zip_builder import ruta_expediente
    assert os.path.basename(ruta_expediente(41)) == "expediente_propuesta_41.zip"
    assert ruta_expediente(41) != ruta_expediente(42)


# ─── Con base ────────────────────────────────────────────

async def _entrar(cliente, usuario):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303


async def _cotizacion(empresa, marca, con_datos=True):
    """Una cotizacion menor con dos items con precio, lista para armar."""
    from shared import cotizacion
    from shared.db import connection, kb_set
    lic = f"audit_{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto,
                   descripcion)
               VALUES ($1, 'gore_portals', 'cotizacion', $2, 'Entidad de prueba',
                   'COMPRA DE PRUEBA', 'PRODUCTO 1 (10 KLG)\nPRODUCTO 2 (5 UNIDAD)')""",
            lic, f"COT-{marca}")
        if con_datos:
            await c.execute(
                """UPDATE empresas SET representante_legal='ANA PEREZ',
                          dni_representante='40000000', direccion='Jr. Loreto 123',
                          rnp_numero='S0000001' WHERE id=$1""", empresa)
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'iniciado') RETURNING id", lic, empresa)
    await kb_set(empresa, "financiero", "cci", "00212345678901234567", "prueba")
    await cotizacion.sembrar(pid)
    items = await cotizacion.items_de(pid)
    await cotizacion.guardar(pid, {it["id"]: (f"MARCA {i}", Decimal("2.50"))
                                   for i, it in enumerate(items, 1)}, {})
    return pid, lic


async def _limpiar(lic):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lic)
        await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


@pytest.fixture
def _salida(tmp_path, monkeypatch):
    import prep_bot.autofill.proposal as pp
    import prep_bot.document_gen as dg
    import prep_bot.zip_builder as zb
    import shared.pdf_firmable as pf
    monkeypatch.setattr(pp, "TEMPLATES_DIR", str(tmp_path / "plantillas"))
    monkeypatch.setattr(pf, "SALIDA", tmp_path / "salida")
    monkeypatch.setattr(zb, "EXPEDIENTES_DIR", str(tmp_path / "expedientes"))
    monkeypatch.setattr(zb, "TEMPLATES_DIR", str(tmp_path / "plantillas"))
    monkeypatch.setattr(dg, "TEMPLATES_DIR", str(tmp_path / "plantillas"))
    monkeypatch.setenv("TEMPLATES_DIR", str(tmp_path / "plantillas"))
    return tmp_path


@sin_base
async def test_si_el_validador_se_cae_no_se_arma_nada(usuario, empresa, cliente, marca,
                                                      monkeypatch):
    """Falla CERRADA: no saber si falta algo no es saber que no falta nada."""
    import prep_bot.zip_builder as zb
    from prep_bot.autofill import validator

    async def _se_cae(_pid):
        raise RuntimeError("base caida")

    llamado = []

    async def _no_deberia(_pid):
        llamado.append(True)

    monkeypatch.setattr(validator, "validar_propuesta", _se_cae)
    monkeypatch.setattr(zb, "generar_expediente_zip", _no_deberia)
    pid, lic = await _cotizacion(empresa, marca)
    try:
        await _entrar(cliente, usuario)
        r = await cliente.post(f"/propuestas/{pid}/expediente")
        assert r.status_code == 303
        assert "No pudimos validar" in unquote_plus(r.headers["location"])
        assert not llamado

        r = await cliente.get(f"/propuestas/{pid}/paquete")
        assert r.status_code == 303
        assert "No pudimos validar" in unquote_plus(r.headers["location"])

        # La ficha se sigue viendo, con el aviso y el boton deshabilitado.
        pagina = await cliente.get(f"/propuestas/{pid}")
        assert pagina.status_code == 200
        assert "No pudimos validar tu propuesta" in pagina.text
    finally:
        await _limpiar(lic)


@sin_base
async def test_un_dato_que_falta_en_un_documento_es_un_mensaje_no_un_500(
        usuario, empresa, cliente, marca, monkeypatch):
    import prep_bot.zip_builder as zb

    async def _incompleto(_pid):
        raise DocumentoIncompleto("el Anexo 07", ["el CCI"])

    monkeypatch.setattr(zb, "generar_expediente_zip", _incompleto)
    pid, lic = await _cotizacion(empresa, marca)
    try:
        await _entrar(cliente, usuario)
        r = await cliente.post(f"/propuestas/{pid}/expediente")
        assert r.status_code == 303
        destino = unquote_plus(r.headers["location"])
        assert "No se puede generar el Anexo 07: falta el CCI" in destino
    finally:
        await _limpiar(lic)


@sin_base
async def test_el_expediente_avisa_lo_que_no_salio_y_no_lo_marca_como_incluido(
        usuario, empresa, cliente, marca, monkeypatch, _salida):
    import prep_bot.autofill.annexes as anexos
    from shared.db import connection

    async def _falla(*_a, **_k):
        raise RuntimeError("disco lleno")

    monkeypatch.setattr(anexos, "_generar_pacto_integridad", _falla)
    pid, lic = await _cotizacion(empresa, marca)
    try:
        await _entrar(cliente, usuario)
        r = await cliente.post(f"/propuestas/{pid}/expediente")
        destino = unquote_plus(r.headers["location"])
        assert "Expediente listo" in destino and "pacto de integridad" in destino
        async with connection() as c:
            ruta = await c.fetchval(
                "SELECT expediente_zip_path FROM propuestas WHERE id=$1", pid)
        # El nombre lleva el id de la propuesta: dos usuarios sin RUC ya no chocan.
        assert os.path.basename(ruta) == f"expediente_propuesta_{pid}.zip"
        z = zipfile.ZipFile(ruta)
        nombres = z.namelist()
        assert "08_Pacto_Integridad.docx" not in nombres
        assert "[x] 08_Pacto_Integridad" not in z.read("00_INDICE.txt").decode()
        assert "pacto de integridad" in z.read("00_FALTANTES.txt").decode()
        assert "05_Cotizacion.pdf" in nombres

        r = await cliente.get(f"/propuestas/{pid}/descargar")
        assert r.status_code == 200
        assert f'filename="Expediente_COT-{marca}.zip"' in r.headers["content-disposition"]
    finally:
        await _limpiar(lic)


@sin_base
async def test_las_descargas_sueltas_no_dejan_archivos_ni_se_pisan(
        usuario, empresa, cliente, marca, tmp_path, monkeypatch, _salida):
    from shared import llenar_formatos
    monkeypatch.setattr(llenar_formatos, "DIR", tmp_path / "formatos")
    pid, lic = await _cotizacion(empresa, marca)
    try:
        await _entrar(cliente, usuario)
        r = await cliente.get(f"/propuestas/{pid}/cotizacion?modo=dnie")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert f"cotizacion-{pid}-dnie.pdf" in r.headers["content-disposition"]
        r = await cliente.get(f"/propuestas/{pid}/declaracion-jurada?modo=dnie")
        assert r.status_code == 200
        salida = _salida / "salida"
        assert not salida.exists() or not list(salida.iterdir())

        # "Con firma escaneada" sin firma ni sello cargados: no sale un
        # documento "firmado" sin firma.
        for ruta in (f"/propuestas/{pid}/cotizacion?modo=escaneada",
                     f"/propuestas/{pid}/declaracion-jurada?modo=escaneada",
                     f"/propuestas/{pid}/anexo/rnp"):
            r = await cliente.get(ruta)
            assert r.status_code == 303, ruta
            assert "sube la firma y el sello" in unquote_plus(r.headers["location"]).lower()
    finally:
        await _limpiar(lic)


@sin_base
async def test_el_paquete_sin_rnp_no_se_arma(usuario, empresa, cliente, marca, tmp_path,
                                            monkeypatch):
    from PIL import Image

    from shared import constancia_rnp, ficha_ruc
    from shared.db import connection

    monkeypatch.setattr(constancia_rnp, "DIR_PDF", tmp_path / "rnp")
    monkeypatch.setattr(ficha_ruc, "DIR_PDF", tmp_path / "ficha")
    for nombre in ("f.png", "s.png"):
        Image.new("RGB", (200, 80), "black").save(tmp_path / nombre)
    pid, lic = await _cotizacion(empresa, marca)
    async with connection() as c:
        await c.execute("UPDATE empresas SET firma_representante_path=$2, "
                        "sello_empresa_path=$3 WHERE id=$1", empresa,
                        str(tmp_path / "f.png"), str(tmp_path / "s.png"))
    try:
        await _entrar(cliente, usuario)
        r = await cliente.get(f"/propuestas/{pid}/paquete")
        assert r.status_code == 303
        destino = unquote_plus(r.headers["location"])
        assert "constancia del RNP" in destino and "ficha RUC" in destino
        assert "confirmar_paquete" not in destino      # bloquea: no hay "bajar igual"
        r = await cliente.get(f"/propuestas/{pid}/paquete?confirmar=1")
        assert r.status_code == 303                    # ni forzandolo
    finally:
        await _limpiar(lic)


@sin_base
async def test_doble_clic_en_postular_no_crea_dos_propuestas(usuario, empresa, cliente, marca):
    from shared.db import connection
    lic = f"audit_doble_{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, tipo, entidad, objeto)
               VALUES ($1, 'gore_portals', 'cotizacion', 'E', 'O')""", lic)
        unico = await c.fetchval(
            """SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE tablename='propuestas'
                  AND indexdef ILIKE '%UNIQUE%' AND indexdef ILIKE '%licitacion_id%'
                  AND indexdef ILIKE '%empresa_id%')""")
    try:
        await _entrar(cliente, usuario)
        datos = {"licitacion_id": lic, "empresa_id": str(empresa)}
        primera = await cliente.post("/postular", data=datos)
        segunda = await cliente.post("/postular", data=datos)
        assert primera.headers["location"] == segunda.headers["location"]
        if unico:
            # Con el indice UNIQUE, ni dos a la vez: la que choca re-selecciona.
            import asyncio
            await asyncio.gather(*(cliente.post("/postular", data=datos) for _ in range(3)))
        async with connection() as c:
            n = await c.fetchval("SELECT COUNT(*) FROM propuestas WHERE licitacion_id=$1", lic)
        assert n == 1
    finally:
        await _limpiar(lic)


@sin_base
async def test_postular_a_una_licitacion_que_no_existe_es_404(usuario, empresa, cliente):
    await _entrar(cliente, usuario)
    r = await cliente.post("/postular", data={"licitacion_id": "no_existe_jamas",
                                              "empresa_id": str(empresa)})
    assert r.status_code == 404
    assert "no existe" in r.text


@sin_base
async def test_precios_imposibles_son_un_mensaje_y_no_un_500(usuario, empresa, cliente, marca):
    from shared import cotizacion
    from shared.db import connection
    pid, lic = await _cotizacion(empresa, marca)
    try:
        await _entrar(cliente, usuario)
        for malo in ("nan", "inf", "-5", "1E+30"):
            r = await cliente.post(f"/propuestas/{pid}/precio", data={"precio": malo})
            assert r.status_code == 303 and "error=" in r.headers["location"], malo
        async with connection() as c:
            precio = await c.fetchval("SELECT precio_ofertado FROM propuestas WHERE id=$1", pid)
        assert precio == pytest.approx(37.5)          # el de los items, intacto

        item = (await cotizacion.items_de(pid))[0]
        r = await cliente.post(f"/propuestas/{pid}/items",
                               data={f"precio_{item['id']}": "1E+30"})
        assert r.status_code == 303
        assert "demasiado grande" in unquote_plus(r.headers["location"])
        r = await cliente.post(f"/propuestas/{pid}/items/nuevo",
                               data={"descripcion": "X", "cantidad": "1E+30"})
        assert "demasiado grande" in unquote_plus(r.headers["location"])
    finally:
        await _limpiar(lic)


@sin_base
async def test_si_la_ia_falla_no_se_dice_redactada_con_ia(empresa, marca, monkeypatch,
                                                          _salida):
    """La plantilla de respaldo no se vende como un documento de IA, y el
    global ANTHROPIC_KEY ya no se toca para forzarla."""
    import prep_bot.autofill.proposal as pp
    from shared import ia
    from web.propuestas import _propuesta_tecnica

    async def _cuota(_uid):
        return {"por_plan": True, "permitido": True}

    async def _falla(*_a, **_k):
        raise RuntimeError("API caida")

    monkeypatch.setattr(ia, "cuota", _cuota)
    monkeypatch.setattr(ia, "disponible", lambda: True)
    monkeypatch.setattr(pp.ia, "pedir_json", _falla)
    clave_antes = pp.ANTHROPIC_KEY
    prop = {"id": 999000 + int(marca[:3], 16), "emp_id": empresa, "licitacion_id": "x",
            "objeto": "COMPRA DE PRUEBA", "entidad": "E", "tipo": "licitacion",
            "nomenclatura": "LP-1-2026", "descripcion": "", "monto_referencial": 1000}
    aviso = await _propuesta_tecnica(1, prop)
    assert "con IA" not in aviso.replace("la IA no respondió", "")
    assert "plantilla" in aviso
    assert pp.ANTHROPIC_KEY == clave_antes


@sin_base
async def test_las_preguntas_se_generan_sin_pedir_dos_conexiones(empresa, marca, monkeypatch):
    """`generar_preguntas_propuesta` pedia kb_get/kb_set con una conexion ya
    tomada: con el pool lleno, la app se colgaba sin error."""
    import prep_bot.questioner as q
    from shared import db

    abiertas = 0
    maximo = 0
    original = db.connection

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _contando():
        nonlocal abiertas, maximo
        abiertas += 1
        maximo = max(maximo, abiertas)
        try:
            async with original() as conn:
                yield conn
        finally:
            abiertas -= 1

    lic = f"audit_preg_{marca}"
    async with original() as c:
        await c.execute("""INSERT INTO licitaciones (id, fuente, tipo, entidad, objeto)
                           VALUES ($1, 'gore_portals', 'cotizacion', 'E', 'O')""", lic)
        pid = await c.fetchval("INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
                               "VALUES ($1, $2, 'iniciado') RETURNING id", lic, empresa)
    for modulo in (q, db):
        monkeypatch.setattr(modulo, "connection", _contando)
    try:
        creadas = await q.generar_preguntas_propuesta(pid, empresa)
        assert creadas                                  # la empresa de prueba no tiene datos
        assert maximo == 1
        maximo = 0
        await q.procesar_respuesta(creadas[0]["id"], "ANA PEREZ")
        assert maximo == 1
    finally:
        await _limpiar(lic)


def test_un_zip_en_memoria_lleva_los_avisos_dentro():
    from web.propuestas import _zip_en_memoria
    z = zipfile.ZipFile(io.BytesIO(_zip_en_memoria([("01_SC.pdf", b"%PDF")], ["Va sin TDR"])))
    assert z.namelist() == ["01_SC.pdf", "00_FALTANTES.txt"]
    z = zipfile.ZipFile(io.BytesIO(_zip_en_memoria([("01_SC.pdf", b"%PDF")], [])))
    assert z.namelist() == ["01_SC.pdf"]
