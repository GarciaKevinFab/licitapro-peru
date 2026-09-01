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
import re

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

async def test_la_politica_prohibe_lo_embebido(cliente):
    """Ni script ni estilo en linea. Es lo que hace que la CSP sirva de algo.

    Mientras admitia 'unsafe-inline', la politica estaba puesta y no protegia:
    un XSS podia inyectar un <script> y ejecutarlo igual. Se cerro sacando del
    HTML los manejadores on*=, el script de la portada y los 44 atributos
    style=.
    """
    r = await cliente.get("/entrar")
    csp = r.headers["Content-Security-Policy"]
    for directiva in ("script-src", "style-src"):
        valor = next(d for d in csp.split("; ") if d.startswith(directiva))
        assert "'unsafe-inline'" not in valor, valor
    # Y lo que protege sin depender de lo embebido.
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert "object-src 'none'" in csp


async def test_el_nonce_cambia_en_cada_peticion(cliente):
    """Un nonce fijo es 'unsafe-inline' con pasos extra: el atacante lo lee del
    HTML y se lo pone a su propia etiqueta."""
    import re

    def nonce_de(respuesta):
        m = re.search(r"'nonce-([^']+)'",
                      respuesta.headers["Content-Security-Policy"])
        return m.group(1) if m else None

    primera = await cliente.get("/entrar")
    segunda = await cliente.get("/entrar")
    a, b = nonce_de(primera), nonce_de(segunda)
    assert a and b and a != b

    # Y el <style> de la pagina tiene que traer EXACTAMENTE el de su cabecera:
    # si no coincide, el navegador descarta los estilos y la web sale desnuda.
    assert f'<style nonce="{a}">' in primera.text


async def test_no_quedan_atributos_style_en_las_plantillas():
    """Un nonce no cubre los atributos style=: solo vale para elementos <style>.

    Asi que si alguien vuelve a meter uno, el navegador lo ignora en silencio y
    el elemento se descoloca sin ningun error visible.
    """
    import pathlib
    culpables = [f.name for f in pathlib.Path("web/templates").glob("*.html")
                 if 'style="' in f.read_text(encoding="utf-8")]
    assert not culpables, f"estilos embebidos en: {culpables}"


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


# ─── Experiencia y equipo tecnico ────────────────────────

async def test_no_se_puede_agregar_experiencia_a_una_empresa_ajena(
        usuario, empresa, marca, cliente):
    """Rutas de escritura nuevas, asi que la propiedad se comprueba explicitamente.

    La experiencia acreditada es dato competitivo: con quien has contratado y
    por cuanto. Poder escribirla en la empresa de otro seria, ademas de una
    fuga, una forma de estropearle la calificacion.
    """
    from shared.db import borrar_cuenta, crear_usuario, connection
    from shared.seguridad import hashear_password

    email = f"intruso-exp-{marca}@ejemplo.pe"
    intruso = await crear_usuario(email, hashear_password("ClaveDePrueba123!"), "Intruso")
    try:
        await cliente.post("/entrar", data={"email": email,
                                            "password": "ClaveDePrueba123!"})
        r = await cliente.post(f"/empresas/{empresa}/experiencia", data={
            "objeto_contrato": "Contrato inventado",
            "entidad_contratante": "Entidad inventada"})
        assert r.status_code == 303
        assert "no+es+tuya" in r.headers["location"]

        r = await cliente.post(f"/empresas/{empresa}/equipo",
                               data={"nombre_completo": "Profesional inventado"})
        assert r.status_code == 303
        assert "no+es+tuya" in r.headers["location"]

        # Y no se escribio nada.
        async with connection() as c:
            assert await c.fetchval(
                "SELECT COUNT(*) FROM experiencia WHERE empresa_id=$1", empresa) == 0
            assert await c.fetchval(
                "SELECT COUNT(*) FROM equipo_tecnico WHERE empresa_id=$1", empresa) == 0
    finally:
        await borrar_cuenta(intruso["id"])


