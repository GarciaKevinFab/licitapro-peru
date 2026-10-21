"""Los bots de Telegram solo responden al dueno de cada empresa.

QUE PASABA

  Ningun comando de LicitaPrep ni de LicitaWin miraba quien escribia. Bastaba
  con dar con el bot para ver las propuestas, contratos y pagos de TODAS las
  empresas, responder preguntas ajenas (que acaban en la base de conocimiento:
  el CCI donde le pagan a otro), registrar buenas pro y pagos sobre ids
  correlativos, y subir una "firma" a la empresa de cualquiera. Y esa firma se
  copiaba tal cual al disco, sin comprobar que fuera una imagen.

COMO SE PRUEBA

  Llamando a los handlers con un `update` falso: montar la aplicacion de
  Telegram entera no aporta nada, lo que importa es que hace el handler con
  la identidad que le llega. Tres personajes: alguien sin vincular, el dueno y
  un intruso con su propia cuenta vinculada. El intruso tiene que recibir el
  mismo "no encontrado" que un id inexistente.
"""
import io
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tests.conftest import sin_base

pytestmark = [pytest.mark.asyncio, sin_base]


# ─── Update falso ────────────────────────────────────────

class Mensaje:
    """Lo minimo de telegram.Message que usan los handlers."""

    def __init__(self, caption=None, photo=None):
        self.caption = caption
        self.photo = photo or []
        self.textos: list[str] = []
        self.documentos: list[str] = []

    async def reply_text(self, texto, **_):
        self.textos.append(texto)

    async def reply_document(self, document=None, filename=None, **_):
        self.documentos.append(filename)

    @property
    def todo(self) -> str:
        return "\n".join(self.textos)


def _update(chat_id: int | None, **kwargs):
    msg = Mensaje(**kwargs)
    usuario = SimpleNamespace(id=chat_id) if chat_id is not None else None
    return SimpleNamespace(effective_user=usuario, effective_message=msg,
                           message=msg, callback_query=None), msg


def _ctx(*args, bot=None):
    return SimpleNamespace(args=list(args), bot=bot)


def _chat() -> int:
    # Rango alto para no chocar con chats reales ni con otras pruebas.
    return random.randint(9_000_000_000, 9_900_000_000)


# ─── Datos ───────────────────────────────────────────────

