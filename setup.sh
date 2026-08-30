#!/bin/bash
# ============================================
# LicitaPro Perú — Setup Script
# Ejecutar: bash setup.sh
# ============================================

set -e

echo "🇵🇪 ================================"
echo "   LicitaPro Perú — Setup"
echo "================================"
echo ""

# 1. Verificar Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker no está instalado."
    echo "   Instala Docker Desktop: https://www.docker.com/products/docker-desktop/"
    exit 1
fi
echo "✅ Docker encontrado"

if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "❌ Docker Compose no está instalado."
    exit 1
fi
echo "✅ Docker Compose encontrado"

# 2. Crear .env si no existe
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "📝 Se creó el archivo .env"
    echo "   EDITA .env con tus credenciales antes de continuar."
    echo ""
    echo "   Necesitas:"
    echo "   1. Crear 3 bots en @BotFather en Telegram"
    echo "   2. Obtener tu Telegram ID en @userinfobot"
    echo "   3. (Opcional) API key de Anthropic para análisis IA"
    echo "   4. (Opcional) Gmail App Password para emails"
    echo ""
    echo "   Después ejecuta: bash setup.sh"
    exit 0
fi

# 3. Verificar que los tokens estén configurados
source .env

if [ -z "$RADAR_BOT_TOKEN" ]; then
    echo "❌ RADAR_BOT_TOKEN no configurado en .env"
    echo "   Crea un bot en @BotFather y pega el token"
    exit 1
fi

if [ -z "$TELEGRAM_ADMIN_ID" ]; then
    echo "❌ TELEGRAM_ADMIN_ID no configurado en .env"
    echo "   Envía un mensaje a @userinfobot para obtener tu ID"
    exit 1
fi

echo "✅ Configuración .env verificada"

# 4. Crear directorios de datos
mkdir -p data/postgres data/redis data/bases data/expedientes
echo "✅ Directorios creados"

# 5. Levantar PostgreSQL y Redis primero
echo ""
echo "🔧 Levantando PostgreSQL + Redis..."
docker compose up -d postgres redis
echo "⏳ Esperando que PostgreSQL esté listo..."
sleep 5

# Verificar que PostgreSQL está corriendo
until docker compose exec -T postgres pg_isready -U licitapro > /dev/null 2>&1; do
    echo "   Esperando PostgreSQL..."
    sleep 2
done
echo "✅ PostgreSQL listo"

# 6. Construir imagen de los bots
echo ""
echo "🔧 Construyendo imagen Docker de los bots..."
docker compose build radar_bot
echo "✅ Imagen construida"

# 7. Levantar todo
echo ""
echo "🚀 Levantando los 3 bots..."
docker compose up -d
echo ""

# 8. Verificar estado
echo "📊 Estado de los servicios:"
docker compose ps
echo ""

echo "🎉 ================================"
echo "   ¡LicitaPro Perú está corriendo!"
echo "================================"
echo ""
echo "Abre Telegram y envía /start a tus 3 bots:"
echo "  🔍 @LicitaRadarBot — Detección"
echo "  📝 @LicitaPrepBot  — Preparación"
echo "  🏆 @LicitaWinBot   — Post-ganar"
echo ""
echo "El primer scraping se ejecutará en 60 segundos."
echo ""
echo "Para ver logs: docker compose logs -f"
echo "Para detener:  docker compose down"
echo "Para reiniciar: docker compose restart"
