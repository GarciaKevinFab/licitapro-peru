-- Rol de la aplicacion SIN superusuario: licitapro_app.
--
-- POR QUE
--
--   La web y los tres bots se conectaban con `licitapro`, que es SUPERUSER
--   (lo crea asi la imagen de postgres con POSTGRES_USER). Una inyeccion SQL,
--   o cualquier fallo que deje ejecutar SQL arbitrario desde la app, con un
--   superusuario no se queda en la base: `COPY ... TO PROGRAM` ejecuta
--   comandos en el contenedor, `pg_read_server_files` lee archivos, y se
--   pueden crear roles o borrar otras bases. Con este rol lo peor que se puede
--   hacer es leer y escribir filas de las tablas de LicitaPro.
--
--   `licitapro` sigue existiendo y sigue siendo el DUENO de las tablas: lo
--   usan las migraciones (servicio `migraciones` del compose) y el respaldo.
--
-- QUE PUEDE Y QUE NO
--
--   Puede:    SELECT/INSERT/UPDATE/DELETE en todas las tablas de `public`,
--             usar las secuencias (SERIAL), y lo mismo en las tablas que las
--             migraciones creen mas adelante (ALTER DEFAULT PRIVILEGES).
--   No puede: crear, alterar ni borrar tablas (CREATE en el esquema se le
--             niega), TRUNCATE, COPY a/desde archivos del servidor, crear
--             roles o bases, ni nada de superusuario. La app no usa nada de
--             eso (revisado el 20/10/2026: shared, web, prep_bot, radar_bot,
--             win_bot). pg_advisory_xact_lock no exige privilegios.
--
-- USO (idempotente: correrlo dos veces deja lo mismo, y vuelve a fijar la
-- contrasena, que es como se rota)
--
--   docker exec -i licitapro-postgres-1 psql -U licitapro -d licitapro \
--       -v ON_ERROR_STOP=1 -v clave="$PG_APP_PASSWORD" < tools/crear_rol_app.sql
--
--   Sin `-v clave=...` el script se detiene antes de tocar nada.
--
-- DEBE CORRERLO `licitapro` (el dueno de las tablas): ALTER DEFAULT PRIVILEGES
-- FOR ROLE licitapro solo surte efecto si lo ejecuta ese rol o un superusuario.

\set ON_ERROR_STOP on

-- Sin contrasena, o con una corta, no se sigue. `:{?clave}` dice si existe.
\if :{?clave}
\else
  \echo 'ERROR: falta -v clave=... (la contrasena de licitapro_app).'
  SELECT 1 / 0 AS falta_la_clave;
\endif
SELECT length(:'clave') >= 16 AS clave_valida \gset
\if :clave_valida
\else
  \echo 'ERROR: la contrasena de licitapro_app tiene que tener al menos 16 caracteres.'
  SELECT 1 / 0 AS clave_corta;
\endif

-- Crear o, si ya existe, volver a fijar atributos y contrasena (asi se rota,
-- y se deshace cualquier ALTER ROLE ... SUPERUSER que alguien hiciera a mano).
-- psql no sustituye variables dentro de un bloque DO, por eso \gexec.
SELECT format('%s ROLE licitapro_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
              'NOREPLICATION NOBYPASSRLS PASSWORD %L',
              CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'licitapro_app')
                   THEN 'ALTER' ELSE 'CREATE' END,
              :'clave') \gexec

-- Conectarse a ESTA base (la del -d), y nada mas.
SELECT format('GRANT CONNECT ON DATABASE %I TO licitapro_app', current_database()) \gexec

-- El esquema se usa pero no se modifica. En Postgres 15+ PUBLIC ya no tiene
-- CREATE en `public`; se revoca igual por si la base viene de una version
-- anterior (el volcado de Supabase, por ejemplo).
GRANT USAGE ON SCHEMA public TO licitapro_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM licitapro_app;

-- Lo que ya existe.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO licitapro_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO licitapro_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO licitapro_app;

-- Lo que creen las migraciones de aqui en adelante (las corre `licitapro`).
-- Sin esto, la primera migracion que anada una tabla la dejaria invisible
-- para la app: "permission denied for table ..." en produccion.
ALTER DEFAULT PRIVILEGES FOR ROLE licitapro IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO licitapro_app;
ALTER DEFAULT PRIVILEGES FOR ROLE licitapro IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO licitapro_app;
ALTER DEFAULT PRIVILEGES FOR ROLE licitapro IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO licitapro_app;

-- Comprobacion final, a la vista de quien lo corre.
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin
  FROM pg_roles WHERE rolname = 'licitapro_app';