@pytest_asyncio.fixture
async def escena(usuario, empresa, marca):
    """Dueno vinculado con propuesta, pregunta y contrato; intruso vinculado."""
    from shared.db import borrar_cuenta, connection, crear_usuario
    from shared.seguridad import hashear_password

    dueno_chat, intruso_chat = _chat(), _chat()
    intruso = await crear_usuario(f"intruso-bot-{marca}@ejemplo.pe",
                                  hashear_password("ClaveDePrueba123!"), "Intruso")
    lic = f"prueba_bots_{marca}"
    async with connection() as c:
        await c.execute("UPDATE usuarios SET telegram_chat_id=$2 WHERE id=$1",
                        usuario["id"], dueno_chat)
        await c.execute("UPDATE usuarios SET telegram_chat_id=$2 WHERE id=$1",
                        intruso["id"], intruso_chat)
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto)
               VALUES ($1, 'gore_portals', 'cotizacion', $1, $2, $3)""",
            lic, f"ENTIDAD {marca}", f"OBJETO SECRETO {marca}")
        prop = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado, preguntas_pendientes) "
            "VALUES ($1, $2, 'preguntas_pendientes', 1) RETURNING id", lic, empresa)
        preg = await c.fetchval(
            """INSERT INTO preguntas (propuesta_id, empresa_id, campo_requerido, pregunta,
                                      kb_categoria, kb_clave)
               VALUES ($1, $2, 'cci', '¿CCI?', 'financiero', 'cci') RETURNING id""",
            prop, empresa)
        contrato = await c.fetchval(
            """INSERT INTO contratos (propuesta_id, licitacion_id, empresa_id,
                                      monto_adjudicado, estado)
               VALUES ($1, $2, $3, 12345, 'adjudicado') RETURNING id""",
            prop, lic, empresa)
        await c.execute(
            "INSERT INTO pagos (contrato_id, concepto, monto, estado) "
            "VALUES ($1, $2, 777, 'pendiente')", contrato, f"CONCEPTO {marca}")
        await c.execute(
            "INSERT INTO plazos (contrato_id, tipo, descripcion, fecha_limite) "
            "VALUES ($1, 'firma', $2, CURRENT_DATE + 2)", contrato, f"PLAZO {marca}")
    try:
        yield SimpleNamespace(dueno=dueno_chat, intruso=intruso_chat, empresa=empresa,
                              propuesta=prop, pregunta=preg, contrato=contrato,
                              marca=marca, usuario=usuario)
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM pagos WHERE contrato_id IN "
                            "(SELECT id FROM contratos WHERE licitacion_id=$1)", lic)
            await c.execute("DELETE FROM plazos WHERE contrato_id IN "
                            "(SELECT id FROM contratos WHERE licitacion_id=$1)", lic)
            await c.execute("DELETE FROM contratos WHERE licitacion_id=$1", lic)
            await c.execute("DELETE FROM preguntas WHERE propuesta_id=$1", prop)
            await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lic)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)
        await borrar_cuenta(intruso["id"])


# ─── LicitaPrep ──────────────────────────────────────────

@pytest.mark.parametrize("comando,args", [
    ("cmd_estado", ()), ("cmd_datos", ()), ("cmd_firma", ()),
    ("cmd_responder", ("1", "x")), ("cmd_aprobar", ("1",)), ("cmd_start", ()),
])
async def test_prep_sin_vincular_solo_explica_como_vincular(escena, comando, args):
    from prep_bot import main as prep

    update, msg = _update(_chat())
    await getattr(prep, comando)(update, _ctx(*args))
    assert len(msg.textos) == 1
    assert "no está vinculado" in msg.todo
    assert escena.marca not in msg.todo


async def test_prep_estado_y_datos_solo_ensenan_lo_propio(escena):
    from prep_bot import main as prep

    update, msg = _update(escena.intruso)
    await prep.cmd_estado(update, _ctx())
    await prep.cmd_datos(update, _ctx())
    await prep.cmd_firma(update, _ctx())
    assert escena.marca not in msg.todo

    update, msg = _update(escena.dueno)
    await prep.cmd_estado(update, _ctx())
    await prep.cmd_datos(update, _ctx())
    assert f"OBJETO SECRETO {escena.marca}" in msg.todo
    assert f"Empresa {escena.marca} SAC" in msg.todo


async def test_prep_una_pregunta_ajena_no_se_puede_responder(escena):
    from prep_bot import main as prep
    from shared.db import connection

    update, msg = _update(escena.intruso)
    await prep.cmd_responder(update, _ctx(str(escena.pregunta), "CCI-DEL-INTRUSO"))
    assert "no encontrada" in msg.todo
    async with connection() as c:
        fila = await c.fetchrow("SELECT respondida, respuesta FROM preguntas WHERE id=$1",
                                escena.pregunta)
    assert not fila["respondida"] and fila["respuesta"] is None

    update, msg = _update(escena.dueno)
    await prep.cmd_responder(update, _ctx(str(escena.pregunta), "CCI-BUENO"))
    async with connection() as c:
        fila = await c.fetchrow("SELECT respondida, respuesta FROM preguntas WHERE id=$1",
                                escena.pregunta)
        estado = await c.fetchval("SELECT estado FROM propuestas WHERE id=$1",
                                  escena.propuesta)
    # El CCI entero va cifrado a la KB; en la pregunta solo queda el final
    # (ver shared.db.kb_para_registro y tests/test_auditoria_cifrado.py).
    assert fila["respondida"] and fila["respuesta"] == "****UENO"
    from shared.db import kb_get
    assert await kb_get(escena.empresa, "financiero", "cci") == "CCI-BUENO"
    assert estado == "listo"


async def test_prep_aprobar_no_cambia_el_estado_ni_promete_un_zip(escena):
    from prep_bot import main as prep
    from shared.db import connection

    async with connection() as c:
        await c.execute("UPDATE propuestas SET preguntas_pendientes=0, estado='revisando' "
                        "WHERE id=$1", escena.propuesta)

    update, msg = _update(escena.intruso)
    await prep.cmd_aprobar(update, _ctx(str(escena.propuesta)))
    assert "no encontrada" in msg.todo and escena.marca not in msg.todo

    update, msg = _update(escena.dueno)
    await prep.cmd_aprobar(update, _ctx(str(escena.propuesta)))
    assert "panel web" in msg.todo
    assert "/descargar" not in msg.todo
    async with connection() as c:
        assert await c.fetchval("SELECT estado FROM propuestas WHERE id=$1",
                                escena.propuesta) == "revisando"


def _png() -> bytes:
    from PIL import Image
    salida = io.BytesIO()
    Image.new("RGB", (20, 10), "black").save(salida, format="PNG")
    return salida.getvalue()


def _bot_con(datos: bytes):
    archivo = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(datos)))
    return SimpleNamespace(get_file=AsyncMock(return_value=archivo))


async def test_prep_no_se_sube_firma_a_una_empresa_ajena(escena, tmp_path, monkeypatch):
    from prep_bot import main as prep
    from shared import archivos
    from shared.db import connection

    monkeypatch.setattr(archivos, "DIRECTORIO", tmp_path)
    foto = [SimpleNamespace(file_id="x")]

    update, msg = _update(escena.intruso, caption=f"firma {escena.empresa}", photo=foto)
    bot = _bot_con(_png())
    await prep.handle_photo(update, _ctx(bot=bot))
    assert "no encontrada" in msg.todo
    bot.get_file.assert_not_called()
    async with connection() as c:
        assert await c.fetchval("SELECT firma_representante_path FROM empresas WHERE id=$1",
                                escena.empresa) is None

    update, msg = _update(escena.dueno, caption=f"firma {escena.empresa}", photo=foto)
    await prep.handle_photo(update, _ctx(bot=_bot_con(_png())))
    assert "guardado" in msg.todo
    async with connection() as c:
        ruta = await c.fetchval("SELECT firma_representante_path FROM empresas WHERE id=$1",
                                escena.empresa)
    # Nombre generado por el servidor, con sufijo aleatorio, dentro del directorio.
    assert ruta and ruta.startswith(str(tmp_path)) and ruta.endswith(".png")
    assert ruta != str(tmp_path / f"empresa_{escena.empresa}_firma.png")


async def test_prep_una_foto_que_no_es_imagen_se_rechaza(escena, tmp_path, monkeypatch):
    from prep_bot import main as prep
    from shared import archivos

    monkeypatch.setattr(archivos, "DIRECTORIO", tmp_path)
    update, msg = _update(escena.dueno, caption=f"sello {escena.empresa}",
                          photo=[SimpleNamespace(file_id="x")])
    await prep.handle_photo(update, _ctx(bot=_bot_con(b"<?php system($_GET[1]); ?>")))
    assert "no es una imagen" in msg.todo
    assert list(tmp_path.iterdir()) == []


async def test_guardar_firma_valida_lo_que_le_llega_por_ruta(escena, tmp_path, monkeypatch):
    """El camino por ruta de archivo tambien pasa por la validacion."""
    from shared import archivos
    from shared.archivos import ArchivoInvalido
    from shared.firma_manager import guardar_firma

    monkeypatch.setattr(archivos, "DIRECTORIO", tmp_path / "firmas")
    malo = tmp_path / "x.png"
    malo.write_bytes(b"no soy una imagen")
    with pytest.raises(ArchivoInvalido):
        await guardar_firma(escena.empresa, "logo", str(malo))


# ─── LicitaWin ───────────────────────────────────────────

@pytest.mark.parametrize("comando,args", [
    ("cmd_contratos", ()), ("cmd_plazos", ()), ("cmd_pagos", ()), ("cmd_resumen", ()),
    ("cmd_ganar", ("1", "100")), ("cmd_factura", ("1", "100")),
    ("cmd_conformidad", ("1",)), ("cmd_entrega", ("1", "x")), ("cmd_pago", ("1", "100")),
])
async def test_win_sin_vincular_solo_explica_como_vincular(escena, comando, args):
    from win_bot import main as win

    update, msg = _update(_chat())
    await getattr(win, comando)(update, _ctx(*args))
    assert len(msg.textos) == 1 and "no está vinculado" in msg.todo


async def test_win_listados_solo_ensenan_lo_propio(escena):
    from win_bot import main as win

    update, msg = _update(escena.intruso)
    for comando in (win.cmd_contratos, win.cmd_plazos, win.cmd_pagos, win.cmd_resumen):
        await comando(update, _ctx())
    assert escena.marca not in msg.todo
    assert "12.345" not in msg.todo and "12,345" not in msg.todo

    update, msg = _update(escena.dueno)
    for comando in (win.cmd_contratos, win.cmd_plazos, win.cmd_pagos, win.cmd_resumen):
        await comando(update, _ctx())
    assert f"ENTIDAD {escena.marca}" in msg.todo
    assert f"PLAZO {escena.marca}" in msg.todo
    assert f"CONCEPTO {escena.marca}" in msg.todo
    assert "Contratos totales: 1" in msg.todo


async def test_win_no_se_registra_nada_sobre_lo_ajeno(escena, monkeypatch):
    from shared.db import connection
    from win_bot import main as win

    generadores = {n: AsyncMock(return_value=None) for n in
                   ("generar_factura", "generar_acta_conformidad",
                    "generar_informe_entrega", "registrar_pago")}
    for nombre, falso in generadores.items():
        monkeypatch.setattr(win, nombre, falso)

    update, msg = _update(escena.intruso)
    c = str(escena.contrato)
    await win.cmd_ganar(update, _ctx(str(escena.propuesta), "999"))
    await win.cmd_factura(update, _ctx(c, "100"))
    await win.cmd_conformidad(update, _ctx(c))
    await win.cmd_entrega(update, _ctx(c, "algo"))
    await win.cmd_pago(update, _ctx(c, "100"))

    assert msg.todo.count("no encontrad") == 5
    for falso in generadores.values():
        falso.assert_not_called()
    async with connection() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM contratos WHERE propuesta_id=$1",
                                   escena.propuesta) == 1
        assert await conn.fetchval("SELECT estado FROM propuestas WHERE id=$1",
                                   escena.propuesta) == "preguntas_pendientes"

    # El dueno si puede: el pago llega a registrarse.
    generadores["registrar_pago"].return_value = 42
    update, msg = _update(escena.dueno)
    await win.cmd_pago(update, _ctx(c, "100"))
    generadores["registrar_pago"].assert_awaited_once_with(escena.contrato, 100.0)
    assert "#42" in msg.todo


async def test_win_el_dueno_recibe_el_documento_y_no_la_ruta(escena, monkeypatch, tmp_path):
    from win_bot import main as win

    archivo = tmp_path / "factura_F001_1.docx"
    archivo.write_bytes(b"docx")
    monkeypatch.setattr(win, "generar_factura", AsyncMock(return_value=str(archivo)))

    update, msg = _update(escena.dueno)
    await win.cmd_factura(update, _ctx(str(escena.contrato), "100"))
    assert msg.documentos == ["factura_F001_1.docx"]
    assert str(tmp_path) not in msg.todo
