#!/usr/bin/env bash
# Mudanza de la base de LicitaPro: de Supabase al Postgres del propio VPS.
#
# POR QUE
#
#   Cada consulta iba y volvia a Supabase en ~50 ms, y una pagina hace
#   decenas: la ficha de una propuesta llego a tardar 2 s solo esperando a la
#   red. En el mismo servidor esa espera desaparece.
#
# QUE SE MUDA Y QUE NO
#
#   Solo el esquema `public`, que es LicitaPro. En la misma base de Supabase
#   vive `bot_global`, de /opt/bot-global: NO se toca y sigue en Supabase.
#   auth, storage, realtime y vault son de Supabase y estan vacios para
#   nosotros.
#
# MODOS (correr desde /opt/licitapro, como root)
#
#   ensayo   Copia Supabase -> Postgres del VPS SIN parar nada ni cambiar la
#            conexion, y compara cuantas filas hay en cada tabla. Se puede
#            repetir: cada pasada borra lo que dejo la anterior.
#
#   corte    Para web, radar, prep y win; copia; compara; cambia DATABASE_URL
#            en el .env (guardando el anterior); levanta todo y prueba /salud.
#            Antes: pausar la tarea "LicitaPro - Traer OECE" de la PC puente.
#            Unos 5 minutos sin servicio.
#
#   volver   Deshace el corte: repone el .env anterior (Supabase) y levanta.
#            Lo escrito en el VPS despues del corte NO vuelve a Supabase:
#            pensado para el primer dia, si algo sale mal.
#
# NADA DE ESTO BORRA SUPABASE. Se apaga a mano, dias despues, cuando el VPS
# lleve tiempo funcionando y haya respaldos del VPS ya restaurados de prueba.

set -euo pipefail

MODO="${1:-}"
cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.prod.yml)
IMAGEN_PG="postgres:17-alpine"
TRABAJO="/root/migracion-licitapro"
mkdir -p "$TRABAJO"
chmod 700 "$TRABAJO"

leer_env() {
  # Igual que en respaldar.sh: el .env se LEE, no se ejecuta.
  local v
  v="$(grep -E "^$1=" .env | tail -1 | cut -d= -f2-)" || true
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}

paso() { echo; echo "[$(date '+%F %T')] $*"; }

PG_VPS_PASSWORD="$(leer_env PG_VPS_PASSWORD)"
URL_VPS="postgresql://licitapro:${PG_VPS_PASSWORD}@postgres:5432/licitapro"
# En el .env va con sslmode=disable: el Postgres local no tiene TLS, y sin
# esto Alembic (migrations/env.py) anade sslmode=require y no arranca. Paso en
# el corte del 16/10/2026: la aplicacion quedo dos minutos sin levantar.
URL_VPS_ENV="${URL_VPS}?sslmode=disable"

# La URL de Supabase sale del .env actual o, si ya se hizo el corte, de la
# copia que se guardo al hacerlo.
url_supabase() {
  local u
  u="$(leer_env DATABASE_URL)"
  if [[ "$u" != *supabase* && -f "$TRABAJO/env.antes-del-corte" ]]; then
    u="$(grep -E '^DATABASE_URL=' "$TRABAJO/env.antes-del-corte" | tail -1 | cut -d= -f2-)"
    u="${u%\"}"; u="${u#\"}"
  fi
  # 6543 (pooler de transaccion) no aguanta pg_dump; 5432 es el de sesion.
  printf '%s' "${u/:6543/:5432}"
}

RED=""

psql_vps() {
  # La contrasena viaja por el entorno del contenedor, no por la linea de
  # comandos: los argumentos los ve cualquiera que liste procesos.
  docker run --rm -i --network "$RED" -e PGURL="$URL_VPS" "$IMAGEN_PG" \
    sh -c 'psql "$PGURL" -v ON_ERROR_STOP=1 -q "$@"' -- "$@"
}

psql_supabase() {
  docker run --rm -i -e PGURL="$(url_supabase)" "$IMAGEN_PG" \
    sh -c 'psql "$PGURL" -v ON_ERROR_STOP=1 -q "$@"' -- "$@"
}

preparar_vps() {
  if [[ -z "$PG_VPS_PASSWORD" ]]; then
    echo "ERROR: falta PG_VPS_PASSWORD en .env" >&2; exit 1
  fi
  paso "Levantando el Postgres del VPS..."
  "${COMPOSE[@]}" up -d postgres
  for _ in $(seq 1 30); do
    "${COMPOSE[@]}" exec -T postgres pg_isready -U licitapro -d licitapro >/dev/null 2>&1 && break
    sleep 2
  done
  RED="$(docker network ls --format '{{.Name}}' | grep -m1 '^licitapro_default$')"
}

