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

  Y limpiar lo suyo solo es suficiente si la base es la correcta. Durante un
  tiempo no lo fue: ver el bloque de blindaje mas abajo.
"""
import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from dotenv import dotenv_values

# Sin clave la app se niega a arrancar fuera de desarrollo. Se pone una de
# pruebas antes de importar nada del proyecto.
os.environ.setdefault("LICITAPRO_SECRET_KEY",
                      "clave_solo_para_pruebas_no_usar_en_produccion")
os.environ.setdefault("LICITAPRO_ENTORNO", "dev")

# El limite de peticiones por IP, apagado durante las pruebas.
#
#   La suite habla con la app por httpx.ASGITransport, que no abre un socket:
#   no hay cabecera de Cloudflare y `scope["client"]` no identifica a nadie, asi
#   que las 22 llamadas a POST /entrar caen todas en la MISMA clave. A partir de
#   la undecima llega el 429, la prueba se queda sin sesion y las siguientes
#   acaban en /entrar. Se veia como `assert 303 == 200` y
#   `'/propuestas/3' in '/entrar'` en pruebas que no tienen nada que ver con el
#   login, que es la peor forma de encontrarse un fallo.
#
#   Subir el limite hasta que la suite quepa seria dejar que el arnes decida un
#   numero que le toca a lo que necesita una persona que se equivoca de
#   contrasena. El middleware se prueba aparte y a fondo en tests/test_limites.py.
os.environ.setdefault("LICITAPRO_LIMITE_PETICIONES", "off")

# ─── Blindaje: la suite no puede tocar la base de produccion ─────────────────
#
# ESTO NO ES PRECAUCION TEORICA: PASABA
#
#   `shared/db.py` llama a `load_dotenv()` al importarse, y el `.env` de esta
#   maquina trae la DATABASE_URL de Supabase. Es decir, cualquiera que lanzara
#   `pytest` en su portatil se conectaba a PRODUCCION sin enterarse. Y estas
#   pruebas no solo leen: crean usuarios, empresas y licitaciones, y despues
#   los BORRAN. `borrar_cuenta` sobre la base equivocada es exactamente lo que
#   parece.
#
#   No saltaba ninguna alarma porque todo "funcionaba": las pruebas pasaban en
#   verde. Un fallo asi solo se ve el dia que una prueba muere a medias y deja
#   basura -- o se lleva algo por delante -- en la base de los clientes.
#
# COMO SE CIERRA, EN DOS CAPAS
#
#   1. Se NEUTRALIZA DATABASE_URL antes de que nadie importe el proyecto. Se
#      pone vacia y PRESENTE en el entorno: `load_dotenv()` no pisa lo que ya
#      existe, asi que la del `.env` no llega a entrar. Sin URL, `get_pool()`
#      cae a las piezas POSTGRES_*, que apuntan al Postgres local de
#      docker-compose.yml. En el CI no cambia nada: alli no hay DATABASE_URL y
#      las piezas apuntan al contenedor del runner.
#
#   2. Se COMPRUEBA el destino final contra una LISTA BLANCA y se aborta si no
#      esta en ella. La primera capa no basta: alguien podria tener
#      POSTGRES_HOST apuntando directamente a la base de verdad.
#
#      POR QUE LISTA BLANCA Y NO `_es_gestionado`
#
#        Antes se reutilizaba `shared.db._es_gestionado`, que decide por
#        PREFIJO: "local" es todo lo que empieza por localhost, 127., db o
#        postgres. `db.<ref>.supabase.co` empieza por "db", asi que la base de
#        Supabase contaba como local. Y al reves, `localhost` no garantiza
#        nada: con el tunel SSH abierto, localhost:5442 ES produccion (el
#        Postgres del VPS, ver tools/tunel_puente.py; en la PC puente llega
#        por el 15432). Por eso se exige el host EXACTO y se vetan esos
#        puertos aunque el host sea local.
#
#      Y el puerto por defecto es el 5434, el del Postgres de pruebas de este
#      proyecto: el 5433 lo ocupa el Postgres de OTRO proyecto en esta maquina,
#      y las pruebas creaban y borraban cuentas en una base ajena.
#
#   Y se ABORTA, no se salta. Saltar dejaria la suite en verde sin haber
#   probado nada, que es la forma silenciosa de este mismo problema.
#
#   Para correr a proposito contra otra base local -- otro nombre de base, otro
#   usuario -- se usa DATABASE_URL_PRUEBAS. Hay que nombrarla a mano, y eso es
#   el punto: el riesgo no esta en quien lo decide, esta en quien no sabe que
#   lo esta haciendo. Esa via pasa por la misma lista blanca: ninguna URL
#   remota, ni siquiera nombrada a mano.
#
#   Lo unico que esa via NO deja pasar es la URL de produccion. Se compara
#   contra el `.env` LEYENDO EL ARCHIVO, sin meterlo en el entorno: copiar y
#   pegar la de siempre en la variable de escape es justo el descuido que este
#   bloque existe para frenar, y es un descuido comodo de cometer.
_URL_PRODUCCION = (dotenv_values(Path(__file__).resolve().parent.parent / ".env")
                   or {}).get("DATABASE_URL") or ""
_URL_PRUEBAS = os.environ.pop("DATABASE_URL_PRUEBAS", "")

if _URL_PRUEBAS and _URL_PRODUCCION and _URL_PRUEBAS.strip() == _URL_PRODUCCION.strip():
    raise pytest.UsageError(
        "DATABASE_URL_PRUEBAS es exactamente la URL que hay en el .env, o sea "
        "produccion. Esta suite crea y BORRA cuentas: no corre contra ahi.")

os.environ["DATABASE_URL"] = _URL_PRUEBAS

# Se importa ANTES de mirar el destino: `shared.db` llama a `load_dotenv()`, y
# un POSTGRES_HOST o POSTGRES_PORT del .env que entrara despues de la
# comprobacion cambiaria la base a la que se conecta la app sin que el
# blindaje lo hubiera visto.
import shared.db  # noqa: F401

# El puerto por defecto de get_pool es el 5433 (el de docker-compose). Se fija
# aqui en el entorno, no solo en la comprobacion: si no, el blindaje miraria el
# 5434 y la app se conectaria al 5433.
os.environ.setdefault("POSTGRES_PORT", "5434")

# Hosts que son esta maquina o el contenedor de la base del compose/CI, por su
# nombre EXACTO. Nada de prefijos: ver el bloque de arriba.
HOSTS_PERMITIDOS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db"})
# Puertos locales que en realidad son produccion a traves del tunel SSH.
PUERTOS_VETADOS = frozenset({5442, 15432})


def _destino_de_las_pruebas() -> tuple[str, int]:
    """Host y puerto a los que se conectarian las pruebas, ya resueltos."""
    url = os.getenv("DATABASE_URL") or ""
    if url:
        partes = urlparse(url)
        return (partes.hostname or ""), (partes.port or 5432)
    return os.getenv("POSTGRES_HOST", "localhost"), int(
        os.getenv("POSTGRES_PORT", "5434"))


def motivo_para_abortar(host: str, puerto: int) -> str | None:
    """Por que las pruebas NO pueden correr contra host:puerto, o None si pueden."""
    if (host or "").strip().lower() not in HOSTS_PERMITIDOS:
        return (f"Las pruebas apuntan a {host!r}, que no es una base local "
                f"(se admiten solo {', '.join(sorted(HOSTS_PERMITIDOS))}).")
    if puerto in PUERTOS_VETADOS:
        return (f"Las pruebas apuntan a {host}:{puerto}, el puerto del tunel SSH "
                f"a la base de PRODUCCION del VPS.")
    return None


_HOST, _PUERTO = _destino_de_las_pruebas()

if (_motivo := motivo_para_abortar(_HOST, _PUERTO)):
    raise pytest.UsageError(
        f"{_motivo} Se aborta: esta suite crea y BORRA cuentas. Apunta "
        f"POSTGRES_HOST/POSTGRES_PORT al Postgres local de pruebas (por defecto "
        f"localhost:5434), o declara DATABASE_URL_PRUEBAS con una URL local.")


def _hay_base() -> bool:
    """Si hay algo escuchando de verdad, no solo una contrasena en el entorno.

    Antes bastaba con que POSTGRES_PASSWORD existiera. Con eso, una maquina sin
    el Postgres local levantado no saltaba las pruebas: las intentaba y moria
    con un error de conexion, que parece un fallo del codigo y es del entorno.
    Media decision se tomaba con un dato que no decia nada.
    """
    if not os.getenv("POSTGRES_PASSWORD"):
        return False
    try:
        with socket.create_connection((_HOST, _PUERTO), timeout=1.5):
            return True
    except OSError:
        return False


sin_base = pytest.mark.skipif(
    not _hay_base(),
    reason=f"No hay PostgreSQL en {_HOST}:{_PUERTO}: se omiten las pruebas "
           f"que usan la base.")


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
        except Exception as e:  # noqa: BLE001
            # La limpieza no puede tumbar la prueba: si el fallo real fue otro,
            # este error taparia el que importa. Pero se imprime, o una cuenta
            # que no se borra se convierte en una prueba que falla mañana sin
            # motivo aparente.
            print(f"aviso: no se pudo borrar la cuenta de prueba {uid}: {e}")


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
