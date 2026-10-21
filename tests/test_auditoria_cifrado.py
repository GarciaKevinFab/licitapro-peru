"""El CCI, la cuenta corriente y el DNI no quedan en claro en la base.

Cubre lo que encontro la auditoria del 20/10/2026 en `knowledge_base.valor`:
un volcado de la base se llevaba los datos bancarios de cada empresa tal cual.

  - Lo sensible se cifra al guardar y se descifra al leer, sin que el resto
    del codigo cambie: kb_get y kb_todos devuelven el valor en claro.
  - Lo viejo en claro se sigue leyendo (el despliegue no para nada).
  - tools/cifrar_knowledge_base.py cifra lo existente y es idempotente.
  - La copia que quedaba en `preguntas.respuesta` se tapa.
"""
import pytest
import pytest_asyncio

from tests.conftest import sin_base

CCI = "00219300123456789012"


# ─── Sin base: piezas puras ──────────────────────────────

def test_cifrar_y_descifrar_vuelve_al_mismo_valor():
    from shared.db import PREFIJO_CIFRADO, kb_cifrar_valor, kb_descifrar_valor

    guardado = kb_cifrar_valor("financiero", "cci", CCI)
    assert guardado.startswith(PREFIJO_CIFRADO)
    assert CCI not in guardado
    assert kb_descifrar_valor(guardado) == CCI
    # Fernet lleva IV aleatorio: dos cifrados del mismo CCI no se parecen, y
    # asi no se puede saber mirando la base que dos empresas comparten cuenta.
    assert kb_cifrar_valor("financiero", "cci", CCI) != guardado


def test_cifrar_es_idempotente():
    from shared.db import kb_cifrar_valor
    una = kb_cifrar_valor("legal", "dni_representante", "40000000")
    assert kb_cifrar_valor("legal", "dni_representante", una) == una


def test_lo_que_no_es_sensible_queda_igual():
    from shared.db import kb_cifrar_valor
    assert kb_cifrar_valor("financiero", "entidad_bancaria", "BCP") == "BCP"
    assert kb_cifrar_valor("legal", "representante_legal", "ANA") == "ANA"
    assert kb_cifrar_valor("financiero", "cci", None) is None


def test_un_valor_viejo_en_claro_se_devuelve_tal_cual():
    from shared.db import kb_descifrar_valor
    assert kb_descifrar_valor(CCI) == CCI
    assert kb_descifrar_valor("") == ""
    assert kb_descifrar_valor(None) is None


def test_con_otra_clave_no_devuelve_el_texto_cifrado(monkeypatch):
    """Si se rota LICITAPRO_SECRET_KEY sin re-cifrar, el dato cuenta como no
    sabido. Lo que no puede pasar es que el texto cifrado acabe en el anexo
    como si fuera el CCI."""
    from shared.db import kb_cifrar_valor, kb_descifrar_valor
    guardado = kb_cifrar_valor("financiero", "cci", CCI)
    monkeypatch.setenv("LICITAPRO_SECRET_KEY", "otra-clave-que-no-es-la-de-antes")
    assert kb_descifrar_valor(guardado) is None


def test_el_registro_de_la_pregunta_solo_guarda_el_final():
    from shared.db import kb_para_registro
    assert kb_para_registro("financiero", "cci", CCI) == "****9012"
    assert kb_para_registro("legal", "dni_representante", "123") == "****"
    assert kb_para_registro("financiero", "entidad_bancaria", "BCP") == "BCP"
    assert kb_para_registro(None, None, "x") == "x"


# ─── Con base ────────────────────────────────────────────

async def _valor_crudo(empresa_id, categoria, clave):
    from shared.db import connection
    async with connection() as c:
        return await c.fetchval(
            "SELECT valor FROM knowledge_base WHERE empresa_id=$1 AND categoria=$2 AND clave=$3",
            empresa_id, categoria, clave)


@sin_base
async def test_guardar_y_leer_el_cci_da_lo_mismo_y_la_base_no_lo_ve(empresa):
    from shared.db import kb_get, kb_set, kb_todos

    await kb_set(empresa, "financiero", "cci", CCI)
    await kb_set(empresa, "financiero", "entidad_bancaria", "BCP")

    assert await kb_get(empresa, "financiero", "cci") == CCI
    sabido = await kb_todos(empresa)
    assert sabido[("financiero", "cci")] == CCI
    assert sabido[("financiero", "entidad_bancaria")] == "BCP"

    crudo = await _valor_crudo(empresa, "financiero", "cci")
    assert CCI not in crudo and "9012" not in crudo
    # El nombre del banco queda en claro a proposito (ver shared/db.py).
    assert await _valor_crudo(empresa, "financiero", "entidad_bancaria") == "BCP"


