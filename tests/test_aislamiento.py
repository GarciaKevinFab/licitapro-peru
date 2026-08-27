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


# ─── Banderas sobre licitaciones abiertas ────────────────

async def test_la_bandera_de_entidad_cae_en_licitaciones_ABIERTAS(marca):
    """Las otras banderas llegaban tarde y esa era la mitad del problema.

    postor_unico y pocos_postores solo se saben cuando el proceso ya se
    adjudico, o sea cuando ya no puedes presentarte. Medido sobre la base real:
    cero licitaciones vigentes tenian bandera. Esta se apoya en el historial de
    quien convoca, que si se sabe antes de postular.
    """
    from shared.banderas import (
        CUOTA_POSTOR_UNICO, MIN_RESUELTOS_ENTIDAD,
        marcar_entidades_con_mal_historial,
    )
    from shared.db import connection

    ruc = "20" + marca[:9]
    abierta = f"PRUEBA-ABIERTA-{marca}"
    async with connection() as c:
        # Historial: todos resueltos con un solo postor, por encima de la cuota.
        for i in range(MIN_RESUELTOS_ENTIDAD):
            await c.execute(
                """INSERT INTO licitaciones
                       (id, fuente, entidad, entidad_ruc, objeto,
                        fecha_cierre, numero_postores)
                   VALUES ($1, 'prueba', 'ENTIDAD CON HISTORIAL', $2,
                           'Proceso ya resuelto', NOW() - INTERVAL '30 days', 1)
                   ON CONFLICT (id) DO NOTHING""",
                f"PRUEBA-HIST-{marca}-{i}", ruc)
        # Y una convocatoria viva de la misma entidad.
        await c.execute(
            """INSERT INTO licitaciones
                   (id, fuente, entidad, entidad_ruc, objeto, fecha_cierre)
               VALUES ($1, 'prueba', 'ENTIDAD CON HISTORIAL', $2,
                       'Convocatoria abierta', NOW() + INTERVAL '10 days')
               ON CONFLICT (id) DO NOTHING""",
            abierta, ruc)
    try:
        await marcar_entidades_con_mal_historial()
        async with connection() as c:
            fila = await c.fetchrow(
                "SELECT banderas, banderas_nivel FROM licitaciones WHERE id=$1",
                abierta)
        assert "entidad_postor_unico_frecuente" in fila["banderas"]
        assert fila["banderas_nivel"] >= 2
        assert CUOTA_POSTOR_UNICO == 0.5   # medido: la media nacional es 0,21
    finally:
        async with connection() as c:
            await c.execute(
                "DELETE FROM licitaciones WHERE entidad_ruc=$1", ruc)


