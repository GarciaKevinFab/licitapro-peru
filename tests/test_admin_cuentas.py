"""Pruebas del panel multi-cuenta del dueno (shared/admin_cuentas.py).

QUE PROTEGEN

  Dos cosas que fallan sin ruido. La primera, el estado que se le ensena al
  dueno: una prueba con la fecha pasada que sigue diciendo "prueba" lleva a
  regalar dias sin saberlo. La segunda, el borrado: una cuenta borrada a
  medias no da error, deja filas huerfanas que un dia rompen un JOIN o, peor,
  conservan datos que el titular pidio suprimir.

  Las de logica pura corren siempre; las que necesitan base se saltan sin
  ella, como el resto de la suite (ver conftest.py).
"""
from datetime import datetime, timedelta

import pytest

from shared.admin_cuentas import (
    correo_valido, filtrar_cuentas, password_temporal, resumir_cuentas,
)
from shared.seguridad import password_debil
from shared.suscripciones import DIAS_GRACIA, estado_efectivo_de
from tests.conftest import sin_base

AHORA = datetime(2026, 9, 3, 12, 0)


# ─── Estado efectivo ─────────────────────────────────────

@pytest.mark.parametrize("estado", ["prueba", "activa"])
def test_con_fecha_futura_el_estado_guardado_se_respeta(estado):
    assert estado_efectivo_de(estado, AHORA + timedelta(days=1), AHORA) == estado


@pytest.mark.parametrize("estado", ["prueba", "activa"])
def test_pasada_la_fecha_es_vencida(estado):
    assert estado_efectivo_de(estado, AHORA - timedelta(hours=1), AHORA) == "vencida"


def test_agotada_la_gracia_es_suspendida():
    vence = AHORA - timedelta(days=DIAS_GRACIA + 1)
    assert estado_efectivo_de("activa", vence, AHORA) == "suspendida"
    assert estado_efectivo_de("vencida", vence, AHORA) == "suspendida"


def test_dentro_de_la_gracia_sigue_vencida():
    assert estado_efectivo_de("vencida", AHORA - timedelta(days=DIAS_GRACIA - 1), AHORA) == "vencida"


@pytest.mark.parametrize("estado", ["cancelada", "suspendida"])
def test_cancelada_y_suspendida_no_cambian_por_la_fecha(estado):
    assert estado_efectivo_de(estado, AHORA + timedelta(days=30), AHORA) == estado


def test_sin_fecha_no_vence_nunca():
    assert estado_efectivo_de("activa", None, AHORA) == "activa"


# ─── Lista: filtros y cifras ─────────────────────────────

def _fila(**k):
    base = {"id": 1, "email": "a@b.pe", "nombre": None, "activo": True,
            "plan_codigo": "pro", "estado": "activa", "vence": AHORA + timedelta(days=10)}
    base.update(k)
    return base


FILAS = [
    _fila(id=1, email="kevin@corp.pe", nombre="Kevin", estado="activa"),
    _fila(id=2, email="maria@gmail.com", nombre="María Quispe", estado="prueba"),
    _fila(id=3, email="vencido@x.pe", estado="activa", vence=AHORA - timedelta(days=1)),
    _fila(id=4, email="apagado@x.pe", activo=False, estado="cancelada"),
    _fila(id=5, email="sinplan@x.pe", plan_codigo=None, estado=None, vence=None),
    _fila(id=6, email="basico@x.pe", plan_codigo="basico", estado="prueba",
          vence=AHORA - timedelta(days=DIAS_GRACIA + 3)),
]


def test_las_cifras_usan_el_estado_efectivo():
    r = resumir_cuentas(FILAS, AHORA)
    assert r["total"] == 6
    assert r["activas"] == 5              # activo = TRUE, la cuenta, no el plan
    assert r["prueba"] == 1               # la 6 ya no es prueba: se agoto
    assert r["pagando"] == 1              # la 3 esta vencida, no cuenta


def test_el_buscador_mira_correo_y_nombre_sin_mayusculas():
    assert [f["id"] for f in filtrar_cuentas(FILAS, q="QUISPE", ahora=AHORA)] == [2]
    assert [f["id"] for f in filtrar_cuentas(FILAS, q="x.pe", ahora=AHORA)] == [3, 4, 5, 6]


