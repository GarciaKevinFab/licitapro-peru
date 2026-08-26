"""Pruebas de integracion: aislamiento, acceso, suscripcion y avisos.

QUE SE CUBRE Y POR QUE ESTO Y NO OTRA COSA

  Son los cuatro sitios donde un fallo no se ve hasta que ya hizo dano:

  - Aislamiento entre inquilinos. Un JOIN olvidado y un cliente ve las
    propuestas de otro. No hay error, no hay excepcion: simplemente devuelve
    filas de mas. Es el fallo mas caro que puede tener este producto.
  - Freno al acceso. Se comprobo que sin el se admitian intentos ilimitados.
  - Corte de la prueba gratuita. Si deja de cortar, el producto es gratis y
    nadie lo nota hasta mirar la facturacion.
  - Reparto de avisos. Ya fallo una vez mandandolo todo al administrador; y un
    duplicado en WhatsApp cuesta dinero de verdad.
"""
import pytest

from tests.conftest import sin_base

pytestmark = [pytest.mark.asyncio, sin_base]


# ─── Aislamiento entre inquilinos ────────────────────────

async def test_una_empresa_no_es_de_otro_usuario(usuario, empresa, marca):
    """empresa_es_de es la guarda que usan todas las rutas con id de empresa."""
    from shared.db import borrar_cuenta, connection, empresa_es_de
    from shared.seguridad import hashear_password

    async with connection() as c:
        intruso = await c.fetchval(
            """INSERT INTO usuarios (email, password_hash, nombre, activo)
               VALUES ($1, $2, 'Intruso', TRUE) RETURNING id""",
            f"intruso-{marca}@ejemplo.pe", hashear_password("Otra12345!"))
    try:
        assert await empresa_es_de(empresa, usuario["id"]) is True
        assert await empresa_es_de(empresa, intruso) is False
    finally:
        await borrar_cuenta(intruso)


async def test_la_ruta_rechaza_editar_una_empresa_ajena(usuario, empresa, marca, cliente):
    """El id llega del formulario, o sea de fuera: no se puede confiar en el."""
    from shared.db import borrar_cuenta, connection
    from shared.seguridad import hashear_password

    email_intruso = f"intruso-{marca}@ejemplo.pe"
    async with connection() as c:
        intruso = await c.fetchval(
            """INSERT INTO usuarios (email, password_hash, nombre, activo)
               VALUES ($1, $2, 'Intruso', TRUE) RETURNING id""",
            email_intruso, hashear_password("Otra12345!"))
    try:
        await cliente.post("/entrar", data={"email": email_intruso,
                                            "password": "Otra12345!"})
        r = await cliente.post("/empresas/guardar", data={
            "empresa_id": str(empresa), "razon_social": "SECUESTRADA SAC"})
        assert r.status_code == 303
        assert "no+es+tuya" in r.headers.get("location", "")

        async with connection() as c:
            nombre = await c.fetchval(
                "SELECT razon_social FROM empresas WHERE id=$1", empresa)
        assert nombre != "SECUESTRADA SAC"
    finally:
        await borrar_cuenta(intruso)


async def test_el_pozo_de_licitaciones_es_compartido(usuario):
    """Las licitaciones son datos publicos: se scrapean una vez y valen para
    todos. Lo privado es a quien le interesa cada una, y eso se filtra al leer.
    """
    from shared.db import licitaciones_para_usuario
    # Sin configuracion propia ve el pozo entero, que es el comportamiento
    # buscado: un panel vacio recien creada la cuenta se lee como "esto no
    # funciona".
    assert isinstance(await licitaciones_para_usuario(usuario["id"]), list)


# ─── Freno a los intentos de acceso ──────────────────────

async def test_el_acceso_se_frena_tras_varios_fallos(usuario, cliente):
    """Medido antes de existir el freno: 20 intentos, 20 respuestas 401 y la
    cuenta intacta."""
    from shared.db import connection
    from web.auth import MAX_INTENTOS_ACCESO

    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")

    for _ in range(MAX_INTENTOS_ACCESO):
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": "incorrecta"})

    # Ni siquiera con la contrasena buena: si pasara, el limite no serviria.
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 401

    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")