async def test_borrar_experiencia_exige_que_sea_de_esa_empresa(
        usuario, empresa, marca, cliente):
    """El id de la experiencia y el de la empresa vienen los DOS de la URL.

    Sin el empresa_id en el WHERE bastaria con cambiar un numero para borrar la
    experiencia de cualquier otra empresa del sistema, aun siendo dueno de la
    propia.
    """
    from shared.db import connection

    async with connection() as c:
        otra_empresa = await c.fetchval(
            """INSERT INTO empresas (razon_social, ruc, usuario_id, activa)
               VALUES ($1, $2, $3, TRUE) RETURNING id""",
            f"Ajena {marca} SAC", "21" + marca[:9], usuario["id"])
        exp_ajena = await c.fetchval(
            """INSERT INTO experiencia (empresa_id, entidad_contratante,
                                        objeto_contrato)
               VALUES ($1,'Entidad','Obra de la otra empresa') RETURNING id""",
            otra_empresa)

    await cliente.post("/entrar", data={"email": usuario["email"],
                                        "password": usuario["password"]})
    # Se pide borrar la experiencia de `otra_empresa` a traves de `empresa`.
    r = await cliente.post(f"/empresas/{empresa}/experiencia/{exp_ajena}/borrar")
    assert r.status_code == 303

    async with connection() as c:
        sigue = await c.fetchval("SELECT COUNT(*) FROM experiencia WHERE id=$1",
                                 exp_ajena)
    assert sigue == 1, "el empresa_id del WHERE no esta frenando el cruce de ids"


async def test_las_claves_de_busqueda_salen_normalizadas(usuario, empresa, cliente):
    """`knowledge_base` cruza estas claves contra el objeto de la licitacion.

    Ese cruce compara arrays, asi que solo casa si los dos lados estan
    normalizados igual: minusculas y sin tildes. Guardando el texto tal como lo
    escribe el usuario, el cruce no encontraria nunca nada y la experiencia
    relevante no se propondria jamas.
    """
    from shared.db import connection

    await cliente.post("/entrar", data={"email": usuario["email"],
                                        "password": usuario["password"]})
    await cliente.post(f"/empresas/{empresa}/experiencia", data={
        "objeto_contrato": "AMPLIACIÓN del SISTEMA de Alcantarillado",
        "entidad_contratante": "Municipalidad de Prueba",
        "monto": "S/ 1,240,500.00"})

    async with connection() as c:
        fila = await c.fetchrow(
            "SELECT monto, keywords FROM experiencia WHERE empresa_id=$1", empresa)

    assert "ampliacion" in fila["keywords"], fila["keywords"]
    assert "alcantarillado" in fila["keywords"]
    # Sin tildes ni mayusculas en ninguna.
    assert all(k == k.lower() and k.isalnum() for k in fila["keywords"])
    # Y el monto con separadores y simbolo de moneda se guardo como numero.
    assert fila["monto"] == pytest.approx(1240500.0)


# ─── Vista de administracion ─────────────────────────────

async def test_la_vista_de_gasto_se_cierra_a_los_clientes(usuario, cliente):
    """Ensena los correos de todos los clientes y cuanto consume cada uno.

    Es la pagina con mas datos de terceros de todo el producto. Se comprueban
    los dos cierres: un cliente con sesion valida no la ve, y sin la variable
    configurada no la ve NADIE -- ni el dueno. Lo segundo importa porque un
    despliegue con la variable olvidada, si abriera, publicaria esa lista.
    """
    import os

    previo = os.environ.get("LICITAPRO_ADMIN_EMAIL")
    try:
        # Un cliente cualquiera, con el dueno configurado y siendo otro.
        os.environ["LICITAPRO_ADMIN_EMAIL"] = "dueno-que-no-eres-tu@ejemplo.pe"
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
        assert (await cliente.get("/admin/ia")).status_code == 404

        # Y ahora ese mismo usuario SI es el dueno: tiene que verla. En
        # mayusculas a proposito, para fijar que el correo no distingue caja.
        os.environ["LICITAPRO_ADMIN_EMAIL"] = usuario["email"].upper()
        assert (await cliente.get("/admin/ia")).status_code == 200

        # Sin variable, cerrado incluso para el.
        os.environ.pop("LICITAPRO_ADMIN_EMAIL", None)
        assert (await cliente.get("/admin/ia")).status_code == 404
    finally:
        os.environ.pop("LICITAPRO_ADMIN_EMAIL", None)
        if previo is not None:
            os.environ["LICITAPRO_ADMIN_EMAIL"] = previo


