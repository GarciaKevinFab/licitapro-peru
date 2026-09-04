"""Pruebas del analisis con IA y de las puertas que lo rodean.

QUE PROTEGEN

  El analisis con IA se cobra en los planes Pro y Empresa, y cada llamada la
  paga la plataforma. Hay tres formas de perder dinero o clientes aqui, y una
  prueba para cada una:

    - Que un plan sin IA pueda usarla. Se regala justo lo que se cobra.
    - Que el tope mensual no frene. Una cuenta en bucle gasta mas de lo que
      paga, y el primer aviso seria la factura.
    - Que el analisis de un inquilino se le muestre a otro. El analisis lleva
      dentro la experiencia y el equipo de una empresa concreta: filtrarlo es
      filtrar datos de negocio de un cliente a otro.

  La cuarta es de producto, no de dinero: un expediente sin los campos legales
  obligatorios no es un expediente incompleto, es una oferta que la entidad
  devuelve en mesa de partes cuando ya vencio el plazo.

NINGUNA LLAMA A LA API

  Se prueban las puertas, no el modelo. Una suite que gasta dinero real cada
  vez que corre acaba sin correrse, y ademas fallaria por causas ajenas al
  codigo -- una caida de la API, un cambio de latencia -- entrenando a todo el
  mundo a ignorar el rojo.
"""
import pytest

from tests.conftest import sin_base

pytestmark = [pytest.mark.asyncio, sin_base]


async def _licitacion_de_prueba(marca: str) -> str:
    """Una licitacion sintetica. El pozo es compartido, asi que se identifica."""
    from shared.db import connection
    lid = f"prueba-ia-{marca}"
    async with connection() as c:
        await c.execute(
            """INSERT INTO licitaciones (id, fuente, entidad, objeto,
                                         monto_referencial, tipo, departamento)
               VALUES ($1,'prueba','Entidad de prueba',
                       'Servicio de prueba para el analisis con IA',
                       100000, 'AS', 'Lima')
               ON CONFLICT (id) DO NOTHING""", lid)
    return lid


async def _borrar_licitacion(lid: str) -> None:
    """Se borra lo que cuelga antes que la licitacion.

    `propuestas.licitacion_id` tiene clave foranea sin cascada, asi que
    borrarla de primeras falla y deja la fila de prueba en el pozo compartido,
    donde la siguiente ejecucion se la encuentra.
    """
    from shared.db import connection
    async with connection() as c:
        await c.execute("DELETE FROM propuestas WHERE licitacion_id=$1", lid)
        await c.execute("DELETE FROM licitaciones WHERE id=$1", lid)


async def test_el_plan_gratuito_no_puede_usar_la_ia(usuario):
    """La IA es la linea de pago entre Basico y Pro, y cuesta dinero real.

    `puede_usar_ia` existia y estaba probada desde el principio; lo que no
    existia era nadie que la llamase. Esta mira la cuota, que es lo que ahora
    consulta la ruta antes de gastar.
    """
    from shared import ia
    from shared.suscripciones import cambiar_plan

    await cambiar_plan(usuario["id"], "gratis", "mensual")
    c = await ia.cuota(usuario["id"])
    assert c["permitido"] is False
    assert c["por_plan"] is False
    # El tope se informa igual, porque la ficha lo pinta para explicar por que
    # no hay boton: "0 de 0" con el nombre del plan dice mas que nada.
    assert c["tope"] == 0


async def test_el_plan_pro_tiene_ia_con_tope(usuario):
    from shared import ia
    from shared.suscripciones import cambiar_plan

    await cambiar_plan(usuario["id"], "pro", "mensual")
    c = await ia.cuota(usuario["id"])
    assert c["permitido"] is True
    assert c["tope"] == 60
    assert c["restantes"] == 60


