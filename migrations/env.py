"""Entorno Alembic. La URL sale del entorno, nunca del .ini, para que el
archivo de configuracion pueda commitearse sin credenciales dentro."""
import os
from logging.config import fileConfig
from urllib.parse import urlparse

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


def _importar_es_gestionado():
    """`shared.db._es_gestionado`, sin el `load_dotenv()` que trae al importarse.

    POR QUE SE IMPORTA Y NO SE COPIA

      Decidir "esta base exige TLS" en dos sitios con dos reglas distintas es
      como acabo este archivo forzando sslmode=require contra todo, incluido el
      Postgres del VPS (host `postgres`), que no habla TLS: la migracion moria
      con "server does not support SSL connection" justo en el despliegue.

    POR QUE SE DESHACE LO QUE CARGA load_dotenv

      `shared/db.py` llama a `load_dotenv()` al importarse. Alembic nunca leyo
      el .env -- la URL sale del entorno de quien lo lanza -- y empezar a
      leerlo por un import haria que un `alembic upgrade head` en un portatil
      sin variables saltara a la DATABASE_URL del .env, que es la de
      produccion. Se devuelve el entorno tal como estaba antes del import.
    """
    antes = dict(os.environ)
    try:
        from shared.db import _es_gestionado
    finally:
        for clave in set(os.environ) - set(antes):
            del os.environ[clave]
        os.environ.update(antes)
    return _es_gestionado


_es_gestionado = _importar_es_gestionado()


def _password_obligatoria() -> str:
    """Como en `shared/db.py`: sin valor por defecto.

    Antes caia a 'licitapro'. Una contrasena por defecto escrita en un
    repositorio es una contrasena conocida en toda instalacion que no la
    cambio, y ademas esconde el error real (falta la variable) detras de un
    "password authentication failed" que apunta a otro sitio.
    """
    clave = os.getenv("POSTGRES_PASSWORD")
    if not clave:
        raise RuntimeError(
            "Falta POSTGRES_PASSWORD (o DATABASE_URL). Alembic no adivina la "
            "contrasena de la base: exportala antes de migrar.")
    return clave


def url_bd() -> str:
    """La misma base a la que apunta la aplicacion.

    DATABASE_URL MANDA SOBRE LAS PIEZAS SUELTAS

      `shared/db.py` ya la respetaba; esto no. Con Supabase configurada, la app
      hablaba con el pooler y Alembic intentaba `localhost:5433`: las
      migraciones fallaban con "connection refused" mientras todo lo demas
      parecia bien puesto, o -- peor -- las aplicaba contra una base local
      vacia que ningun otro proceso usa.

    POR QUE SE REESCRIBE EL ESQUEMA DE LA URL

      Supabase entrega `postgresql://...`. Con ese prefijo, SQLAlchemy elige su
      driver por defecto, que en SQLAlchemy 2 no es psycopg2; aqui el
      controlador instalado es psycopg2, asi que se fuerza.

    POR QUE sslmode=require SOLO EN BASES GESTIONADAS

      Supabase solo acepta TLS, y sin esto la conexion se rechaza con un
      mensaje que no apunta a la causa. Pero el Postgres del VPS y el local no
      tienen TLS, y forzarlo alli rompe la conexion igual de mal. La regla es
      la de `get_pool` (`_es_gestionado`), y vale tambien para las piezas
      sueltas: un POSTGRES_HOST de Supabase necesita TLS igual que su URL.
      Si la URL ya trae su propio sslmode, se respeta.
    """
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        for viejo, nuevo in (("postgresql+asyncpg://", "postgresql+psycopg2://"),
                             ("postgres://", "postgresql+psycopg2://"),
                             ("postgresql://", "postgresql+psycopg2://")):
            if url.startswith(viejo):
                url = nuevo + url[len(viejo):]
                break
        if "sslmode=" not in url and _es_gestionado(urlparse(url).hostname or ""):
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return url

    host = os.getenv("POSTGRES_HOST", "localhost")
    url = (
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER', 'licitapro')}:"
        f"{_password_obligatoria()}@"
        f"{host}:"
        f"{os.getenv('POSTGRES_PORT', '5433')}/"
        f"{os.getenv('POSTGRES_DB', 'licitapro')}"
    )
    return url + ("?sslmode=require" if _es_gestionado(host) else "")


# LOS % DE LA URL HAY QUE DUPLICARLOS AQUI
#
#   `set_main_option` escribe en un configparser, que trata `%` como sintaxis
#   de interpolacion. La contrasena de Supabase llega percent-encoded -- %40
#   por @, %23 por # -- y eso basta para que Alembic ni arranque:
#
#     ValueError: invalid interpolation syntax at position 58
#
#   El mensaje no menciona la contrasena ni el .env, asi que se parece mas a un
#   alembic.ini roto que a lo que es. Duplicando los % queda bien guardado, y
#   `config.get_section()` los devuelve a uno solo al leerlos de vuelta, que es
#   por donde pasa run_migrations_online().
#
#   run_migrations_offline() NO necesita esto: usa url_bd() directamente, sin
#   configparser de por medio.
config.set_main_option("sqlalchemy.url", url_bd().replace("%", "%%"))
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(url=url_bd(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