def test_el_filtro_de_estado_es_el_efectivo():
    assert [f["id"] for f in filtrar_cuentas(FILAS, estado="vencida", ahora=AHORA)] == [3]
    assert [f["id"] for f in filtrar_cuentas(FILAS, estado="suspendida", ahora=AHORA)] == [6]
    assert [f["id"] for f in filtrar_cuentas(FILAS, estado="sin_suscripcion", ahora=AHORA)] == [5]
    assert [f["id"] for f in filtrar_cuentas(FILAS, estado="desactivada", ahora=AHORA)] == [4]


def test_el_filtro_de_plan_se_combina_con_el_de_estado():
    assert [f["id"] for f in filtrar_cuentas(FILAS, plan="pro", estado="prueba", ahora=AHORA)] == [2]
    assert filtrar_cuentas(FILAS, plan="basico", estado="prueba", ahora=AHORA) == []


def test_cada_fila_sale_con_su_estado_efectivo():
    por_id = {f["id"]: f["estado_efectivo"] for f in filtrar_cuentas(FILAS, ahora=AHORA)}
    assert por_id == {1: "activa", 2: "prueba", 3: "vencida", 4: "cancelada",
                      5: "sin_suscripcion", 6: "suspendida"}


# ─── Utilidades ──────────────────────────────────────────

@pytest.mark.parametrize("correo", ["a@b.pe", "  gerencia@empresa.com.pe ", "x.y+z@dominio.org"])
def test_correos_validos(correo):
    assert correo_valido(correo)


@pytest.mark.parametrize("correo", ["", "sinarroba.pe", "a@b", "a @b.pe", "@b.pe", None])
def test_correos_invalidos(correo):
    assert not correo_valido(correo)


def test_la_contrasena_temporal_pasa_la_misma_regla_que_el_registro():
    for _ in range(50):
        p = password_temporal()
        assert password_debil(p) is None, p
        assert not set(p) & set("0O1lI"), "caracteres que se confunden al dictarla"


def test_dos_contrasenas_temporales_no_coinciden():
    assert password_temporal() != password_temporal()


# ─── Con base ────────────────────────────────────────────

@sin_base
async def test_activar_manual_pisa_la_prueba_y_deja_el_pago(usuario):
    """Lo que hace el formulario 'poner a mano': plan, estado y fecha nuevos, y
    con monto un pago 'manual' pagado en el historial."""
    from shared.db import connection
    from shared.suscripciones import activar_manual, estado_suscripcion

    vence = datetime.now().replace(microsecond=0) + timedelta(days=45)
    ok = await activar_manual(usuario["id"], "basico", "activa", "mensual", vence,
                              "Yape recibo 0042", 49.0, por="dueno@prueba.pe")
    assert ok is True

    s = await estado_suscripcion(usuario["id"])
    assert s["plan_codigo"] == "basico"
    assert s["estado_efectivo"] == "activa"
    assert s["vence"] == vence

    async with connection() as c:
        pago = await c.fetchrow(
            """SELECT p.monto, p.estado, p.metodo, p.respuesta FROM pagos_suscripcion p
                 JOIN suscripciones s ON s.id = p.suscripcion_id
                WHERE s.usuario_id = $1""", usuario["id"])
    assert pago["estado"] == "pagado" and pago["metodo"] == "manual"
    assert float(pago["monto"]) == 49.0
    assert "0042" in str(pago["respuesta"])


@sin_base
async def test_activar_manual_rechaza_lo_que_no_existe(usuario):
    from shared.suscripciones import activar_manual
    assert await activar_manual(usuario["id"], "plan-inventado", "activa", "mensual", None, None) is False
    assert await activar_manual(usuario["id"], "pro", "estado-inventado", "mensual", None, None) is False
    assert await activar_manual(usuario["id"], "pro", "activa", "semanal", None, None) is False


