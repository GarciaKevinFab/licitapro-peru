#!/usr/bin/env bash
# Copia de seguridad de LicitaPro: base de datos y archivos subidos.
#
# QUE PROTEGE
#
#   La base tiene las cuentas, las empresas, las propuestas y los contratos de
#   todos los clientes. El volumen datos_expedientes tiene sus logos, firmas y
#   sellos escaneados. Perder cualquiera de los dos no se arregla con dinero:
#   son datos que el cliente nos confio y que no tiene por que volver a subir.
#
# ESTE SCRIPT ESTABA ROTO PARA PRODUCCION, Y DE LA PEOR MANERA
#
#   Volcaba con `docker compose exec -T postgres pg_dump`. En produccion NO HAY
#   servicio postgres: la base es Supabase y viene entera en DATABASE_URL. El
#   compose de produccion lo dice en su cabecera. Es decir, este respaldo no
#   habia fallado nunca porque nunca se habia programado; el dia que se
#   programara habria fallado en la primera pasada.
#
#   Ahora vuelca contra DATABASE_URL, que es donde esta el dato de verdad.
#
# EL PUERTO IMPORTA: 6543 NO SIRVE PARA VOLCAR
#
#   DATABASE_URL apunta al pooler en modo TRANSACCION (6543), que es lo
#   correcto para la aplicacion. pg_dump no funciona ahi: necesita mantener una
#   sesion, con sus sentencias preparadas y su snapshot, y el pooler de
#   transaccion reparte cada sentencia por una conexion distinta.
#
#   El mismo host en el 5432 es el pooler en modo SESION, que si lo aguanta y
#   sigue siendo IPv4. Por eso aqui se cambia el puerto y no se pide otra URL:
#   una segunda variable que hay que acordarse de actualizar acaba apuntando a
#   la base de hace dos migraciones.
#
# COMO SE PROGRAMA (en el cron del HOST, no dentro de compose)
#
#   Un respaldo que vive dentro del mismo compose se pierde con el compose.
#
#     crontab -e
#     0 3 * * * /ruta/al/proyecto/tools/respaldar.sh /respaldos >> /var/log/licitapro-respaldo.log 2>&1
#
# COMO SE RESTAURA (probarlo ANTES de necesitarlo)
#
#   gunzip -c licitapro-AAAAMMDD-HHMMSS.sql.gz | psql "$DATABASE_URL_SESION"
#
#   docker run --rm -v licitapro-peru_datos_expedientes:/destino \
#       -v "$PWD":/origen alpine \
#       tar xzf /origen/firmas-AAAAMMDD-HHMMSS.tar.gz -C /destino
#
#   Un respaldo que nunca se restauro es una suposicion, no una copia.

set -euo pipefail

DESTINO="${1:-}"
if [[ -z "$DESTINO" ]]; then
  echo "Uso: $0 <directorio-destino> [dias-a-conservar]" >&2
  exit 1
fi
DIAS_CONSERVAR="${2:-14}"

cd "$(dirname "$0")/.."

# ─── Leer el .env, NO ejecutarlo ─────────────────────────────────────────────
#
#   El cron arranca sin el entorno de la sesion. Sin leer el .env, DATABASE_URL
#   llega vacia y el volcado se hace contra nada -- el mismo modo de fallo que
#   ya nos mordio con la tarea programada del puente.
#
#   Pero `. ./.env` no lee el archivo: lo EJECUTA. Un valor con espacios --una
#   contrasena de aplicacion, por ejemplo-- se parte, y bash intenta correr el
#   segundo trozo como si fuera un comando:
#
#     ./.env: line 179: M: command not found
#
#   Paso en el VPS, con SMTP_PASSWORD. docker-compose lee ese mismo archivo sin
#   quejarse porque su formato NO es shell, asi que el .env nunca tuvo por que
#   ser codigo valido. Ese es justo el malentendido.
#
#   Y hay algo peor que el fallo: ejecutar el archivo de configuracion
#   significa que cualquier cosa escrita ahi corre como root cada madrugada.
#
#   Se extraen solo las cinco variables que hacen falta, literales.
leer_env() {
  local v
  [[ -f .env ]] || return 0
  v="$(sed -n "s/^$1=//p" .env | head -1)"
  # Las comillas envolventes son del formato, no del valor.
  v="${v%\"}"; v="${v#\"}"
  v="${v%'}"; v="${v#'}"
  printf '%s' "$v"
}

: "${DATABASE_URL:=$(leer_env DATABASE_URL)}"
: "${RADAR_BOT_TOKEN:=$(leer_env RADAR_BOT_TOKEN)}"
: "${TELEGRAM_ADMIN_ID:=$(leer_env TELEGRAM_ADMIN_ID)}"
: "${RESPALDO_REMOTO:=$(leer_env RESPALDO_REMOTO)}"
: "${IMAGEN_PG:=$(leer_env IMAGEN_PG)}"

# ─── Aviso de fallo ──────────────────────────────────────────────────────────
# Un respaldo que falla en silencio es peor que no tener respaldo: da por
# cubierto un riesgo que sigue abierto. Cualquier salida distinta de cero
# manda un mensaje antes de morir.
avisar_fallo() {
  local codigo=$?
  [[ $codigo -eq 0 ]] && return 0
  echo "[$(date '+%F %T')] FALLO con codigo $codigo" >&2
  if [[ -n "${RADAR_BOT_TOKEN:-}" && -n "${TELEGRAM_ADMIN_ID:-}" ]]; then
    curl -s -m 20 -o /dev/null \
      "https://api.telegram.org/bot${RADAR_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_ADMIN_ID}" \
      --data-urlencode "text=🛑 El respaldo de LicitaPro fallo (codigo $codigo). Sin copia de hoy. Revisa /var/log/licitapro-respaldo.log en el VPS." || true
  fi
}
trap avisar_fallo EXIT

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: DATABASE_URL vacia. Es donde vive el dato de los clientes." >&2
  exit 1
