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
# POR QUE UN SCRIPT DEL HOST Y NO UN SERVICIO MAS EN COMPOSE
#
#   Un respaldo que vive dentro del mismo compose se pierde con el compose. La
#   gracia es que la copia acabe FUERA: otro disco, otro servidor, otra cuenta.
#   Por eso esto se programa en el cron del host y se le pasa un destino.
#
# COMO SE PROGRAMA
#
#   crontab -e   y anadir, para las 3 de la madrugada:
#     0 3 * * * /ruta/al/proyecto/tools/respaldar.sh /respaldos >> /var/log/licitapro-respaldo.log 2>&1
#
#   Y despues -- esto es la mitad del trabajo -- copiar /respaldos a otro sitio.
#   Un respaldo en el mismo disco que la base protege contra un borrado, no
#   contra el disco.
#
# COMO SE RESTAURA (probarlo ANTES de necesitarlo)
#
#   gunzip -c licitapro-AAAAMMDD-HHMMSS.sql.gz | docker compose exec -T postgres \
#       psql -U licitapro -d licitapro
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
mkdir -p "$DESTINO"

# El sufijo lleva la hora porque puede correr mas de una vez al dia (antes de un
# despliegue, por ejemplo) y no queremos que la segunda pise a la primera.
SELLO="$(date +%Y%m%d-%H%M%S)"
DUMP="$DESTINO/licitapro-$SELLO.sql.gz"
FIRMAS="$DESTINO/firmas-$SELLO.tar.gz"

# La contrasena viaja por el entorno del contenedor, nunca en la linea de
# comandos: los argumentos son visibles para cualquiera que liste procesos.
USUARIO="${POSTGRES_USER:-licitapro}"
BASE="${POSTGRES_DB:-licitapro}"

echo "[$(date '+%F %T')] Volcando la base..."
docker compose exec -T postgres \
  pg_dump -U "$USUARIO" -d "$BASE" --clean --if-exists \
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

echo "[$(date '+%F %T')] Retirando copias de mas de $DIAS_CONSERVAR dias..."
find "$DESTINO" -name 'licitapro-*.sql.gz' -mtime "+$DIAS_CONSERVAR" -delete
find "$DESTINO" -name 'firmas-*.tar.gz'    -mtime "+$DIAS_CONSERVAR" -delete

echo "[$(date '+%F %T')] Listo:"
ls -lh "$DUMP" "$FIRMAS" 2>/dev/null | sed 's/^/  /'
echo
echo "  Recuerda: esto sigue estando en el mismo servidor. Copialo fuera."