async def test_no_se_descarga_la_proforma_de_un_contrato_ajeno(
        usuario, empresa, marca, cliente):
    """La proforma lleva dentro entidad, montos y datos bancarios."""
    from shared.db import borrar_cuenta, connection, crear_usuario
    from shared.seguridad import hashear_password

    async with connection() as c:
        contrato = await c.fetchval(
            """INSERT INTO contratos (empresa_id, numero_contrato,
                                     monto_adjudicado, estado)
               VALUES ($1,$2,$3,'vigente') RETURNING id""",
            empresa, f"C-{marca}", 50000)
        pago = await c.fetchval(
            """INSERT INTO pagos (contrato_id, concepto, monto, estado)
               VALUES ($1,'Entregable',$2,'facturado') RETURNING id""",
            contrato, 50000)

    email = f"intruso-doc-{marca}@ejemplo.pe"
    intruso = await crear_usuario(email, hashear_password("ClaveDePrueba123!"), "Intruso")
    try:
        await cliente.post("/entrar", data={"email": email,
                                            "password": "ClaveDePrueba123!"})
        r = await cliente.get(f"/contratos/{contrato}/pagos/{pago}/proforma")
        assert r.status_code == 303
        assert "no+es+tuyo" in r.headers["location"]

        r = await cliente.get(f"/contratos/{contrato}/conformidad")
        assert r.status_code == 303
        assert "no+es+tuyo" in r.headers["location"]
    finally:
        await borrar_cuenta(intruso["id"])
        async with connection() as c:
            await c.execute("DELETE FROM pagos WHERE contrato_id=$1", contrato)
            await c.execute("DELETE FROM contratos WHERE id=$1", contrato)


# ─── Seguimiento, vencimientos y exportacion ─────────────

async def test_seguir_no_deja_redirigir_fuera_del_sitio(usuario, marca, cliente):
    """`volver` llega de un formulario, o sea de fuera.

    Sin comprobarlo seria una redireccion abierta: basta con montar un enlace
    con `volver=//otro-sitio` para sacar al usuario del panel justo despues de
    una accion que el mismo pidio, que es cuando menos se sospecha.
    """
    from shared.db import connection

    lid = f"prueba-seg-{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto)
               VALUES ($1,'prueba','Entidad','Objeto de prueba')
               ON CONFLICT (id) DO NOTHING""", lid)
    try:
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
        for destino in ("//evil.example", "https://evil.example", "javascript:1"):
            r = await cliente.post(f"/licitacion/{lid}/seguir",
                                   data={"volver": destino})
            assert r.headers["location"] == f"/licitacion/{lid}", destino

        # Y una ruta propia si se respeta.
        r = await cliente.post(f"/licitacion/{lid}/seguir", data={"volver": "/panel"})
        assert r.headers["location"] == "/panel"
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM licitaciones_seguidas WHERE licitacion_id=$1", lid)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lid)


async def test_seguir_alterna_y_no_duplica(usuario, marca, cliente):
    """Seguir dos veces no es un estado distinto de seguir una."""
    from shared.db import connection

    lid = f"prueba-seg2-{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto)
               VALUES ($1,'prueba','Entidad','Objeto de prueba')
               ON CONFLICT (id) DO NOTHING""", lid)
    try:
        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})

        async def cuantas():
            async with connection() as c:
                return await c.fetchval(
                    """SELECT COUNT(*) FROM licitaciones_seguidas
                        WHERE usuario_id=$1 AND licitacion_id=$2""",
                    usuario["id"], lid)

        await cliente.post(f"/licitacion/{lid}/seguir")
        assert await cuantas() == 1
        await cliente.post(f"/licitacion/{lid}/seguir")   # alterna: deja de seguir
        assert await cuantas() == 0
    finally:
        async with connection() as c:
            await c.execute("DELETE FROM licitaciones_seguidas WHERE licitacion_id=$1", lid)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lid)