async def test_una_entidad_con_poco_historial_no_se_juzga(marca):
    """Con dos procesos, una casualidad da el 100% y no significa nada."""
    from shared.banderas import marcar_entidades_con_mal_historial
    from shared.db import connection

    ruc = "20" + marca[:9]
    abierta = f"PRUEBA-POCA-{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones
                   (id, fuente, entidad, entidad_ruc, objeto,
                    fecha_cierre, numero_postores)
               VALUES ($1, 'prueba', 'ENTIDAD NUEVA', $2, 'Unico resuelto',
                       NOW() - INTERVAL '30 days', 1)
               ON CONFLICT (id) DO NOTHING""",
            f"PRUEBA-POCA-HIST-{marca}", ruc)
        await c.execute(
            """INSERT INTO licitaciones
                   (id, fuente, entidad, entidad_ruc, objeto, fecha_cierre)
               VALUES ($1, 'prueba', 'ENTIDAD NUEVA', $2, 'Convocatoria abierta',
                       NOW() + INTERVAL '10 days')
               ON CONFLICT (id) DO NOTHING""",
            abierta, ruc)
    try:
        await marcar_entidades_con_mal_historial()
        async with connection() as c:
            banderas = await c.fetchval(
                "SELECT banderas FROM licitaciones WHERE id=$1", abierta)
        assert "entidad_postor_unico_frecuente" not in (banderas or [])
    finally:
        async with connection() as c:
            await c.execute(
                "DELETE FROM licitaciones WHERE entidad_ruc=$1", ruc)


# ─── Cabeceras y politica de seguridad ───────────────────

async def test_la_politica_prohibe_el_script_embebido(cliente):
    """Es la linea que de verdad protege contra XSS.

    Mientras script-src admitia 'unsafe-inline', la CSP estaba puesta y no
    servia: un XSS podia inyectar un <script> y ejecutarlo igual. Se pudo
    cerrar al sacar del HTML los tres manejadores on*= y el script de la
    portada.
    """
    r = await cliente.get("/entrar")
    csp = r.headers["Content-Security-Policy"]
    script_src = next(d for d in csp.split("; ") if d.startswith("script-src"))
    assert "'unsafe-inline'" not in script_src
    # Y lo que protege desde el primer dia, sin depender de lo embebido.
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert "object-src 'none'" in csp


async def test_las_cabeceras_acompanan_a_los_errores(cliente):
    """El middleware va registrado el ultimo para quedar por fuera. Si quedara
    por dentro, un 404 saldria sin ninguna cabecera."""
    r = await cliente.get("/ruta-que-no-existe")
    assert r.status_code == 404
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_no_quedan_manejadores_embebidos_en_las_plantillas():
    """Si alguien vuelve a meter un onclick=, la CSP lo bloquea en silencio y el
    boton deja de funcionar sin ningun error visible. Mejor detenerlo aqui."""
    import pathlib
    import re
    culpables = []
    for f in pathlib.Path("web/templates").glob("*.html"):
        if re.search(r' on[a-z]+="', f.read_text(encoding="utf-8")):
            culpables.append(f.name)
    assert not culpables, f"manejadores embebidos en: {culpables}"


# ─── Primeros pasos de una cuenta nueva ──────────────────

async def test_una_cuenta_nueva_recibe_la_guia(usuario, cliente):
    """El primer minuto decide si vuelve. Sin empresa no puede postular, y sin
    filtros el panel le muestra seis mil licitaciones que no le interesan."""
    from web.app import _primeros_pasos

    pasos = await _primeros_pasos(usuario["id"])
    assert pasos is not None
    assert pasos["hechos"] == 0
    assert [p["titulo"] for p in pasos["pasos"]][0] == "Carga tu empresa"


async def test_la_guia_desaparece_al_completar_los_pasos(usuario, empresa):
    """Se calcula cada vez en vez de guardar "ya vio el tutorial": asi el aviso
    vuelve si algun dia se queda sin empresas, y una marca de vista mentiria."""
    from shared.db import connection
    from web.app import _primeros_pasos

    async with connection() as c:
        await c.execute(
            "UPDATE user_config SET keywords = ARRAY['obra'] WHERE usuario_id=$1",
            usuario["id"])
        await c.execute(
            "UPDATE usuarios SET telegram_chat_id = $2 WHERE id = $1",
            usuario["id"], 12345)

    assert await _primeros_pasos(usuario["id"]) is None

    # Y si se queda sin empresa activa, vuelve a aparecer.
    async with connection() as c:
        await c.execute(
            "UPDATE empresas SET activa = FALSE WHERE id = $1", empresa)
    pasos = await _primeros_pasos(usuario["id"])
    assert pasos is not None and pasos["hechos"] == 2


# ─── API interna de n8n ──────────────────────────────────

async def test_la_api_interna_falla_cerrada_sin_token():
    """No tenia ninguna comprobacion de acceso, y uno de sus endpoints devuelve
    los contratos activos de TODOS los inquilinos.

    Sin la variable configurada se niega entera, en vez de responder: una API
    interna sin token no es "todavia sin proteger", es una filtracion esperando
    a que alguien la arranque siguiendo el comentario de ejecucion.
    """
    import os

    import httpx

    from shared.api_server import app as api

    previo = os.environ.pop("LICITAPRO_API_TOKEN", None)
    try:
        transporte = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transporte,
                                     base_url="http://interna") as c:
            sin_configurar = await c.get("/api/contratos")
            assert sin_configurar.status_code == 503

            os.environ["LICITAPRO_API_TOKEN"] = "token-de-prueba"
            assert (await c.get("/api/contratos")).status_code == 401
            assert (await c.get("/api/contratos",
                                headers={"X-API-Token": "otro"})).status_code == 401
            assert (await c.get("/api/contratos",
                                headers={"X-API-Token": "token-de-prueba"})
                    ).status_code == 200
            # El healthcheck tiene que poder consultarse sin token.
            assert (await c.get("/api/health")).status_code == 200
    finally:
        os.environ.pop("LICITAPRO_API_TOKEN", None)
        if previo:
            os.environ["LICITAPRO_API_TOKEN"] = previo


# ─── Paginas legales ─────────────────────────────────────

async def test_las_paginas_legales_son_publicas(cliente):
    """Hay que poder leerlas ANTES de registrarse. Pedir una cuenta para saber
    que hacemos con tus datos es exactamente lo que la Ley 29733 no quiere."""
    for ruta in ("/privacidad", "/terminos"):
        r = await cliente.get(ruta)
        assert r.status_code == 200, ruta
        # El aviso de borrador tiene que seguir ahi hasta que un abogado lo
        # revise: quitarlo sin revision es publicar como definitivo un texto
        # que nadie valido.
        assert "pendiente de revisión legal" in r.text


async def test_la_privacidad_describe_el_sistema_real(cliente):
    """No es una plantilla generica: nombra los terceros a los que de verdad se
    envian datos y los limites concretos del producto."""
    t = (await cliente.get("/privacidad")).text
    assert "29733" in t
    for tercero in ("Meta", "Telegram", "Izipay", "Anthropic"):
        assert tercero in t, tercero
    assert "No te pedimos el PIN" in t
    assert "No guardamos el número de tu tarjeta" in t


async def test_las_legales_se_leen_con_la_suscripcion_caida(usuario, cliente):
    """El derecho a saber que hacemos con tus datos, y a pedir que los
    borremos, no depende de estar al dia con el pago."""
    from shared.db import connection

    async with connection() as c:
        await c.execute(
            "UPDATE suscripciones SET estado='cancelada', "
            "vence = NOW() - INTERVAL '60 days' WHERE usuario_id = $1",
            usuario["id"])

    await cliente.post("/entrar", data={"email": usuario["email"],
                                        "password": usuario["password"]})
    assert (await cliente.get("/privacidad")).status_code == 200
    assert (await cliente.get("/terminos")).status_code == 200
