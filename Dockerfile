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

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# Dependencias en su propia capa: cambiar codigo no reinstala todo.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Usuario sin privilegios: si alguien escapa del proceso, no es root.
RUN useradd --create-home --shell /bin/bash licitapro \
 && mkdir -p /app/data/bases /app/data/expedientes \
 && chown -R licitapro:licitapro /app
USER licitapro

EXPOSE 8200
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8200/salud || exit 1

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8200"]