async def test_no_se_anotan_vencimientos_en_una_empresa_ajena(
        usuario, empresa, marca, cliente):
    from shared.db import borrar_cuenta, connection, crear_usuario
    from shared.seguridad import hashear_password

    email = f"intruso-venc-{marca}@ejemplo.pe"
    intruso = await crear_usuario(email, hashear_password("ClaveDePrueba123!"), "Intruso")
    try:
        await cliente.post("/entrar", data={"email": email,
                                            "password": "ClaveDePrueba123!"})
        r = await cliente.post(f"/empresas/{empresa}/vencimiento", data={
            "tipo": "Poliza inventada", "fecha_vencimiento": "2027-01-01"})
        assert r.status_code == 303
        assert "no+es+tuya" in r.headers["location"]

        async with connection() as c:
            assert await c.fetchval(
                "SELECT COUNT(*) FROM vencimientos WHERE empresa_id=$1", empresa) == 0
    finally:
        await borrar_cuenta(intruso["id"])


async def test_el_csv_lo_abre_excel_en_espanol(usuario, cliente):
    """Dos detalles que parecen manias y deciden si la exportacion sirve.

    Sin BOM, Excel lee el archivo como ANSI y toda tilde sale rota en cada
    fila. Con comas en vez de punto y coma, la configuracion regional de Peru
    mete la fila entera en la primera columna. Cualquiera de las dos convierte
    la funcion en algo que el usuario prueba una vez y no vuelve a usar.
    """
    await cliente.post("/entrar", data={"email": usuario["email"],
                                        "password": usuario["password"]})
    r = await cliente.get("/informes/cobros.csv")
    assert r.status_code == 200
    assert r.content.startswith(b"\xef\xbb\xbf"), "falta el BOM"
    assert b";" in r.content.split(b"\n")[0], "la cabecera no usa punto y coma"
    assert "attachment" in r.headers.get("content-disposition", "")


async def test_el_html_no_se_cachea_nunca(cliente):
    """Un intermediario que guarde /panel se lo sirve al siguiente visitante.

    Hoy Cloudflare no cachea HTML por defecto, asi que en la practica no
    ocurre. Pero eso es configuracion que vive FUERA de este repositorio: una
    regla de "Cache Everything" puesta con buena intencion convierte el panel
    en una fuga entre clientes. La aplicacion tiene que defenderse sola.

    Hay un segundo motivo, independiente: todas las plantillas llevan un nonce
    de CSP distinto por peticion. Un cuerpo guardado con el nonce de ayer,
    servido con la cabecera de hoy, no casa, y la pagina sale sin estilos.

    `/static` queda fuera a proposito: archivos sin nonce y sin datos de nadie.
    """
    for ruta in ("/", "/entrar", "/registro", "/privacidad", "/panel"):
        r = await cliente.get(ruta)
        assert r.headers.get("cache-control") == "private, no-store", ruta

    estatico = await cliente.get("/static/licitapro.js")
    assert "cache-control" not in estatico.headers


# ─── Paginas legales ─────────────────────────────────────

async def test_las_paginas_legales_son_publicas(cliente):
    """Hay que poder leerlas ANTES de registrarse. Pedir una cuenta para saber
    que hacemos con tus datos es exactamente lo que la Ley 29733 no quiere."""
    for ruta in ("/privacidad", "/terminos"):
        assert (await cliente.get(ruta)).status_code == 200, ruta


async def test_los_terminos_no_se_publican_a_medias(cliente):
    """Los terminos ya no llevan aviso de borrador, y eso sube el liston.

    ANTES ESTA PRUEBA EXIGIA EL AVISO

      Mientras el texto estuviera sin revisar, el aviso era lo honesto: decia
      al lector que no se fiara del todo. El dueno decidio retirarlo, y con el
      aviso fuera lo que hay que impedir es lo contrario -- que se publique
      como definitivo un texto con huecos dentro.

      El hueco tenia una forma concreta y reconocible: corchetes con la palabra
      "pendiente" ("[Jurisdiccion: pendiente de definir.]"). Eso es lo que se
      fija aqui, junto a las dos clausulas que un contrato de servicio no puede
      no tener.
    """
    import re
    t = (await cliente.get("/terminos")).text

    huecos = re.findall(r"\[[^\]]*pendiente[^\]]*\]", t, flags=re.I)
    assert not huecos, f"quedan marcadores sin resolver: {huecos}"
    assert "pendiente de revisión legal" not in t

    # Las dos que faltaban, y que sin ellas el contrato no cierra.
    assert "doce meses anteriores" in t, "falta el limite de responsabilidad"
    assert "Cercado de Lima" in t, "falta la jurisdiccion"
    # Ninguna de las dos puede recortar lo que la ley no deja recortar.
    assert "29571" in t and "Indecopi" in t


