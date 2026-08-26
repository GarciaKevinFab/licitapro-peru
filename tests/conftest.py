"""Piezas comunes de las pruebas.

POR QUE HAY DOS CLASES DE PRUEBA AQUI

  Las de logica pura (plazos, banderas, numeros de telefono) no tocan nada
  externo y corren siempre, en cualquier maquina y en cualquier CI. Son las que
  protegen los calculos donde un error se ve como un numero plausible y no como
  una excepcion: una fecha limite mal contada no revienta, solo esta mal.

  Las de integracion necesitan PostgreSQL. Si no hay base se SALTAN con un
  motivo visible, en vez de fallar: una suite que falla porque falta la
  infraestructura acostumbra a la gente a ignorar el rojo, y a partir de ahi
  deja de servir para nada.

CADA PRUEBA LIMPIA LO SUYO

  Los usuarios de prueba se crean con un sufijo aleatorio y se borran al final,
  incluso si la prueba falla. Compartir la base de desarrollo con datos reales
  obliga a esto: una prueba que deja basura acaba haciendo fallar a la
  siguiente por un RUC duplicado, y se pierde media tarde buscando un fallo que
  no existe.
"""
import os
import uuid

import pytest
import pytest_asyncio

# Sin clave la app se niega a arrancar fuera de desarrollo. Se pone una de
# pruebas antes de importar nada del proyecto.
os.environ.setdefault("LICITAPRO_SECRET_KEY",
                      "clave_solo_para_pruebas_no_usar_en_produccion")
os.environ.setdefault("LICITAPRO_ENTORNO", "dev")


def _hay_base() -> bool:
    return bool(os.getenv("POSTGRES_PASSWORD"))


sin_base = pytest.mark.skipif(
    not _hay_base(),
    reason="No hay POSTGRES_PASSWORD: se omiten las pruebas que usan la base.")


@pytest.fixture
def marca() -> str:
    """Sufijo unico para no chocar con datos que ya existan."""
    return uuid.uuid4().hex[:10]


@pytest_asyncio.fixture
async def usuario(marca):
    """Un usuario recien creado, con su suscripcion de prueba. Se borra al final.

    El borrado va en el finally para que una prueba que falla no deje la cuenta
    detras: la siguiente ejecucion chocaria con el correo repetido y el fallo
    pareceria otro.
    """
    from shared.db import borrar_cuenta, crear_usuario
    from shared.seguridad import hashear_password

    # Se usa crear_usuario y no un INSERT directo a proposito: esa funcion crea
    # tambien la fila de user_config y la suscripcion de prueba. Un INSERT a
    # mano deja la cuenta a medias, y entonces las pruebas comprueban un estado
    # que ningun usuario real llega a tener nunca -- que es la forma callada de
    # que una suite pase mientras el producto falla.
    email = f"prueba-{marca}@ejemplo.pe"
    fila = await crear_usuario(email, hashear_password("ClaveDePrueba123!"),
                               "Cuenta de prueba")
    uid = fila["id"]
    try:
        yield {"id": uid, "email": email, "password": "ClaveDePrueba123!"}
    finally:
        try:
            await borrar_cuenta(uid)
        except Exception:
            pass


@pytest_asyncio.fixture
async def empresa(usuario, marca):
    """Una empresa del usuario de la prueba. Cae con la cuenta."""
    from shared.db import connection
    async with connection() as c:
        return await c.fetchval(
            """INSERT INTO empresas (razon_social, ruc, usuario_id, activa)
               VALUES ($1, $2, $3, TRUE) RETURNING id""",
            f"Empresa {marca} SAC", "20" + marca[:9], usuario["id"])


@pytest_asyncio.fixture
async def cliente():
    """Cliente HTTP contra la app, sin seguir redirecciones.

    Sin seguirlas a proposito: media prueba de esta aplicacion consiste en
    comprobar que algo devuelve 303 hacia el sitio correcto, y siguiendolas se
    veria el 200 del destino y pasaria por buena una redireccion equivocada.
    """
    import httpx
    from web.app import app
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transporte, base_url="http://pruebas",
                                 follow_redirects=False) as c:
        yield c