copiar() {
  local dump
  dump="$TRABAJO/supabase-$(date +%Y%m%d-%H%M%S).sql"
  paso "Volcando el esquema public de Supabase..."
  docker run --rm -e PGURL="$(url_supabase)" "$IMAGEN_PG" \
    sh -c 'pg_dump "$PGURL" --schema=public --no-owner --no-privileges' > "$dump"
  chmod 600 "$dump"
  if ! grep -q "CREATE TABLE public.licitaciones" "$dump"; then
    echo "ERROR: el volcado no trae licitaciones: base equivocada o vacia." >&2; exit 1
  fi
  echo "  $(du -h "$dump" | cut -f1) en $dump"

  paso "Restaurando en el Postgres del VPS (desde cero)..."
  psql_vps -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public AUTHORIZATION licitapro;"
  # El volcado trae `CREATE SCHEMA public`: ya existe, asi que esa linea se
  # salta. pg_trgm vive en public en Supabase; se instala antes por si el
  # volcado no la trae (ningun indice la usa hoy, pero el codigo podria).
  psql_vps -c "CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;"
  grep -v '^CREATE SCHEMA public;$' "$dump" | grep -v '^CREATE EXTENSION' | psql_vps
  # `rls_auto_enable` es un ayudante de Supabase sin uso aqui. RLS se apaga:
  # en Supabase estaba encendido sin ninguna politica, y aqui no hay roles
  # anon ni authenticated contra los que protegerse.
  psql_vps <<'SQL'
DROP FUNCTION IF EXISTS public.rls_auto_enable() CASCADE;
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t.tablename);
  END LOOP;
END $$;
ANALYZE;
SQL
}

CONTEO="SELECT string_agg(format('%s=%s', tablename, (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM public.%I', tablename), false, true, '')))[1]::text), ' ' ORDER BY tablename) FROM pg_tables WHERE schemaname='public';"

comparar() {
  paso "Comparando filas por tabla (Supabase vs VPS)..."
  local a b
  a="$(psql_supabase -At -c "$CONTEO")"
  b="$(psql_vps -At -c "$CONTEO")"
  if [[ "$a" != "$b" ]]; then
    echo "DIFERENCIAS (izquierda Supabase, derecha VPS):" >&2
    diff <(tr ' ' '\n' <<<"$a") <(tr ' ' '\n' <<<"$b") >&2 || true
    return 1
  fi
  echo "  Iguales: $(tr ' ' '\n' <<<"$a" | wc -l) tablas."
  echo "  alembic: $(psql_vps -At -c 'SELECT version_num FROM alembic_version')"
}

case "$MODO" in
  ensayo)
    preparar_vps
    copiar
    # En el ensayo la aplicacion sigue escribiendo en Supabase mientras se
    # copia, asi que alguna tabla viva (scraping_log, licitaciones) puede
    # diferir en unas filas. En el corte, con todo parado, tiene que cuadrar.
    comparar || echo "(En ensayo es normal alguna diferencia pequena en tablas que se escriben solas.)"
    paso "Ensayo terminado. La aplicacion sigue usando Supabase; nada cambio para los usuarios."
    ;;

  corte)
    if [[ "$(leer_env DATABASE_URL)" != *supabase* ]]; then
      echo "ERROR: el .env ya no apunta a Supabase: el corte ya se hizo." >&2; exit 1
    fi
    preparar_vps
    paso "Parando la aplicacion (web, radar, prep, win)..."
    "${COMPOSE[@]}" stop web radar prep win
    copiar
    if ! comparar; then
      echo "ERROR: la copia no cuadra. Se levanta otra vez contra Supabase." >&2
      "${COMPOSE[@]}" start web radar prep win
      exit 1
    fi
    paso "Cambiando DATABASE_URL en el .env (copia en $TRABAJO/env.antes-del-corte)..."
    cp -p .env "$TRABAJO/env.antes-del-corte"
    chmod 600 "$TRABAJO/env.antes-del-corte"
    tmp="$(mktemp)"
    # POSTGRES_HOST tambien cambia: shared/db.py decide el TLS mirando ese
    # host, y si sigue diciendo Supabase pide TLS a un Postgres que no lo tiene.
    grep -vE '^(DATABASE_URL|POSTGRES_ZONA_SESION|POSTGRES_HOST|POSTGRES_PORT)=' .env > "$tmp"
    { echo "DATABASE_URL=$URL_VPS_ENV"; echo "POSTGRES_ZONA_SESION=UTC"
      echo "POSTGRES_HOST=postgres"; echo "POSTGRES_PORT=5432"; } >> "$tmp"
    cat "$tmp" > .env && rm -f "$tmp"
    paso "Levantando la aplicacion contra el VPS..."
    "${COMPOSE[@]}" up -d --no-build migraciones web radar prep win
    sleep 15
    if docker exec licitapro-web-1 curl -fsS -m 10 http://localhost:8200/salud >/dev/null; then
      paso "Corte hecho. /salud responde."
    else
      echo "ERROR: /salud no responde. Para volver a Supabase: $0 volver" >&2; exit 1
    fi
    ;;

  volver)
    if [[ ! -f "$TRABAJO/env.antes-del-corte" ]]; then
      echo "ERROR: no hay copia del .env de antes del corte." >&2; exit 1
    fi
    paso "Volviendo a Supabase..."
    cp -p .env "$TRABAJO/env.despues-del-corte"
    cat "$TRABAJO/env.antes-del-corte" > .env
    "${COMPOSE[@]}" up -d --no-build --force-recreate web radar prep win
    paso "De vuelta en Supabase. Lo escrito en el VPS tras el corte no se paso a Supabase."
    ;;

  *)
    echo "Uso: $0 ensayo | corte | volver" >&2
    exit 1
    ;;
esac