# Los cuatro que la Ley 29733 exige para identificar y poder contactar al
# responsable. El telefono no entra: es cortesia, no requisito.
_IDENTIDAD = {
    "LICITAPRO_RAZON_SOCIAL": "EJEMPLO DE PRUEBA S.A.C.",
    "LICITAPRO_RUC": "20123456789",
    "LICITAPRO_DIRECCION": "Av. de Prueba 123, Lima",
    "LICITAPRO_CONTACTO_EMAIL": "datos@ejemplo.pe",
}


async def test_la_privacidad_avisa_mientras_falten_datos_del_responsable(cliente, monkeypatch):
    """Sin los datos del responsable, la politica se publica diciendo que lo esta.

    No es escrupulo: sin domicilio ni correo, el derecho de acceso de la Ley
    29733 no se puede ejercer contra nadie. Publicarla como definitiva seria
    afirmar que se cumple algo que no se puede cumplir.
    """
    for v in _IDENTIDAD:
        monkeypatch.delenv(v, raising=False)
    t = (await cliente.get("/privacidad")).text
    assert "Política incompleta" in t
    # Y nunca un corchete de marcador delante del lector.
    assert not re.findall(r"\[[^\]]*pendiente[^\]]*\]", t, flags=re.I)


async def test_el_aviso_de_privacidad_se_apaga_solo_al_completar_la_identidad(cliente, monkeypatch):
    """La razon de ser del cambio: el aviso depende del HECHO, no de la memoria.

    Antes era texto fijo, asi que sobreviviria a que los datos se rellenaran y
    nadie sabria si seguia puesto por descuido o a proposito. Con los cuatro en
    el entorno tiene que desaparecer, y los datos tienen que verse.
    """
    for v, valor in _IDENTIDAD.items():
        monkeypatch.setenv(v, valor)
    t = (await cliente.get("/privacidad")).text
    assert "Política incompleta" not in t
    assert _IDENTIDAD["LICITAPRO_RAZON_SOCIAL"] in t
    assert _IDENTIDAD["LICITAPRO_RUC"] in t
    assert _IDENTIDAD["LICITAPRO_DIRECCION"] in t
    assert _IDENTIDAD["LICITAPRO_CONTACTO_EMAIL"] in t


async def test_la_marca_se_presta_pero_no_se_atribuye(cliente, monkeypatch):
    """La frase que no se puede escribir mal, en las seis paginas que la llevan.

    Los certificados de Indecopi 00165236 y 00162741 estan a nombre de dos
    personas naturales, NO de la sociedad. El sitio puede decir que presta el
    servicio bajo esa marca; no puede decir que es su titular.

    No es un matiz de redactor: quien valida un comercio comprueba el registro,
    y ahi la titularidad afirmada de mas se cae sola. Se fija aqui porque es el
    tipo de frase que alguien "mejora" un martes sin saber lo que hay detras.
    """
    monkeypatch.setenv("LICITAPRO_RAZON_SOCIAL", "EJEMPLO DE PRUEBA S.A.C.")
    monkeypatch.setenv("LICITAPRO_RUC", "20123456789")
    monkeypatch.setenv("LICITAPRO_MARCA", "Marca De Prueba")
    monkeypatch.setenv("LICITAPRO_MARCA_CERTIFICADO", "00000001")

    for ruta in ("/", "/precios", "/comprar/pro", "/terminos", "/privacidad"):
        t = (await cliente.get(ruta)).text
        assert "prestado bajo la marca" in t, ruta
        assert "Marca De Prueba" in t, ruta
        # Lo que NUNCA puede aparecer.
        for prohibido in ("titular de la marca", "marca propia",
                          "somos titulares", "marca de nuestra propiedad"):
            assert prohibido not in t.lower(), f"{ruta}: afirma titularidad"


