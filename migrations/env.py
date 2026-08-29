"""Entorno Alembic. La URL sale del entorno, nunca del .ini, para que el
archivo de configuracion pueda commitearse sin credenciales dentro."""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


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

    POR QUE SE ANADE sslmode=require

      Supabase solo acepta TLS. Sin esto la conexion se rechaza con un mensaje
      que no apunta a la causa.
    """
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        for viejo, nuevo in (("postgresql+asyncpg://", "postgresql+psycopg2://"),
                             ("postgres://", "postgresql+psycopg2://"),
                             ("postgresql://", "postgresql+psycopg2://")):
            if url.startswith(viejo):
                url = nuevo + url[len(viejo):]
                break
        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        return url

    return (
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER', 'licitapro')}:"
        f"{os.getenv('POSTGRES_PASSWORD', 'licitapro')}@"
        f"{os.getenv('POSTGRES_HOST', 'localhost')}:"
        f"{os.getenv('POSTGRES_PORT', '5433')}/"
        f"{os.getenv('POSTGRES_DB', 'licitapro')}"
    )


config.set_main_option("sqlalchemy.url", url_bd())
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
