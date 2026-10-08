"""Rellenar la empresa desde la ficha RUC y la constancia del RNP.

Los textos imitan lo que pypdf saca de una ficha real (columnas intercaladas,
etiquetas partidas) y lo que Tesseract lee de una constancia, con datos
inventados: ningun DNI ni nombre de una persona real.
"""
from datetime import date

import pytest

from shared.constancia_rnp import ConstanciaInvalida
from shared.constancia_rnp import parsear_texto as parsear_constancia
from shared.ficha_ruc import FichaInvalida, leer_ficha_ruc
from shared.ficha_ruc import parsear_texto as parsear_ficha
from tests.conftest import sin_base

FICHA = """Información General del Contribuyente
Código y descripción de Tipo de Contribuyente 39  SOCIEDAD ANONIMA CERRADA
Estado del Contribuyente ACTIVO
Condición del Domicilio Fiscal HABIDO
Datos del Contribuyente
Nombre Comercial FERRETERIA EL EJEMPLO
Actividad Económica Principal 4663 - VENTA AL POR MAYOR DE MATERIALES DE
CONSTRUCCIÓN, ARTÍCULOS DE FERRETERÍA
Actividad Económica Secundaria 1 4752 - VENTA AL POR MENOR DE ARTÍCULOS DE FERRETERÍA
Actividad Económica Secundaria 2
4330 - TERMINACIÓN Y ACABADO DE EDIFICIOS
Sistema Emisión Comprobantes de Pago COMPUTARIZADO
Teléfono Fijo 1 -
Teléfono Móvil 1 84 - 987654321
Correo Electrónico 1 VENTAS@EJEMPLO.PE
Domicilio Fiscal
Departamento CUSCO
Provincia CUSCO
Distrito WANCHAQ
Tipo y Nombre Zona URB. LOS PINOS
Tipo y Nombre Vía AV.  LA CULTURA
Reporte de Ficha RUC
FERRETERIA EL EJEMPLO SOCIEDAD ANONIMA CERRADA - FERRETERIA EL EJEMPLO S
20123456789
Lima, 01/10/2026
Página 1 de 4
Nro 1234
Dpto -
Interior B
Representantes Legales
DOC. NACIONAL DE
IDENTIDAD/LE
12345678
QUISPE MAMANI ROSA
ELENA
CUSCO
- - - - - -
GERENTE GENERAL 01/01/1980 15/03/2015 -
Otras Personas Vinculadas
DOC. NACIONAL
DE IDENTIDAD/LE
- 87654321
OTRA PERSONA SOCIA
"""


def test_la_ficha_rellena_lo_que_el_alta_necesita():
    d = parsear_ficha(FICHA)
    assert d["ruc"] == "20123456789"
    # El nombre abreviado que SUNAT anade tras " - " se recorta.
    assert d["razon_social"] == "FERRETERIA EL EJEMPLO SOCIEDAD ANONIMA CERRADA"
    assert d["representante_legal"] == "QUISPE MAMANI ROSA ELENA"
    assert d["dni_representante"] == "12345678"
    assert d["cargo_representante"] == "GERENTE GENERAL"
    assert d["departamento"] == "Cusco"
    assert d["direccion"] == "AV. LA CULTURA 1234 Int. B, URB. LOS PINOS, WANCHAQ, CUSCO, CUSCO"
    assert d["telefono"] == "987654321"
    assert d["email"] == "ventas@ejemplo.pe"
    assert d["rubros"] == [
        "venta al por mayor de materiales de construcción, artículos de ferretería",
        "venta al por menor de artículos de ferretería",
        "terminación y acabado de edificios",
    ]


def test_el_socio_no_se_confunde_con_el_representante():
    """El bloque de "Otras personas vinculadas" se parte distinto; si se leyera
    ese, el expediente saldria firmado por un socio."""
    sin_repre = (FICHA.split("Representantes Legales")[0]
                 + FICHA.split("GERENTE GENERAL 01/01/1980 15/03/2015 -")[1])
    d = parsear_ficha(sin_repre)
    assert d["dni_representante"] is None
    assert d["representante_legal"] is None


def test_un_guion_propio_del_nombre_no_se_recorta():
    texto = FICHA.replace(
        "FERRETERIA EL EJEMPLO SOCIEDAD ANONIMA CERRADA - FERRETERIA EL EJEMPLO S",
        "CONSORCIO NORTE - SUR S.A.C.")
    assert parsear_ficha(texto)["razon_social"] == "CONSORCIO NORTE - SUR S.A.C."


def test_un_pdf_que_no_es_ficha_se_rechaza_con_un_mensaje_claro():
    with pytest.raises(FichaInvalida):
        parsear_ficha("Factura electronica F001-123")
    with pytest.raises(FichaInvalida):
        leer_ficha_ruc(b"no soy un pdf")