async def test_sin_marca_configurada_la_linea_no_deja_hueco(cliente, monkeypatch):
    """Lo que falta no se pinta a medias: o la frase entera, o nada."""
    monkeypatch.setenv("LICITAPRO_RAZON_SOCIAL", "EJEMPLO DE PRUEBA S.A.C.")
    monkeypatch.setenv("LICITAPRO_RUC", "20123456789")
    monkeypatch.delenv("LICITAPRO_MARCA", raising=False)
    monkeypatch.delenv("LICITAPRO_MARCA_CERTIFICADO", raising=False)

    t = (await cliente.get("/terminos")).text
    assert "EJEMPLO DE PRUEBA S.A.C." in t
    assert "prestado bajo la marca" not in t
    assert "Indecopi" in t          # la clausula de jurisdiccion, que sigue
    assert "clase 42" not in t      # pero no un certificado sin marca


async def test_el_checkout_y_la_privacidad_nombran_al_mismo_responsable(cliente, monkeypatch):
    """Una politica que no coincide con el pie del cobro es peor que no tenerla.

    Las dos leen las mismas variables, asi que discrepar solo es posible si
    alguien vuelve a escribir los datos a mano en una de las dos plantillas.
    """
    for v, valor in _IDENTIDAD.items():
        monkeypatch.setenv(v, valor)
    for ruta in ("/privacidad", "/precios", "/comprar/pro"):
        t = (await cliente.get(ruta)).text
        assert _IDENTIDAD["LICITAPRO_RAZON_SOCIAL"] in t, ruta
        assert _IDENTIDAD["LICITAPRO_RUC"] in t, ruta


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


# ─── Deteccion de adjudicaciones ─────────────────────────

@pytest.mark.parametrize("mia, gano, casan", [
    ("Constructora Andina S.A.C.", "CONSTRUCTORA ANDINA SAC", True),
    ("Sotomayor Fam E.I.R.L.", "SOTOMAYOR FAM EIRL", True),
    ("Servicios Generales del Perú S.A.C.", "SERVICIOS GENERALES DEL PERU", True),
    # Una letra de diferencia es otra empresa.
    ("Constructora Andina SAC", "CONSTRUCTORA ANDINO SAC", False),
    ("ABC SAC", "XYZ SAC", False),
])
def test_el_cruce_de_nombres_no_confunde_empresas(mia, gano, casan):
    """El cruce va por nombre porque la API no entrega el RUC del proveedor.

    Medido: de 2.702 procesos resueltos, 2.664 traen el nombre del ganador y
    CERO traen su RUC. Un nombre es buena pista y mala prueba, asi que la
    normalizacion tiene que quitar la forma societaria sin llegar a fundir dos
    empresas distintas.
    """
    from shared.notificaciones import _clave_empresa
    assert (_clave_empresa(mia) == _clave_empresa(gano)) is casan


def test_un_nombre_que_queda_vacio_no_casa_con_nada():
    """"SAC" a secas se queda en nada al normalizar. Sin comprobar el vacio,
    dos cadenas vacias serian iguales y le diriamos a alguien que gano un
    proceso que no gano: el peor falso positivo que puede dar esto."""
    from shared.notificaciones import _clave_empresa
    assert _clave_empresa("SAC") == ""
    assert _clave_empresa("S.A.C.") == ""