@sin_base
async def test_un_valor_viejo_en_claro_se_sigue_leyendo(empresa):
    from shared.db import connection, kb_get, kb_todos
    async with connection() as c:
        await c.execute(
            """INSERT INTO knowledge_base (empresa_id, categoria, clave, valor)
               VALUES ($1, 'legal', 'dni_representante', '40000000')""", empresa)
    assert await kb_get(empresa, "legal", "dni_representante") == "40000000"
    assert (await kb_todos(empresa))[("legal", "dni_representante")] == "40000000"


@sin_base
async def test_los_documentos_reciben_el_valor_en_claro(empresa):
    """document_gen y obtener_datos_empresa_completos leian la tabla con SQL
    propio: con el valor cifrado habrian impreso 'fernet:...' en el anexo."""
    from prep_bot.document_gen import _recopilar_datos
    from shared.db import kb_set
    from shared.knowledge_base import obtener_datos_empresa_completos

    await kb_set(empresa, "financiero", "cci", CCI)
    assert (await _recopilar_datos(empresa))["financiero.cci"] == CCI
    assert (await obtener_datos_empresa_completos(empresa))["financiero.cci"] == CCI


@pytest_asyncio.fixture
async def pregunta_cci(empresa, marca):
    """Una pregunta de CCI ya respondida con el codigo viejo (en claro)."""
    from shared.db import connection
    lic = f"prueba_cifrado_{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, tipo, nomenclatura, entidad, objeto)
               VALUES ($1, 'gore_portals', 'cotizacion', $1, 'ENTIDAD', 'OBJETO')""", lic)
        prop = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'listo') RETURNING id", lic, empresa)
        preg = await c.fetchval(
            """INSERT INTO preguntas (propuesta_id, empresa_id, campo_requerido, pregunta,
                                      kb_categoria, kb_clave, respuesta, respondida)
               VALUES ($1, $2, 'cci', '¿CCI?', 'financiero', 'cci', $3, TRUE)
               RETURNING id""", prop, empresa, CCI)
    try:
        yield preg
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM preguntas WHERE propuesta_id=$1", prop)
            await c.execute("DELETE FROM propuestas WHERE id=$1", prop)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


@sin_base
async def test_el_script_cifra_lo_existente_y_es_idempotente(empresa, pregunta_cci):
    from shared.db import PREFIJO_CIFRADO, connection, kb_get
    from tools.cifrar_knowledge_base import cifrar_existentes

    async with connection() as c:
        await c.execute(
            """INSERT INTO knowledge_base (empresa_id, categoria, clave, valor)
               VALUES ($1, 'financiero', 'cci', $2),
                      ($1, 'financiero', 'entidad_bancaria', 'BCP')""", empresa, CCI)

        # --dry-run: cuenta, no escribe.
        n_kb, n_preg = await cifrar_existentes(c, aplicar=False)
        assert n_kb >= 1 and n_preg >= 1
        assert await _valor_crudo(empresa, "financiero", "cci") == CCI

        await cifrar_existentes(c, aplicar=True)
        crudo = await _valor_crudo(empresa, "financiero", "cci")
        assert crudo.startswith(PREFIJO_CIFRADO) and CCI not in crudo
        assert await _valor_crudo(empresa, "financiero", "entidad_bancaria") == "BCP"
        assert await c.fetchval("SELECT respuesta FROM preguntas WHERE id=$1",
                                pregunta_cci) == "****9012"

        # Segunda pasada: nada que hacer, y el valor no cambia.
        assert await cifrar_existentes(c, aplicar=True) == (0, 0)
        assert await _valor_crudo(empresa, "financiero", "cci") == crudo

    assert await kb_get(empresa, "financiero", "cci") == CCI


@sin_base
async def test_responder_la_pregunta_no_deja_el_cci_en_claro(empresa, pregunta_cci):
    from shared.db import connection, kb_get, responder_pregunta

    await responder_pregunta(pregunta_cci, CCI)
    async with connection() as c:
        respuesta = await c.fetchval("SELECT respuesta FROM preguntas WHERE id=$1",
                                     pregunta_cci)
    assert respuesta == "****9012"
    assert await kb_get(empresa, "financiero", "cci") == CCI
    assert CCI not in await _valor_crudo(empresa, "financiero", "cci")


@pytest.mark.parametrize("campo", [("financiero", "cci"), ("financiero", "cuenta_corriente"),
                                   ("legal", "dni_representante")])
def test_los_tres_campos_sensibles_estan_en_la_lista(campo):
    from shared.db import CAMPOS_KB_CIFRADOS
    assert campo in CAMPOS_KB_CIFRADOS
