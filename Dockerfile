# Imagen unica para el panel web y los tres bots: mismo codigo, distinto
# comando. Mantener dos imagenes que instalan lo mismo solo garantiza que un
# dia diverjan.
FROM python:3.12-slim

# Zona horaria de Lima dentro del contenedor. El sistema guarda timestamps
# naive en hora local; con el contenedor en UTC un token de Telegram nace
# caducado y una licitacion se da por vencida cinco horas antes de cerrar.
ENV TZ=America/Lima \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# tesseract-ocr + spa: la constancia del RNP llega como PDF sin texto (fuente
# incrustada sin tabla de caracteres) y solo se puede leer con OCR. Ver
# shared/constancia_rnp.py.
#
# libreoffice-writer-nogui + fonts-liberation: los anexos de la Municipalidad
# de Tambopata llegan en Word y se presentan en PDF (shared/anexos_mpt.py,
# shared/pdf_firma.docx_a_pdf). Liberation tiene las medidas de Times New
# Roman y Arial, que son las de esos formatos: sin ella LibreOffice sustituye
# la letra y cambian los saltos de linea del documento de la entidad.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl tesseract-ocr tesseract-ocr-spa \
      libreoffice-writer-nogui fonts-liberation \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# Dependencias en su propia capa: cambiar codigo no reinstala todo.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Lo que NO entra (el .env con las claves, data/ con los documentos de los
# clientes, .git, las pruebas) lo decide .dockerignore. Leerlo antes de tocar
# esta linea.
COPY . .

# Usuario sin privilegios: si alguien escapa del proceso, no es root.
RUN useradd --create-home --shell /bin/bash licitapro \
 && mkdir -p /app/data/bases /app/data/expedientes \
 && chown -R licitapro:licitapro /app
USER licitapro

EXPOSE 8200
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8200/salud || exit 1

# --proxy-headers hace que uvicorn lea X-Forwarded-Proto y X-Forwarded-For,
# pero SOLO de las conexiones que vienen de una IP de FORWARDED_ALLOW_IPS.
#
# AQUI HUBO --forwarded-allow-ips=*, Y ERA UN AGUJERO
#
#   El argumento era "este contenedor no asoma a internet". Cierto, pero da
#   igual: la cabecera no la escribe el proxy, la ARRASTRA. Cloudflare anade
#   al final de X-Forwarded-For lo que ve y deja delante lo que mandara el
#   cliente. Con `*` uvicorn se fiaba de toda la cadena y tomaba la primera
#   IP, la inventada: el freno a los intentos de acceso (web/auth.py) se
#   saltaba con una IP nueva por intento, o se usaba para bloquear la de otro.
#
# POR QUE 127.0.0.1
#
#   En produccion no hay Caddy: cloudflared corre en el HOST y entra por
#   127.0.0.1:8200, y docker-proxy le pasa la conexion al contenedor desde la
#   puerta de enlace de la red de docker. Ninguna de esas IPs es 127.0.0.1, asi
#   que uvicorn no se fia de nadie y `request.client` es esa puerta de enlace.
#   No importa: la IP real la da CF-Connecting-IP, que Cloudflare pisa siempre
#   y nadie de fuera puede elegir (web/limites.ip_del_cliente). Y el esquema
#   que ve la app pasa a ser http, que tampoco importa: ninguna URL absoluta
#   sale de la peticion -- salen de LICITAPRO_DOMINIO (web/app._base_url).
#
#   Va como variable de entorno, que uvicorn lee por su cuenta, y no fijo en el
#   CMD: el despliegue con Caddy del compose (Caddyfile) tiene el proxy en otro
#   contenedor y ahi hay que poner su IP o su subred. Se cambia en el compose,
#   sin reconstruir la imagen. Nunca `*`.
ENV FORWARDED_ALLOW_IPS=127.0.0.1
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8200", \
     "--proxy-headers"]