async def test_el_freno_no_revela_que_la_cuenta_existe(usuario, cliente, marca):
    """Decir "bloqueada" confirmaria el correo, que es media victoria para quien
    esta probando."""
    from shared.db import connection
    from web.auth import MAX_INTENTOS_ACCESO

    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    for _ in range(MAX_INTENTOS_ACCESO):
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": "incorrecta"})

    frenado = await cliente.post("/entrar", data={"email": usuario["email"],
                                                  "password": "x"})
    inexistente = await cliente.post(
        "/entrar", data={"email": f"nadie-{marca}@ejemplo.pe", "password": "x"})
    assert frenado.status_code == inexistente.status_code
    assert "bloquead" not in frenado.text.lower()

    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")


async def test_entrar_bien_limpia_los_fallos_previos(usuario, cliente):
    """Quien entra bien demuestra ser el dueno: un dedo torcido de ayer no debe
    acercarle al bloqueo manana."""
    from shared.db import connection

    async with connection() as c:
        await c.execute("DELETE FROM intentos_acceso")
    for _ in range(3):
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": "incorrecta"})
    r = await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
    assert r.status_code == 303

    async with connection() as c:
        quedan = await c.fetchval(
            "SELECT COUNT(*) FROM intentos_acceso WHERE identificador=$1",
            usuario["email"])
    assert quedan == 0


# ─── Suscripcion ─────────────────────────────────────────

async def test_la_cuenta_nace_en_prueba_con_avisos(usuario):
    from shared.suscripciones import estado_suscripcion
    s = await estado_suscripcion(usuario["id"])
    assert s["estado_efectivo"] == "prueba"
    assert s["acceso"] is True
    assert s["alertas"] is True


async def test_agotada_la_gracia_cae_al_plan_gratuito(usuario):
    """No se expulsa: se degrada. Bloquear del todo empuja al cliente a la web
    del competidor, que si le deja mirar."""
    from shared.db import connection
    from shared.suscripciones import estado_suscripcion, puede_usar_ia

    async with connection() as c:
        await c.execute(
            "UPDATE suscripciones SET vence = NOW() - INTERVAL '30 days' "
            "WHERE usuario_id = $1", usuario["id"])

    s = await estado_suscripcion(usuario["id"])
    assert s["degradado"] is True
    assert s["plan_codigo"] == "gratis"
    assert s["acceso"] is True         # conserva el panel y sus datos
    assert not s["alertas"]            # pierde lo que cuesta dinero
    assert await puede_usar_ia(usuario["id"]) is False


async def test_el_degradado_no_recibe_avisos(usuario):
    """La linea de pago esta exactamente aqui: mirar es gratis, que te avisen
    se paga."""
    from shared.db import connection
    from shared.notificaciones import destinatarios

    async with connection() as c:
        await c.execute(
            "UPDATE suscripciones SET vence = NOW() - INTERVAL '30 days' "
            "WHERE usuario_id = $1", usuario["id"])

    ids = [d["id"] for d in await destinatarios()]
    assert usuario["id"] not in ids


# ─── Reparto de avisos ───────────────────────────────────