fi

# 6543 (transaccion) -> 5432 (sesion). Si ya viene en 5432 se queda igual.
URL_VOLCADO="${DATABASE_URL/:6543/:5432}"

# pg_dump se niega a volcar un servidor MAS NUEVO que el. Se usa una imagen por
# encima de la version de Supabase a proposito: al reves falla, y falla con un
# mensaje que no dice que el problema es la version.
IMAGEN_PG="${IMAGEN_PG:-postgres:17-alpine}"   # Supabase corre 17.6

SELLO="$(date +%Y%m%d-%H%M%S)"
DUMP="$DESTINO/licitapro-$SELLO.sql.gz"
FIRMAS="$DESTINO/firmas-$SELLO.tar.gz"

mkdir -p "$DESTINO"

echo "[$(date '+%F %T')] Volcando la base desde Supabase..."
# La URL viaja por el entorno del contenedor y se expande DENTRO: los
# argumentos de un proceso los ve cualquiera que liste procesos, y ahi va la
# contrasena de la base.
#
# Solo el esquema `public`: auth, storage y las demas son de Supabase, no las
# gestionamos nosotros y restaurarlas encima romperia su instalacion.
docker run --rm -e PGURL="$URL_VOLCADO" "$IMAGEN_PG" \
  sh -c 'pg_dump "$PGURL" --schema=public --clean --if-exists --no-owner --no-privileges' \
  | gzip -9 > "$DUMP"

# pg_dump puede fallar y aun asi dejar un .gz valido pero vacio, porque gzip
# comprime la nada sin protestar. Se comprueba el tamano: un volcado real de
# este esquema no baja de unos pocos kilobytes.
TAM=$(wc -c < "$DUMP")
if [[ "$TAM" -lt 2048 ]]; then
  echo "ERROR: el volcado pesa $TAM bytes. Se borra para no dar por buena una copia vacia." >&2
  rm -f "$DUMP"
  exit 1
fi

# Y que ademas contenga el esquema: un volcado de una base VACIA pesa mas de 2
# KB y pasaria la prueba de arriba tan campante.
# `grep -q` sale en cuanto acierta, y eso le manda un SIGPIPE a `gunzip`. Con
# `pipefail` la tuberia devolveria 141 aun habiendo encontrado la tabla, y este
# guardia borraria un respaldo BUENO. `grep -c` lee hasta el final: sin SIGPIPE.
CUENTA_TABLA="$(gunzip -c "$DUMP" | grep -c "CREATE TABLE public.licitaciones" || true)"
if [[ "$CUENTA_TABLA" -eq 0 ]]; then
  echo "ERROR: el volcado no trae la tabla licitaciones. Base equivocada o vacia." >&2
  rm -f "$DUMP"
  exit 1
fi

echo "[$(date '+%F %T')] Copiando logos y firmas..."
# Se lee el volumen desde un contenedor efimero: asi funciona aunque los
# servicios de la aplicacion esten parados.
VOLUMEN="$(docker volume ls --format '{{.Name}}' | grep -m1 'datos_expedientes' || true)"
if [[ -n "$VOLUMEN" ]]; then
  docker run --rm -v "$VOLUMEN":/datos:ro -v "$DESTINO":/salida alpine \
    tar czf "/salida/$(basename "$FIRMAS")" -C /datos . 2>/dev/null
else
  echo "AVISO: no se encontro el volumen datos_expedientes; se omiten los archivos." >&2
fi

# ─── La otra mitad del trabajo: sacarlo del servidor ─────────────────────────
# Una copia en el mismo disco que la base protege contra un borrado, no contra
# el disco. Con RESPALDO_REMOTO puesto (un destino de rclone, p.ej. "r2:licitapro")
# se sube; sin el, se avisa en cada pasada para que no se olvide.
if [[ -n "${RESPALDO_REMOTO:-}" ]] && command -v rclone >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] Subiendo a $RESPALDO_REMOTO..."
  rclone copy "$DUMP" "$RESPALDO_REMOTO" --no-traverse
  [[ -f "$FIRMAS" ]] && rclone copy "$FIRMAS" "$RESPALDO_REMOTO" --no-traverse
  echo "  Subido."
else
  echo "AVISO: RESPALDO_REMOTO sin configurar. La copia se queda en este mismo" >&2
  echo "       servidor, que es justo lo que no protege contra perder el servidor." >&2
fi

echo "[$(date '+%F %T')] Retirando copias de mas de $DIAS_CONSERVAR dias..."
find "$DESTINO" -name 'licitapro-*.sql.gz' -mtime "+$DIAS_CONSERVAR" -delete
find "$DESTINO" -name 'firmas-*.tar.gz'    -mtime "+$DIAS_CONSERVAR" -delete

echo "[$(date '+%F %T')] Listo:"
# Sin el `|| true`, un `ls` sobre un firmas-*.tar.gz que no se genero devuelve
# no-cero, `pipefail` lo propaga y `set -e` mata el script en la ultima linea:
# el respaldo habria ido bien y aun asi saldria el aviso de fallo.
ls -lh "$DUMP" "$FIRMAS" 2>/dev/null | sed 's/^/  /' || true