async def test_el_tope_frena_cuando_se_agota(usuario, empresa, marca):
    """Sin esto, una cuenta que pulse en bucle gasta sin limite."""
    from shared import ia
    from shared.db import connection
    from shared.suscripciones import cambiar_plan

    lid = await _licitacion_de_prueba(marca)
    try:
        await cambiar_plan(usuario["id"], "pro", "mensual")
        # Se baja el tope del plan para no insertar sesenta filas: lo que se
        # prueba es la comparacion, no el numero.
        async with connection() as c:
            await c.execute("UPDATE planes SET analisis_ia_mes=1 WHERE codigo='pro'")
        try:
            await ia.guardar_analisis(usuario["id"], empresa, lid,
                                      {"score_viabilidad": 50}, ia.ORIGEN_IA)
            c2 = await ia.cuota(usuario["id"])
            assert c2["usados"] == 1
            assert c2["restantes"] == 0
            assert c2["permitido"] is False
        finally:
            async with connection() as c:
                await c.execute(
                    "UPDATE planes SET analisis_ia_mes=60 WHERE codigo='pro'")
    finally:
        await _borrar_licitacion(lid)


async def test_el_heuristico_no_gasta_cuota(usuario, empresa, marca):
    """El respaldo no se cobra.

    Cuando la API falla se devuelve el analisis por reglas. Descontarlo del
    tope seria cobrarle al cliente por lo que no recibio, y dejaria sin
    analisis del mes a quien tuvo la mala suerte de pulsar durante una caida.
    """
    from shared import ia
    from shared.suscripciones import cambiar_plan

    lid = await _licitacion_de_prueba(marca)
    try:
        await cambiar_plan(usuario["id"], "pro", "mensual")
        await ia.guardar_analisis(usuario["id"], empresa, lid,
                                  {"score_viabilidad": 50}, ia.ORIGEN_HEURISTICO)
        c = await ia.cuota(usuario["id"])
        assert c["usados"] == 0
        assert c["permitido"] is True
    finally:
        await _borrar_licitacion(lid)


async def test_el_analisis_de_uno_no_lo_ve_otro(usuario, empresa, marca):
    """El analisis lleva dentro la experiencia y el equipo de una empresa.

    Por eso NO puede vivir en `licitaciones.bases_analisis`, que es una fila
    compartida por todos los inquilinos: el analisis hecho con los datos de una
    constructora se le mostraria a la siguiente empresa que abriera la ficha.
    Es el mismo fallo que tuvo `licitaciones.notificado`.
    """
    from shared import ia
    from shared.db import borrar_cuenta, crear_usuario
    from shared.seguridad import hashear_password

    lid = await _licitacion_de_prueba(marca)
    otro = await crear_usuario(f"otro-{marca}@ejemplo.pe",
                               hashear_password("ClaveDePrueba123!"), "Otro")
    try:
        await ia.guardar_analisis(usuario["id"], empresa, lid,
                                  {"score_viabilidad": 91, "resumen": "secreto"},
                                  ia.ORIGEN_IA)
        assert len(await ia.analisis_guardado(usuario["id"], lid)) == 1
        assert await ia.analisis_guardado(otro["id"], lid) == []
    finally:
        await borrar_cuenta(otro["id"])
        await _borrar_licitacion(lid)


async def test_el_expediente_se_niega_si_faltan_los_campos_legales(
        usuario, empresa, marca, cliente):
    """Generar el ZIP sin el DNI del representante ni la partida registral no
    produce un expediente incompleto: produce una oferta que la entidad devuelve
    en mesa de partes. Y eso se descubre cuando ya vencio el plazo.

    El boton sale deshabilitado en la plantilla, pero eso es cosmetica: lo que
    frena de verdad es la ruta, que es adonde llega un formulario enviado a mano.
    """
    from shared.db import connection

    lid = await _licitacion_de_prueba(marca)
    try:
        async with connection() as c:
            pid = await c.fetchval(
                """INSERT INTO propuestas (licitacion_id, empresa_id, estado)
                   VALUES ($1,$2,'iniciado') RETURNING id""", lid, empresa)

        await cliente.post("/entrar", data={"email": usuario["email"],
                                            "password": usuario["password"]})
        r = await cliente.post(f"/propuestas/{pid}/expediente")

        assert r.status_code == 303
        destino = r.headers["location"]
        assert f"/propuestas/{pid}" in destino
        # El mensaje nombra lo que falta. "No se pudo armar el expediente" deja
        # al usuario sin saber que hacer a continuacion.
        assert "falta" in destino.lower()
    finally:
        await _borrar_licitacion(lid)