async def test_no_se_avisa_dos_veces_de_lo_mismo(usuario, marca):
    """La restriccion UNIQUE es la idempotencia. Sin ella un reintento vuelve a
    enviar, y en WhatsApp cada envio se cobra."""
    from shared.db import connection
    from shared.notificaciones import CANAL_WHATSAPP, anotar_envio, pendientes_para

    lic_id = f"PRUEBA-{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto, fecha_cierre)
               VALUES ($1, 'prueba', 'ENTIDAD DE PRUEBA', 'Objeto de prueba',
                       NOW() + INTERVAL '10 days')
               ON CONFLICT (id) DO NOTHING""", lic_id)
    try:
        await anotar_envio(usuario["id"], [lic_id], CANAL_WHATSAPP)
        await anotar_envio(usuario["id"], [lic_id], CANAL_WHATSAPP)  # repetido

        async with connection() as c:
            veces = await c.fetchval(
                """SELECT COUNT(*) FROM notificaciones_enviadas
                    WHERE usuario_id=$1 AND licitacion_id=$2 AND canal=$3""",
                usuario["id"], lic_id, CANAL_WHATSAPP)
        assert veces == 1

        pendientes = await pendientes_para(usuario["id"], CANAL_WHATSAPP)
        assert lic_id not in [p["id"] for p in pendientes]
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic_id)


async def test_lo_avisado_a_uno_sigue_disponible_para_otro(usuario, marca):
    """`licitaciones.notificado` era un booleano global: el primer avisado
    quemaba la licitacion para todos los demas inquilinos, para siempre."""
    from shared.db import borrar_cuenta, connection
    from shared.notificaciones import CANAL_TELEGRAM, anotar_envio, pendientes_para
    from shared.seguridad import hashear_password
    from shared.suscripciones import crear_suscripcion_prueba

    lic_id = f"PRUEBA-{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto, fecha_cierre)
               VALUES ($1, 'prueba', 'ENTIDAD DE PRUEBA', 'Objeto de prueba',
                       NOW() + INTERVAL '10 days')
               ON CONFLICT (id) DO NOTHING""", lic_id)
        otro = await c.fetchval(
            """INSERT INTO usuarios (email, password_hash, nombre, activo)
               VALUES ($1, $2, 'Otro', TRUE) RETURNING id""",
            f"otro-{marca}@ejemplo.pe", hashear_password("Otra12345!"))
    await crear_suscripcion_prueba(otro)
    try:
        await anotar_envio(usuario["id"], [lic_id], CANAL_TELEGRAM)
        pendientes_otro = await pendientes_para(otro, CANAL_TELEGRAM, limite=2000)
        assert lic_id in [p["id"] for p in pendientes_otro]
    finally:
        await borrar_cuenta(otro)
        async with connection() as c:
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic_id)


# ─── Borrado de cuenta (Ley 29733) ───────────────────────

async def test_borrar_la_cuenta_no_deja_rastro(marca):
    """Derecho de supresion. Fallaba en cuanto el cliente tuviera una propuesta,
    porque las claves foraneas estaban en NO ACTION."""
    from shared.db import borrar_cuenta, connection
    from shared.seguridad import hashear_password

    async with connection() as c:
        uid = await c.fetchval(
            """INSERT INTO usuarios (email, password_hash, nombre, activo)
               VALUES ($1, $2, 'Se borra', TRUE) RETURNING id""",
            f"borrar-{marca}@ejemplo.pe", hashear_password("Otra12345!"))
        eid = await c.fetchval(
            """INSERT INTO empresas (razon_social, ruc, usuario_id, activa)
               VALUES ($1, $2, $3, TRUE) RETURNING id""",
            "Se borra SAC", "20" + marca[:9], uid)
        lic = await c.fetchval("SELECT id FROM licitaciones LIMIT 1")
        if lic:
            await c.execute(
                """INSERT INTO propuestas (licitacion_id, empresa_id, estado)
                   VALUES ($1, $2, 'borrador')""", lic, eid)

    resumen = await borrar_cuenta(uid)
    assert resumen["borrada"] is True

    async with connection() as c:
        assert await c.fetchval(
            "SELECT COUNT(*) FROM usuarios WHERE id=$1", uid) == 0
        assert await c.fetchval(
            "SELECT COUNT(*) FROM propuestas WHERE empresa_id=$1", eid) == 0
        # La licitacion es dato publico y de todos: no se va con el cliente.
        if lic:
            assert await c.fetchval(
                "SELECT COUNT(*) FROM licitaciones WHERE id=$1", lic) == 1