async def test_la_adjudicacion_avisa_pero_no_crea_el_contrato(usuario, marca):
    """Crear el contrato con un cruce por nombre seria meter datos falsos en la
    cuenta de alguien y hacerle perseguir un cobro que no existe."""
    from shared.db import connection
    from shared.notificaciones import detectar_adjudicaciones

    lic = f"PRUEBA-ADJ-{marca}"
    async with connection() as c:
        eid = await c.fetchval(
            """INSERT INTO empresas (razon_social, ruc, usuario_id, activa)
               VALUES ($1, $2, $3, TRUE) RETURNING id""",
            "Constructora Andina S.A.C.", "20" + marca[:9], usuario["id"])
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto,
                                         fecha_cierre, proveedor_ganador)
               VALUES ($1, 'prueba', 'ENTIDAD', 'Obra',
                       NOW() - INTERVAL '5 days', 'CONSTRUCTORA ANDINA SAC')""",
            lic)
        await c.execute(
            "INSERT INTO propuestas (licitacion_id, empresa_id, estado) "
            "VALUES ($1, $2, 'enviado')", lic, eid)
        # WhatsApp activo: en modo simulado el envio confirma, que es lo que
        # permite comprobar que el aviso se anota.
        await c.execute(
            "UPDATE usuarios SET whatsapp_numero='+51987654321', "
            "whatsapp_estado='activo' WHERE id=$1", usuario["id"])
    try:
        parte = await detectar_adjudicaciones()
        assert parte["coincidencias"] >= 1

        async with connection() as c:
            anotados = await c.fetchval(
                "SELECT COUNT(*) FROM notificaciones_enviadas "
                "WHERE canal='adjudicacion' AND usuario_id=$1", usuario["id"])
            contratos = await c.fetchval(
                "SELECT COUNT(*) FROM contratos WHERE empresa_id=$1", eid)
        assert anotados == 1
        assert contratos == 0, "no debe crear el contrato por su cuenta"

        # La siguiente pasada del planificador no puede repetir el aviso.
        segunda = await detectar_adjudicaciones()
        assert segunda["avisados"] == 0
    finally:
        # La propuesta va primero: propuestas.licitacion_id sigue en NO ACTION
        # a proposito. Una licitacion es registro publico y no debe poder
        # borrarse mientras alguien tenga una propuesta apoyada en ella; al
        # borrar la CUENTA si desaparece todo, por la cascada desde empresas.
        async with connection() as c:
            await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lic)
            await c.execute("DELETE FROM licitaciones WHERE id=$1", lic)


# ─── Cobro de renovaciones ───────────────────────────────

async def test_nunca_se_cobra_un_plan_de_precio_cero(usuario):
    """Quien cae al plan gratuito conservando su tarjeta no debe generar cobros.

    Sin este filtro, la renovacion diaria lanzaria una orden de S/0.00 contra
    la pasarela, esta la rechazaria, sumaria un intento fallido y acabaria
    suspendiendo a un usuario que no debe nada.
    """
    from shared.db import connection
    from shared.seguridad import cifrar
    from shared.suscripciones import renovaciones_pendientes

    async with connection() as c:
        await c.execute(
            """UPDATE suscripciones
                  SET plan_codigo='gratis', estado='vencida',
                      vence = NOW() - INTERVAL '5 days', token_tarjeta = $2
                WHERE usuario_id = $1""",
            usuario["id"], cifrar("token-de-prueba"))

    pendientes = await renovaciones_pendientes()
    assert not any(p["usuario_id"] == usuario["id"] for p in pendientes)

    # Y el mismo usuario en un plan de pago SI debe entrar, con su importe.
    async with connection() as c:
        await c.execute(
            "UPDATE suscripciones SET plan_codigo='pro' WHERE usuario_id=$1",
            usuario["id"])
    mios = [p for p in await renovaciones_pendientes()
            if p["usuario_id"] == usuario["id"]]
    assert len(mios) == 1
    assert float(mios[0]["monto"]) == 99.0


async def test_los_precios_anuales_son_coherentes():
    """El anual es diez mensualidades en los tres planes de pago: dos meses de
    regalo. Si alguien cambia un precio suelto, esto lo detiene antes de que un
    cliente pague un importe que no cuadra con lo que promete la web."""
    from shared.db import connection

    async with connection() as c:
        planes = await c.fetch(
            "SELECT codigo, precio_mensual, precio_anual FROM planes "
            "WHERE activo = TRUE AND precio_mensual > 0")

    assert planes, "no hay planes de pago activos"
    for p in planes:
        esperado = float(p["precio_mensual"]) * 10
        assert float(p["precio_anual"]) == esperado, (
            f"{p['codigo']}: anual {p['precio_anual']} no son 10 mensualidades "
            f"de {p['precio_mensual']}")