CONSTANCIA = """RUC N° 20123456789
REGISTRO NACIONAL DE PROVEEDORES
CONSTANCIA DE INSCRIPCIÓN
PARA SER PARTICIPANTE, POSTOR Y CONTRATISTA
FERRETERIA EL EJEMPLO SOCIEDAD ANONIMA CERRADA
Se encuentra con inscripción vigente en los siguientes registros:
PROVEEDOR DE BIENES
Vigencia : Desde 18/10/2016
PROVEEDOR DE SERVICIOS
Vigencia : Desde 18/10/2016
FECHA IMPRESIÓN: 28/01/2026
"""


def test_la_constancia_da_ruc_y_capitulos_sin_inventar_un_vencimiento():
    d = parsear_constancia(CONSTANCIA)
    assert d["rnp_numero"] == "20123456789"
    assert d["rnp_categoria"] == "Bienes, Servicios"
    assert [c["desde"] for c in d["capitulos"]] == [date(2016, 10, 18)] * 2
    # Vigencia indeterminada: solo "Desde". Una fecha aqui seria inventada.
    assert d["rnp_vigencia"] is None
    assert d["indeterminada"] is True


def test_si_un_capitulo_vence_manda_el_que_vence_antes():
    texto = CONSTANCIA.replace(
        "FECHA IMPRESIÓN",
        "EJECUTOR DE OBRAS\nVigencia : Desde 01/02/2020 Hasta 31/12/2027\n"
        "CONSULTOR DE OBRAS\nVigencia : Desde 01/02/2020 Hasta 30/06/2027\nFECHA IMPRESIÓN")
    d = parsear_constancia(texto)
    assert d["rnp_categoria"] == "Bienes, Servicios, Ejecutor de Obras, Consultor de Obras"
    assert d["rnp_vigencia"] == date(2027, 6, 30)
    assert d["indeterminada"] is False


def test_un_pdf_que_no_es_constancia_se_rechaza():
    with pytest.raises(ConstanciaInvalida):
        parsear_constancia("Reporte de Ficha RUC 20123456789")
    with pytest.raises(ConstanciaInvalida):
        parsear_constancia("REGISTRO NACIONAL DE PROVEEDORES\nNo se encuentra inscrito")


# ─── La ruta: rellena, pero no guarda ────────────────────

async def _entrar(cliente, usuario):
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303


@sin_base
@pytest.mark.asyncio
async def test_subir_la_ficha_rellena_el_alta_y_no_guarda_nada(usuario, cliente, monkeypatch):
    import shared.ficha_ruc
    from shared.db import connection

    monkeypatch.setattr(shared.ficha_ruc, "leer_ficha_ruc", lambda _b: parsear_ficha(FICHA))
    await _entrar(cliente, usuario)
    r = await cliente.post("/empresas/leer-documentos", data={"empresa_id": "0"},
                           files={"ficha": ("ficha.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert r.status_code == 200
    assert 'value="QUISPE MAMANI ROSA ELENA"' in r.text
    assert 'value="12345678"' in r.text
    assert "todavía no se ha guardado nada" in r.text
    async with connection() as c:
        n = await c.fetchval("SELECT COUNT(*) FROM empresas WHERE usuario_id=$1", usuario["id"])
    assert n == 0


@sin_base
@pytest.mark.asyncio
async def test_una_constancia_de_otro_ruc_no_se_aplica(usuario, empresa, cliente, monkeypatch):
    import shared.constancia_rnp

    monkeypatch.setattr(shared.constancia_rnp, "leer_constancia_rnp",
                        lambda _b: parsear_constancia(CONSTANCIA))
    await _entrar(cliente, usuario)
    r = await cliente.post("/empresas/leer-documentos", data={"empresa_id": str(empresa)},
                           files={"constancia": ("c.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert r.status_code == 200
    assert "No se usó" in r.text
    assert 'value="Bienes, Servicios"' not in r.text


@sin_base
@pytest.mark.asyncio
async def test_no_se_puede_leer_documentos_sobre_la_empresa_de_otro(empresa, cliente, marca):
    """La empresa es de `usuario`; entra otra cuenta y prueba con su id."""
    from shared.db import borrar_cuenta, connection, crear_usuario
    from shared.seguridad import hashear_password

    email = f"intruso-{marca}@ejemplo.pe"
    intruso = await crear_usuario(email, hashear_password("ClaveDePrueba123!"), "Intruso")
    try:
        await _entrar(cliente, {"email": email, "password": "ClaveDePrueba123!"})
        r = await cliente.post("/empresas/leer-documentos", data={"empresa_id": str(empresa)},
                               files={"ficha": ("f.pdf", b"%PDF-1.4 x", "application/pdf")})
        assert r.status_code == 303
        assert "no+es+tuya" in r.headers["location"]
        async with connection() as c:
            assert await c.fetchval("SELECT usuario_id FROM empresas WHERE id=$1",
                                    empresa) != intruso["id"]
    finally:
        await borrar_cuenta(intruso["id"])
