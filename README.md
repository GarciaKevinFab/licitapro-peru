# 🇵🇪 LicitaPro Perú — Sistema Autónomo de Licitaciones

Sistema de 3 bots de Telegram para monitorear, preparar y gestionar licitaciones del Estado peruano.

## 🏗️ Arquitectura

```
🔍 LicitaRadar ──→ 📝 LicitaPrep ──→ 🏆 LicitaWin
   (Detectar)         (Preparar)        (Ganar/Cobrar)
       ↕                  ↕                  ↕
   ┌──────────── PostgreSQL ────────────────┐
   │  12 scrapers │ Knowledge Base │ Pagos  │
   └────────────────────────────────────────┘
```

## ⚡ Setup Rápido (5 minutos)

### 1. Crear los 3 bots en Telegram
Abre @BotFather en Telegram y crea 3 bots:
```
/newbot → LicitaRadar → @LicitaRadar_tuuser_bot  
/newbot → LicitaPrep  → @LicitaPrep_tuuser_bot
/newbot → LicitaWin   → @LicitaWin_tuuser_bot
```
Guarda los 3 tokens.

### 2. Obtener tu Telegram ID
Envía cualquier mensaje a @userinfobot y copia tu ID numérico.

### 3. Configurar variables de entorno
```bash
cp .env.example .env
nano .env  # Llena todos los campos marcados con ← LLENAR
```

### 4. Levantar todo con Docker
```bash
docker-compose up -d
```
Esto levanta: PostgreSQL + Redis + los 3 bots.

### 5. Verificar
Abre Telegram y envía `/start` a cada uno de tus 3 bots.

## 📁 Estructura del Proyecto

```
licitapro-peru/
├── docker-compose.yml          # Infraestructura
├── Dockerfile                  # Imagen Python para los bots
├── .env.example                # Template de configuración
├── requirements.txt            # Dependencias Python
├── shared/
│   ├── schema.sql              # 14 tablas PostgreSQL + datos iniciales
│   ├── db.py                   # Conexión y helpers de base de datos
│   └── config.py               # Configuración compartida
├── radar_bot/
│   ├── main.py                 # Bot 1: Detección y alertas
│   └── scrapers/
│       └── seace.py            # Scraper SEACE 3.0
├── prep_bot/
│   └── main.py                 # Bot 2: Preparación de propuestas
├── win_bot/
│   └── main.py                 # Bot 3: Post-adjudicación
├── templates/                  # Plantillas DOCX/PDF
└── data/                       # Bases descargadas, expedientes
```

## 🤖 Comandos de cada Bot

### 🔍 @LicitaRadarBot
| Comando | Función |
|---------|---------|
| `/hoy` | Licitaciones nuevas del día |
| `/buscar [keyword]` | Buscar en todas las fuentes |
| `/region add/remove [depto]` | Configurar departamentos |
| `/config` | Ver configuración actual |

### 📝 @LicitaPrepBot  
| Comando | Función |
|---------|---------|
| `/estado` | Propuestas en preparación |
| `/r [id] [respuesta]` | Responder pregunta del bot |
| `/datos` | Ver datos de tus empresas |
| `/aprobar [id]` | Generar expediente final |

### 🏆 @LicitaWinBot
| Comando | Función |
|---------|---------|
| `/contratos` | Contratos activos |
| `/plazos` | Próximas fechas límite |
| `/pagos` | Estado de pagos |
| `/ganar [prop_id] [monto]` | Registrar buena pro |
| `/resumen` | Dashboard general |

## 🔧 Desarrollo con Claude Code

Para expandir el sistema (más scrapers, generación de documentos, etc.),
copia el Mega Prompt de la pestaña "Prompt" del artifact interactivo
y pégalo en Claude Code. Contiene toda la especificación para generar
los módulos faltantes.

## 📊 Base de Datos

14 tablas incluyendo:
- `empresas` — Tus 4 empresas pre-configuradas
- `knowledge_base` — Lo que el bot aprende de ti (nunca re-pregunta)
- `licitaciones` — Todas las detectadas a nivel nacional
- `propuestas` — Propuestas en preparación
- `preguntas` — Preguntas pendientes del bot hacia ti
- `contratos` — Contratos ganados
- `plazos` — Timeline de fechas límite
- `pagos` — Seguimiento de cobros

## 🔐 Seguridad
- Las credenciales de SEACE/RNP se guardan SOLO en tu .env local
- Los bots corren en tu máquina (no en la nube)
- PostgreSQL solo escucha en localhost
- Los tokens de Telegram son privados por bot