@sin_base
async def test_activar_manual_crea_la_suscripcion_si_no_habia(usuario):
    from shared.db import connection
    from shared.suscripciones import activar_manual, estado_suscripcion

    async with connection() as c:
        await c.execute("DELETE FROM suscripciones WHERE usuario_id = $1", usuario["id"])
    assert (await estado_suscripcion(usuario["id"]))["existe"] is False
    assert await activar_manual(usuario["id"], "pro", "prueba", "anual", None, None) is True
    s = await estado_suscripcion(usuario["id"])
    assert s["existe"] and s["plan_codigo"] == "pro" and s["periodo"] == "anual"


@sin_base
async def test_el_borrado_en_cascada_no_deja_nada(marca):
    """Se llena la cuenta con todo lo que puede colgar de ella -- empresa,
    propuesta, contrato, plazo, experiencia, vencimiento, seguimiento, pago,
    analisis, token, notificacion -- y despues del borrado no queda ninguna
    fila en ninguna tabla. La reclamacion se conserva, desvinculada."""
    from shared.admin_cuentas import borrar_cuenta_completa
    from shared.db import connection, crear_usuario
    from shared.seguridad import hashear_password

    fila = await crear_usuario(f"borrar-{marca}@ejemplo.pe", hashear_password("ClaveDePrueba123!"))
    uid = fila["id"]
    async with connection() as c:
        eid = await c.fetchval(
            "INSERT INTO empresas (razon_social, ruc, usuario_id) VALUES ($1, $2, $3) RETURNING id",
            f"Borrable {marca}", "20" + marca[:9], uid)
        lid = f"LIC-{marca}"
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto, estado)
               VALUES ($1, 'prueba', 'Entidad', 'Objeto', 'convocado')
               ON CONFLICT (id) DO NOTHING""", lid)
        pid = await c.fetchval(
            "INSERT INTO propuestas (licitacion_id, empresa_id) VALUES ($1, $2) RETURNING id", lid, eid)
        cid = await c.fetchval(
            "INSERT INTO contratos (propuesta_id, licitacion_id, empresa_id) VALUES ($1, $2, $3) RETURNING id",
            pid, lid, eid)
        await c.execute(
            "INSERT INTO plazos (contrato_id, tipo, descripcion, fecha_limite) VALUES ($1, 'entrega', 'Entrega', CURRENT_DATE)", cid)
        await c.execute(
            "INSERT INTO experiencia (empresa_id, entidad_contratante, objeto_contrato) VALUES ($1, 'Entidad', 'Obra')", eid)
        await c.execute(
            "INSERT INTO vencimientos (empresa_id, tipo, fecha_vencimiento) VALUES ($1, 'RNP', CURRENT_DATE)", eid)
        await c.execute(
            "INSERT INTO licitaciones_seguidas (usuario_id, licitacion_id) VALUES ($1, $2)", uid, lid)
        sid = await c.fetchval("SELECT id FROM suscripciones WHERE usuario_id = $1", uid)
        await c.execute(
            "INSERT INTO pagos_suscripcion (suscripcion_id, monto, estado, metodo) VALUES ($1, 10, 'pagado', 'manual')", sid)
        await c.execute(
            """INSERT INTO analisis_ia (usuario_id, empresa_id, licitacion_id, resultado)
               VALUES ($1, $2, $3, '{}'::jsonb)""", uid, eid, lid)
        await c.execute(
            "INSERT INTO tokens_recuperacion (usuario_id, token_hash, expira) VALUES ($1, $2, NOW())",
            uid, "hash-" + marca)
        await c.execute(
            "INSERT INTO notificaciones_enviadas (usuario_id, licitacion_id, canal) VALUES ($1, $2, 'email')",
            uid, lid)
        rid = await c.fetchval(
            """INSERT INTO reclamaciones (usuario_id, nombre, documento_tipo, documento_numero, email,
                                          tipo, detalle, pedido, limite_respuesta)
               VALUES ($1, 'Titular', 'DNI', '12345678', $2, 'queja', 'detalle', 'pedido',
                       NOW() + INTERVAL '15 days') RETURNING id""",
            uid, f"borrar-{marca}@ejemplo.pe")

    resumen = await borrar_cuenta_completa(uid)
    assert resumen["borrada"] is True
    # El resumen cuenta lo que cada DELETE borro directamente: lo que cayo por
    # cascada antes de que le tocara su vuelta aparece como 0, y no importa.
    # Lo que importa es lo de abajo: que no quede NADA.
    assert resumen.get("empresas") == 1

    async with connection() as c:
        for tabla, columna, valor in [
            ("usuarios", "id", uid), ("empresas", "usuario_id", uid),
            ("suscripciones", "usuario_id", uid), ("user_config", "usuario_id", uid),
            ("licitaciones_seguidas", "usuario_id", uid), ("analisis_ia", "usuario_id", uid),
            ("tokens_recuperacion", "usuario_id", uid), ("notificaciones_enviadas", "usuario_id", uid),
            ("propuestas", "empresa_id", eid), ("contratos", "empresa_id", eid),
            ("experiencia", "empresa_id", eid), ("vencimientos", "empresa_id", eid),
            ("plazos", "contrato_id", cid), ("pagos_suscripcion", "suscripcion_id", sid),
        ]:
            n = await c.fetchval(f'SELECT count(*) FROM "{tabla}" WHERE "{columna}" = $1', valor)
            assert n == 0, f"quedan {n} filas en {tabla}"
        # La hoja del Libro de Reclamaciones sigue, sin dueno.
        queda = await c.fetchrow("SELECT usuario_id FROM reclamaciones WHERE id = $1", rid)
        assert queda is not None and queda["usuario_id"] is None
        await c.execute("DELETE FROM reclamaciones WHERE id = $1", rid)
        await c.execute("DELETE FROM licitaciones WHERE id = $1", lid)


@sin_base
async def test_crear_y_editar_cuenta_respetan_el_correo_unico(usuario, marca):
    from shared.admin_cuentas import borrar_cuenta_completa, crear_cuenta, cuenta, editar_cuenta
    from shared.seguridad import hashear_password

    fila, error = await crear_cuenta(usuario["email"], "Repetida", hashear_password("ClaveDePrueba123!"),
                                     "pro", "activa", "mensual", None, por="dueno")
    assert fila is None and "Ya existe" in error

    fila, error = await crear_cuenta(f"alta-{marca}@ejemplo.pe", "Alta", hashear_password("ClaveDePrueba123!"),
                                     "basico", "activa", "anual", None, por="dueno")
    try:
        assert fila and error is None
        c = await cuenta(fila["id"])
        assert "password_hash" not in c.keys()
        assert await editar_cuenta(fila["id"], "Alta", usuario["email"], True) == "Ya existe otra cuenta con ese correo."
        assert await editar_cuenta(fila["id"], "Alta", "no-es-correo", True) == "Ese correo no parece válido."
        assert await editar_cuenta(fila["id"], "Nuevo nombre", f"ALTA-{marca}@ejemplo.pe", False) is None
        c = await cuenta(fila["id"])
        assert c["nombre"] == "Nuevo nombre" and c["email"] == f"alta-{marca}@ejemplo.pe"
        assert c["activo"] is False
    finally:
        await borrar_cuenta_completa(fila["id"])


@sin_base
async def test_una_cuenta_desactivada_no_entra_y_se_le_dice(usuario, cliente):
    """Con la contrasena correcta se le dice que esta desactivada; con una
    incorrecta, el mensaje generico de siempre (no se revela nada)."""
    from shared.admin_cuentas import poner_activo
    from web.auth import ERROR_CREDENCIALES, ERROR_DESACTIVADA

    assert await poner_activo(usuario["id"], False)
    r = await cliente.post("/entrar", data={"email": usuario["email"], "password": usuario["password"]})
    assert r.status_code == 403 and ERROR_DESACTIVADA in r.text
    assert "licitapro_sesion" not in r.cookies

    r = await cliente.post("/entrar", data={"email": usuario["email"], "password": "otra-clave-mal"})
    assert r.status_code == 401 and ERROR_CREDENCIALES in r.text

    assert await poner_activo(usuario["id"], True)
    r = await cliente.post("/entrar", data={"email": usuario["email"], "password": usuario["password"]})
    assert r.status_code == 303
