# LICITAPRO PERÚ — MEGA PROMPT COMPLETO
# Sistema Autónomo de Licitaciones del Estado Peruano
# ============================================================
# Este prompt contiene TODA la especificación del sistema.
# Úsalo con Claude Code para generar/expandir el proyecto.
# ============================================================

## VISIÓN GENERAL

LicitaPro Perú es un sistema de 3 bots de Telegram que automatiza el ciclo COMPLETO de licitaciones del Estado peruano: detectar → analizar → preparar → presentar → ganar → cobrar.

El sistema reemplaza herramientas como LicitaLAB (S/240-325/mes) con una solución gratuita, self-hosted, más completa y personalizada.

### Diferenciadores clave vs LicitaLAB:
- Auto-llenado COMPLETO de todos los anexos (LicitaLAB no tiene)
- Bot que PREGUNTA lo que falta y APRENDE para siempre (LicitaLAB no tiene)
- Generación de propuesta técnica con IA (LicitaLAB no tiene)
- Expediente ZIP listo para subir a SEACE (LicitaLAB no tiene)
- Tracking post-adjudicación de plazos y pagos (LicitaLAB no tiene)
- Notificación de buena pro por Telegram + email (LicitaLAB no tiene)
- Generador de facturas y actas de conformidad (LicitaLAB no tiene)
- Multi-empresa (4+ empresas configuradas)
- 12 fuentes de datos vs 2 de LicitaLAB
- GRATIS (self-hosted en PC local)

## ARQUITECTURA DE 3 BOTS

```
┌─────────────────────────────────────────────────────────────┐
│                    USUARIO (Telegram)                        │
│     📱 Recibe alertas, responde preguntas, aprueba          │
└────────────┬──────────────┬──────────────┬──────────────────┘
             │              │              │
    ┌────────▼────────┐ ┌──▼──────────┐ ┌─▼───────────────┐
    │ 🔍 LicitaRadar  │ │📝 LicitaPrep│ │ 🏆 LicitaWin    │
    │ @LicitaRadarBot │ │@LicitaPrep  │ │ @LicitaWinBot   │
    │                 │ │Bot          │ │                  │
    │ • 12 scrapers   │ │• Auto-fill  │ │• Detecta buena   │
    │ • Filtros multi │ │• Preguntador│ │  pro             │
    │ • Score IA      │ │• Claude API │ │• Timeline plazos │
    │ • Alertas       │ │• Doc gen    │ │• Track pagos     │
    │ • /licitar →    │─│• ZIP builder│ │• Email + Telegram│
    └────────┬────────┘ └──┬──────────┘ └─┬───────────────┘
             │              │              │
    ┌────────▼──────────────▼──────────────▼──────────────────┐
    │                   PostgreSQL (14 tablas)                  │
    │  empresas │ knowledge_base │ licitaciones │ propuestas   │
    │  preguntas │ contratos │ plazos │ pagos │ equipo_tecnico │
    │  experiencia │ historico_precios │ user_config │ logs    │
    └──────────────────────┬──────────────────────────────────┘
                           │
    ┌──────────────────────▼──────────────────────────────────┐
    │                   N8N (Workflows)                         │
    │  CRON 60min → Scrapers │ CRON 8AM → Resumen diario      │
    │  CRON 30min → Check wins │ CRON 6h → Check plazos       │
    └─────────────────────────────────────────────────────────┘
```

### Flujo completo de una licitación:

1. **CRON** activa scrapers cada 60 min
2. **Orchestrator** ejecuta 12 scrapers (si uno falla, los otros siguen)
3. Cada licitación nueva se guarda en DB con dedup
4. **Filtro multi-criterio**: región + entidad + keyword + monto
5. **Score de viabilidad** (match con perfil de empresa)
6. **🔍 LicitaRadar** envía alerta por Telegram con botones
7. Usuario presiona **"🚀 Licitar"** → se crea propuesta en DB
8. **📝 LicitaPrep** detecta propuesta nueva (poll cada 30s)
9. **Auto-fill**: busca datos en knowledge_base → tabla empresas → si no encuentra → PREGUNTA
10. Usuario responde por Telegram → se guarda en KB (nunca re-pregunta)
11. Claude API genera propuesta técnica adaptada a las bases
12. Calculadora estima precio competitivo con histórico
13. Se genera expediente ZIP completo con todos los anexos
14. Usuario revisa, aprueba, y sube manualmente a SEACE (requiere firma digital)
15. **🏆 LicitaWin** monitorea SEACE para detectar buena pro
16. Si gana → Telegram + email + crea timeline de plazos automáticamente
17. Alerta 3 días antes de cada fecha límite
18. Tracking de pagos: pendientes → facturados → cobrados

## STACK TÉCNICO

```
Backend:        Python 3.11+
Bots:           python-telegram-bot 21.6
DB:             PostgreSQL 16 (asyncpg)
Cache:          Redis 7 (dedup + rate limiting)
Scraping:       httpx + BeautifulSoup4 + lxml
IA:             Anthropic Claude API (claude-sonnet-4-20250514)
Documentos:     python-docx + openpyxl + ReportLab
Email:          aiosmtplib (Gmail/SendGrid)
Scheduling:     APScheduler (dentro de cada bot) + N8N (workflows)
Orchestration:  N8N (http://localhost:5678)
API interna:    FastAPI (puerto 8100, para que N8N llame scrapers)
Containers:     Docker Compose
Infraestructura: Local en PC (kevin-1, RTX 4060, accesible via Tailscale)
```

## ESTRUCTURA COMPLETA DEL PROYECTO

```
licitapro-peru/
├── docker-compose.yml              # PostgreSQL + Redis + 3 bots + API
├── Dockerfile                      # Imagen Python para los bots
├── .env.example                    # Template de configuración
├── .env                            # Credenciales (NO commitear)
├── requirements.txt                # Dependencias Python
├── setup.sh                        # Script de setup automático
├── README.md                       # Documentación
├── CLAUDE_CODE_PROMPT.md           # Este archivo
│
├── shared/                         # Módulos compartidos por los 3 bots
│   ├── __init__.py
│   ├── schema.sql                  # ✅ EXISTE — 14 tablas + datos iniciales
│   ├── db.py                       # ✅ EXISTE — Pool asyncpg + helpers CRUD
│   ├── config.py                   # ✅ EXISTE — Config, constantes, formatters
│   ├── analyzer.py                 # 🔨 CREAR — Análisis IA con Claude API
│   ├── email_sender.py             # 🔨 CREAR — Envío SMTP (Gmail/SendGrid)
│   └── api_server.py              # 🔨 CREAR — FastAPI para N8N
│
├── radar_bot/                      # Bot 1: Detección y alertas
│   ├── __init__.py
│   ├── main.py                     # ✅ EXISTE — Bot principal con handlers
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── search.py               # 🔨 CREAR — /buscar avanzado con filtros
│   │   ├── daily.py                # 🔨 CREAR — /hoy /semana con resúmenes
│   │   ├── config_handler.py       # 🔨 CREAR — /config /region /keyword /monto
│   │   └── stats.py                # 🔨 CREAR — /stats con métricas
│   └── scrapers/
│       ├── __init__.py
│       ├── orchestrator.py         # ✅ EXISTE — Ejecuta todos los scrapers
│       ├── seace.py                # ✅ EXISTE — SEACE 3.0 buscador público
│       ├── ocds_api.py             # ✅ EXISTE — API OCDS contrataciones abiertas
│       ├── contratos_menores.py    # ✅ EXISTE — ≤8 UIT (prod6.seace.gob.pe)
│       ├── peru_compras.py         # 🔨 CREAR — Catálogos electrónicos CEAM
│       ├── conosce.py              # 🔨 CREAR — Datos abiertos OSCE (bulk)
│       ├── datos_abiertos.py       # 🔨 CREAR — datosabiertos.gob.pe API
│       ├── open_contracting.py     # 🔨 CREAR — data.open-contracting.org
│       ├── transparencia.py        # 🔨 CREAR — PAC entidades
│       ├── poder_judicial.py       # 🔨 CREAR — sap.pj.gob.pe (Playwright)
│       ├── gore_portals.py         # 🔨 CREAR — Portales regionales cotización
│       ├── essalud.py              # 🔨 CREAR — EsSalud contrataciones
│       └── sbs.py                  # 🔨 CREAR — SBS transparencia
│
├── prep_bot/                       # Bot 2: Preparación de propuestas
│   ├── __init__.py
│   ├── main.py                     # ✅ EXISTE — Bot con questioner + auto-fill
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── empresa.py              # 🔨 CREAR — /datos /editar_empresa /equipo
│   │   └── revision.py             # 🔨 CREAR — /revisar /corregir /descargar
│   └── autofill/
│       ├── __init__.py
│       ├── engine.py               # ✅ EXISTE — Auto-fill con KB lookup
│       ├── annexes.py              # 🔨 CREAR — Generador de TODOS los anexos
│       ├── proposal_generator.py   # 🔨 CREAR — Propuesta técnica con Claude API
│       ├── pricing_calculator.py   # 🔨 CREAR — Precio competitivo con histórico
│       ├── zip_builder.py          # 🔨 CREAR — Expediente ZIP completo
│       └── validator.py            # 🔨 CREAR — Validador de completitud
│
├── win_bot/                        # Bot 3: Post-adjudicación
│   ├── __init__.py
│   ├── main.py                     # ✅ EXISTE — Bot con plazos y pagos
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── payments.py             # 🔨 CREAR — /factura /pago detallado
│   │   └── documents.py            # 🔨 CREAR — /conformidad /factura gen
│   ├── monitor.py                  # 🔨 CREAR — Auto-detectar buena pro en SEACE
│   ├── timeline.py                 # 🔨 CREAR — Calculador inteligente de plazos
│   ├── invoice_generator.py        # 🔨 CREAR — Generador de facturas
│   └── conformity_generator.py     # 🔨 CREAR — Actas de conformidad
│
├── n8n-workflows/                  # Workflows para importar en N8N
│   ├── scraping-pipeline.json      # ✅ EXISTE — CRON 60min scrapers
│   ├── daily-summary.json          # ✅ EXISTE — CRON 8AM resumen
│   ├── win-checker.json            # 🔨 CREAR — CRON 30min check buena pro
│   └── deadline-alerts.json        # 🔨 CREAR — CRON 6h alertas plazos
│
├── templates/                      # Plantillas DOCX/PDF
│   ├── carta_presentacion.docx     # 🔨 CREAR — Template editable
│   ├── declaracion_jurada.docx     # 🔨 CREAR — Template DJ
│   ├── propuesta_tecnica.docx      # 🔨 CREAR — Template PT
│   ├── propuesta_economica.xlsx    # 🔨 CREAR — Template PE
│   ├── experiencia_postor.docx     # 🔨 CREAR — Anexo experiencia
│   ├── equipo_tecnico.docx         # 🔨 CREAR — Anexo equipo
│   ├── compromiso_personal.docx    # 🔨 CREAR — Carta compromiso
│   ├── pacto_integridad.docx       # 🔨 CREAR — Pacto anticorrupción
│   ├── acta_conformidad.docx       # 🔨 CREAR — Para post-entrega
│   ├── factura_template.docx       # 🔨 CREAR — Para cobro
│   └── email_buena_pro.html        # 🔨 CREAR — Template email
│
└── data/                           # Datos runtime
    ├── bases/                      # PDFs de bases descargadas
    └── expedientes/                # ZIPs de expedientes generados
```

## BASE DE DATOS — 14 TABLAS (Ya existe en shared/schema.sql)

### Tabla: empresas
```sql
-- Las 4 empresas de Kevin pre-cargadas:
-- 1. K & A Sistemas y Telecomunicaciones S.A.C. (RUC: 20490765507)
--    Rubros: tecnología, telecomunicaciones, sistemas, redes, videovigilancia
-- 2. Soluciones Informáticas MDD S.A.C.
--    Rubros: software, SISAC, consultoría TI, desarrollo web
-- 3. COMERCIAL Y MULTISERVICIOS SAN JOSE S.A.C. (RUC: 20610570420)
--    Rubros: multiservicios, suministros, equipos de cómputo
-- 4. CUBS FAM S.A.C. (RUC: 20602208070)
--    Rubros: servicios generales, mantenimiento, limpieza

Campos: id, razon_social, ruc, representante_legal, dni_representante,
cargo_representante, partida_registral, direccion, departamento, telefono,
email, web, rnp_numero, rnp_categoria, rnp_vigencia, rubros[], activa,
datos_extra(JSONB)
```

### Tabla: knowledge_base (EL CEREBRO DEL SISTEMA)
```sql
-- Cada vez que el bot pregunta algo y el usuario responde,
-- la respuesta se guarda aquí con (empresa_id, categoria, clave).
-- La próxima vez que se necesite ese dato, se usa directo sin preguntar.
-- 
-- Categorías: legal, financiero, experiencia, equipo, equipamiento, 
--             precios, operativo, general
--
-- Ejemplo: ("legal", "partida_registral") = "11234567 Zona MDD"
--          ("financiero", "cuenta_corriente") = "123-456-789"
--          ("precios", "margen_mantenimiento") = "20%"
--
-- Después de 10-15 licitaciones, el bot ya sabe TODO y no pregunta nada.

Campos: id, empresa_id(FK), categoria, clave, valor, tipo_dato,
fuente, usado_count, created_at, updated_at
UNIQUE(empresa_id, categoria, clave)
```

### Tabla: equipo_tecnico
```sql
-- Personal técnico disponible para asignar a licitaciones
-- Ejemplo: Ing. Carlos Mendoza, titulado, CIP 123456, 7 años

Campos: id, empresa_id(FK), nombre_completo, dni, titulo_profesional,
grado_academico, colegiatura, especialidad, anos_experiencia,
cargo_habitual, cv_resumen, disponible
```

### Tabla: experiencia
```sql
-- Contratos previos para demostrar experiencia
-- Pre-cargado: IESPP Allende (sistema académico) + SISAC (asistencia)

Campos: id, empresa_id(FK), entidad_contratante, objeto_contrato,
numero_contrato, monto, moneda, fecha_inicio, fecha_fin,
conformidad, conformidad_detalle, keywords[]
```

### Tabla: licitaciones
```sql
-- Todas las licitaciones detectadas a nivel nacional
Campos: id, fuente, tipo, nomenclatura, entidad, entidad_tipo,
entidad_ruc, objeto, descripcion, monto_referencial, moneda,
fecha_publicacion, fecha_cierre, fecha_buena_pro, estado,
departamento, provincia, distrito, url, bases_urls[],
bases_descargadas, bases_analisis(JSONB), anexos_identificados(JSONB),
score_viabilidad, score_detalle(JSONB), notificado, descartado
```

### Tabla: propuestas
```sql
-- Propuestas en preparación (link entre licitación y empresa)
Campos: id, licitacion_id(FK), empresa_id(FK), estado,
anexos_completados, anexos_totales, preguntas_pendientes,
propuesta_tecnica_path, propuesta_economica_path, precio_ofertado,
precio_sugerido_min, precio_sugerido_max, expediente_zip_path,
validacion(JSONB), notas, fecha_envio
-- Estados: iniciado → preguntas_pendientes → listo → enviado → adjudicado/perdido
```

### Tabla: preguntas
```sql
-- Preguntas del bot al usuario cuando falta información
-- Se vinculan a KB para guardar respuesta permanentemente
Campos: id, propuesta_id(FK), empresa_id(FK), campo_requerido,
pregunta, contexto, opciones[], respuesta, respondida,
guardada_en_kb, kb_categoria, kb_clave
```

### Tabla: contratos
```sql
-- Contratos ganados con seguimiento post-adjudicación
Campos: id, propuesta_id(FK), licitacion_id(FK), empresa_id(FK),
numero_contrato, monto_adjudicado, moneda, fecha_adjudicacion,
fecha_consentimiento, fecha_firma_contrato, fecha_inicio_ejecucion,
plazo_ejecucion_dias, fecha_entrega_final, estado,
carta_fianza_monto, carta_fianza_numero, carta_fianza_vencimiento,
email_notificado, notas
-- Estados: adjudicado → contrato_firmado → en_ejecucion → entregado → conformidad → pagado
```

### Tabla: plazos
```sql
-- Timeline de fechas límite por contrato (se crean automáticamente)
Campos: id, contrato_id(FK), tipo, descripcion, fecha_limite,
completado, fecha_completado, alerta_enviada, dias_antes_alerta, notas
-- Tipos: firma, fianza, entregable, conformidad, pago, garantia
```

### Tabla: pagos
```sql
-- Seguimiento de cobros
Campos: id, contrato_id(FK), concepto, monto, moneda,
numero_factura, fecha_factura, fecha_pago_esperada,
fecha_pago_real, estado, comprobante, notas
-- Estados: pendiente → facturado → pagado
```

### Tabla: user_config
```sql
-- Configuración del usuario (regiones, keywords, etc.)
Campos: user_id(PK=Telegram ID), regiones[], entidad_tipos[],
keywords[], keywords_excluir[], monto_min, monto_max,
empresa_default_id(FK), email_notificaciones,
horario_inicio, horario_fin, frecuencia_resumen, activo
```

### Tabla: historico_precios
```sql
-- Para estimar precios competitivos
Campos: id, objeto_keywords[], entidad, departamento,
tipo_procedimiento, monto_referencial, monto_adjudicado,
ratio_adj_ref, proveedor_ganador, proveedor_ruc,
num_postores, fecha, fuente
```

### Tabla: scraping_log
```sql
-- Monitoreo de cada ejecución de scraping
Campos: id, fuente, inicio, fin, registros_encontrados,
registros_nuevos, errores, error_detalle, status
```

## 12 FUENTES DE DATOS

### 1. SEACE 3.0 — Buscador Público (✅ EXISTE)
```
URL: https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml
Método: POST request con ViewState JSF + parseo HTML con BeautifulSoup
Frecuencia: cada 30 min
Scope: TODAS las licitaciones nacionales (LP, CP, AS, SIE, CD, CdP)
Prioridad: CRÍTICA
Notas: El buscador público usa JSF/PrimeFaces. El form submission
       requiere obtener el ViewState primero con GET, luego POST.
       La tabla de resultados tiene celdas con: nomenclatura, entidad,
       objeto, monto, fechas, estado. Parsear con Cheerio/BS4.
       Filtrar por departamento y keywords del usuario.
```

### 2. OCDS API — Contrataciones Abiertas (✅ EXISTE)
```
URL: https://contratacionesabiertas.osce.gob.pe/api
Método: REST API con paginación JSON
Frecuencia: cada 1 hora
Scope: Datos estructurados OCDS de SEACE V1/V2/V3
Prioridad: CRÍTICA
Notas: Endpoint principal: /ocds/releases?page=N&pageSize=50
       Cada release tiene: ocid, tender, buyer, awards
       Mapear campos OCDS a estructura interna.
       Formatos: JSON, CSV, XLSX disponibles.
       API pública sin autenticación.
```

### 3. Contratos Menores ≤8 UIT (✅ EXISTE)
```
URL: https://prod6.seace.gob.pe/buscador-publico/contrataciones
Método: React app — interceptar API calls internas (Network tab)
Frecuencia: cada 1 hora
Scope: Contrataciones rápidas hasta S/ 44,000 — alto volumen
Prioridad: ALTA
Notas: La UI es React, la data viene de una API REST interna.
       Usar Chrome DevTools Network tab para descubrir los endpoints.
       Probable: /api/v1/contrataciones o similar.
       Muy importante para MYPE: procesos rápidos, menos requisitos.
```

### 4. Perú Compras — Catálogos Electrónicos (🔨 CREAR)
```
URL: https://buscadorcatalogos.perucompras.gob.pe/
Método: Scraping del buscador CEAM + posible API
Frecuencia: cada 6 horas
Scope: Órdenes de compra de Acuerdos Marco (bienes estandarizados)
Prioridad: ALTA
Notas: Catálogos de equipos de cómputo, impresoras, mobiliario, etc.
       Si Kevin está en un catálogo como proveedor, recibe órdenes directas.
       Buscar: computadoras, impresoras, equipos multimedia, luminarias.
       datosabiertos.gob.pe tiene datasets de órdenes de compra en JSON.
```

### 5. CONOSCE — Datos Abiertos OSCE (🔨 CREAR)
```
URL: https://bi.seace.gob.pe/pentaho/api/repos/:public:portal:datosabiertos.html/content
Método: Descarga bulk de Excel/CSV
Frecuencia: semanal (domingo noche)
Scope: Histórico completo de convocatorias, adjudicaciones, proveedores
Prioridad: MEDIA
Notas: Acceso con userid=public&password=key (público).
       Datasets: convocatorias, adjudicaciones, proveedores, PAC.
       Se usa principalmente para alimentar historico_precios.
       Descargar Excel, parsear con openpyxl, insertar en DB.
```

### 6. datosabiertos.gob.pe (🔨 CREAR)
```
URL: https://www.datosabiertos.gob.pe/
Método: CKAN API REST (JSON)
Frecuencia: diaria
Scope: 354+ entidades, 14,000+ recursos de datos abiertos
Prioridad: MEDIA
Notas: API estándar CKAN: /api/3/action/package_search?q=contrataciones
       Buscar datasets relevantes de contrataciones.
       Datasets del OSCE: proveedores adjudicados, órdenes de compra,
       listado de entidades contratantes del SEACE v3.0.
```

### 7. data.open-contracting.org — OCDS Internacional (🔨 CREAR)
```
URL: https://data.open-contracting.org/es/publication/78
Método: Descarga OCDS JSONL comprimido
Frecuencia: semanal
Scope: Dataset completo de contrataciones de Perú por año
Prioridad: MEDIA
Notas: Archivos .jsonl.gz por año. Cada línea es un proceso completo.
       Útil para análisis de competidores y precios históricos.
       Descarga pesada, procesar en background.
```

### 8. Portal de Transparencia (🔨 CREAR)
```
URL: https://www.transparencia.gob.pe/
Método: Scraping HTML por entidad
Frecuencia: cada 6 horas
Scope: PAC (Plan Anual de Contrataciones) de cada entidad
Prioridad: BAJA
Notas: El PAC muestra las contrataciones PLANIFICADAS para el año.
       Permite anticipar licitaciones ANTES de que salgan en SEACE.
       URL por entidad: /contrataciones/pte_transparencia_pac.aspx?id_entidad=XXXXX
       Scraping de tabla HTML. El ID de entidad se obtiene del OSCE.
```

### 9. Poder Judicial — Portal de Abastecimiento (🔨 CREAR)
```
URL: https://sap.pj.gob.pe/portalabastecimiento-web/
Método: Playwright/Puppeteer (requiere JS rendering)
Frecuencia: cada 2 horas
Scope: Contrataciones propias del PJ en 34 distritos judiciales
Prioridad: MEDIA
Notas: App JavaScript, NO se puede scrapear con httpx solo.
       Necesita Playwright (headless browser).
       pip install playwright && playwright install chromium
       Extraer tabla de procesos activos.
       Si no funciona, usar SEACE como proxy filtrando entidad=PODER JUDICIAL.
```

### 10. Portales Regionales de Cotización (🔨 CREAR)
```
URLs conocidas:
  - Madre de Dios: http://cotizaciones.regionmadrededios.gob.pe/
  - (Descubrir más por región)
Método: Scraping HTML individual por GORE
Frecuencia: cada 2 horas
Scope: Cotizaciones directas que NO pasan por SEACE
Prioridad: ALTA (para cotizaciones locales)
Notas: Cada GORE puede tener o no su propio portal de cotizaciones.
       Implementar como clase base + subclases por región.
       Estas cotizaciones son de menor cuantía y proceso rápido.
       Algunas no tienen captcha ni login — acceso libre.
```

### 11. SBS — Superintendencia de Banca (🔨 CREAR)
```
URL: https://www.sbs.gob.pe/transparencia/bases-de-las-licitaciones-publicas
Método: Scraping HTML
Frecuencia: diaria
Scope: Licitaciones de la SBS
Prioridad: BAJA
Notas: La SBS tiene su propia sección de transparencia.
       Publicaciones de bases de LP, CP, AD.
       Scraping simple de tabla HTML.
```

### 12. EsSalud — Contrataciones Hospitalarias (🔨 CREAR)
```
URL: Via SEACE (filtro entidad) + portal propio
Método: SEACE filtered + scraping
Frecuencia: cada 2 horas
Scope: Compras de medicamentos, equipos médicos, servicios hospitalarios
Prioridad: MEDIA
Notas: EsSalud es uno de los mayores compradores del Estado.
       Primero: filtrar SEACE por entidad="ESSALUD".
       Segundo: verificar si tienen portal propio de contrataciones.
       Alto volumen de compras de bienes y servicios.
```

## MÓDULOS A CREAR

### shared/analyzer.py — Análisis IA con Claude API
```python
"""Analiza bases de licitación con Claude API."""

async def analyze_bases(pdf_text: str, empresa_data: dict) -> dict:
    """
    Envía texto de bases a Claude API y extrae info estructurada.
    
    Input: texto extraído del PDF de bases + datos de la empresa
    Output: {
        "requisitos_tecnicos": [...],
        "criterios_evaluacion": {"tecnico": [...], "economico": [...]},
        "documentos_obligatorios": [...],
        "plazos": {"ejecucion": "60 días", "garantia": "1 año"},
        "monto_referencial": 385000,
        "penalidades": [...],
        "personal_requerido": [{"cargo": "Jefe Proyecto", "requisitos": "..."}],
        "experiencia_minima": {"contratos": 2, "monto_acumulado": 200000},
        "score_viabilidad": 85,
        "score_detalle": {
            "experiencia": 90,
            "personal": 70,
            "monto": 95,
            "region": 100,
        },
        "recomendacion": "Alta viabilidad. Experiencia directa en IESPP..."
    }
    """
    # Prompt para Claude:
    SYSTEM_PROMPT = """Eres un experto en contrataciones del Estado peruano 
    con 20 años de experiencia. Analizas bases de licitación y extraes 
    información estructurada para evaluar si una empresa puede participar.
    
    Responde SOLAMENTE en JSON válido, sin markdown, sin explicaciones.
    Sé preciso con montos, plazos y requisitos."""
    
    USER_PROMPT = f"""Analiza estas bases de licitación:

    {pdf_text[:15000]}

    La empresa que quiere participar tiene este perfil:
    - Razón social: {empresa_data.get('razon_social')}
    - RUC: {empresa_data.get('ruc')}
    - Rubros: {empresa_data.get('rubros')}
    - Experiencia: {empresa_data.get('experiencia_resumen')}
    
    Extrae en JSON:
    {{
        "requisitos_tecnicos": ["lista de requisitos técnicos mínimos"],
        "criterios_evaluacion": {{
            "tecnico": [{{"criterio": "...", "puntaje_max": N}}],
            "economico": [{{"criterio": "...", "puntaje_max": N}}]
        }},
        "documentos_obligatorios": ["lista de documentos/anexos requeridos"],
        "plazos": {{"ejecucion_dias": N, "garantia": "...", "vigencia_oferta": "..."}},
        "monto_referencial": N,
        "penalidades": ["lista de penalidades"],
        "personal_requerido": [
            {{"cargo": "...", "titulo": "...", "experiencia_minima": "..."}}
        ],
        "experiencia_minima": {{
            "contratos_similares": N,
            "monto_acumulado_minimo": N,
            "antiguedad_maxima_anos": N
        }},
        "score_viabilidad": N,  // 0-100 basado en match con perfil empresa
        "score_detalle": {{
            "experiencia": N,
            "personal": N,
            "monto_rango": N,
            "ubicacion": N
        }},
        "recomendacion": "texto breve de recomendación"
    }}"""
    
    # Llamar a Claude API con anthropic SDK
    import anthropic
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": USER_PROMPT}],
    )
    
    import json
    return json.loads(response.content[0].text)


async def extract_pdf_text(pdf_path: str) -> str:
    """Extrae texto de un PDF de bases."""
    # Intentar con PyPDF2 primero, luego pdfplumber
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text
    except Exception:
        pass
    
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
            return text
    except Exception:
        return ""
```

### shared/api_server.py — FastAPI para N8N
```python
"""API HTTP interna que N8N llama para ejecutar scrapers y consultas."""
from fastapi import FastAPI
from radar_bot.scrapers.orchestrator import run_all_scrapers, format_scraping_report
from shared.db import get_licitaciones_nuevas, get_plazos_proximos, get_contratos_activos

app = FastAPI(title="LicitaPro API", version="1.0")

@app.post("/api/scrape")
async def trigger_scrape(sources: str = "all"):
    results = await run_all_scrapers()
    return results

@app.get("/api/daily-summary")
async def daily_summary():
    lics = await get_licitaciones_nuevas(limit=20)
    return {
        "date": str(date.today()),
        "nuevas": len(lics),
        "licitaciones": [dict(l) for l in lics],
    }

@app.post("/api/check-wins")
async def check_wins():
    # Verificar adjudicaciones en SEACE
    return {"checked": True, "wins": 0}

@app.get("/api/check-deadlines")
async def check_deadlines():
    plazos = await get_plazos_proximos(dias=7)
    return {"plazos_proximos": len(plazos), "detalle": [dict(p) for p in plazos]}

# Ejecutar: uvicorn shared.api_server:app --host 0.0.0.0 --port 8100
```

### prep_bot/autofill/annexes.py — Generador de TODOS los anexos
```python
"""Genera los 14+ anexos estándar de una licitación SEACE."""

ANEXOS_SEACE = [
    "carta_presentacion",      # Carta dirigida a la entidad
    "declaracion_jurada",      # DJ de no impedimento
    "declaracion_plazo",       # DJ de cumplimiento de plazo
    "propuesta_tecnica",       # Documento principal técnico
    "propuesta_economica",     # Precio desglosado
    "experiencia_postor",      # Contratos previos similares
    "equipo_tecnico",          # CVs del personal clave
    "compromiso_personal",     # Carta compromiso del equipo
    "equipamiento",            # Lista de equipos/herramientas
    "plan_trabajo",            # Cronograma y metodología
    "rnp_vigente",             # Constancia RNP (se descarga)
    "vigencia_poder",          # Vigencia de poder (se descarga)
    "pacto_integridad",        # Declaración anticorrupción
    "constancia_no_impedido",  # Declaración no impedido
]

async def generar_todos_los_anexos(propuesta_id, empresa_id, licitacion, datos_kb):
    """
    Genera TODOS los anexos para una licitación.
    - datos_kb: diccionario con todos los datos de knowledge_base
    - licitacion: datos de la licitación
    - Retorna lista de paths a documentos generados
    """
    documentos = []
    
    for anexo in ANEXOS_SEACE:
        try:
            path = await generar_anexo(anexo, empresa_id, licitacion, datos_kb)
            if path:
                documentos.append({"tipo": anexo, "path": path})
        except Exception as e:
            documentos.append({"tipo": anexo, "error": str(e)})
    
    return documentos
```

### prep_bot/autofill/proposal_generator.py — Propuesta técnica con IA
```python
"""Genera propuesta técnica personalizada usando Claude API."""

async def generar_propuesta_tecnica(bases_analisis, empresa, experiencias, equipo):
    """
    Genera una propuesta técnica completa en DOCX.
    
    Estructura:
    1. Presentación de la empresa
    2. Entendimiento del requerimiento
    3. Metodología propuesta
    4. Plan de trabajo y cronograma
    5. Equipo técnico asignado (con CVs resumidos)
    6. Experiencia relevante (contratos similares)
    7. Valor agregado y diferenciadores
    
    Usa Claude API para generar el contenido adaptado a las bases.
    El DOCX se genera con python-docx.
    """
    pass  # Implementar
```

### prep_bot/autofill/pricing_calculator.py — Precio competitivo
```python
"""Calcula precio económico competitivo basado en histórico."""

async def calcular_precio(licitacion, empresa_id):
    """
    1. Buscar en historico_precios licitaciones similares (keywords match)
    2. Calcular ratio promedio monto_adjudicado/monto_referencial
    3. Aplicar margen del usuario (knowledge_base: precios.margen_X)
    4. Retornar: {
        "precio_sugerido_min": X,
        "precio_sugerido_recomendado": Y,
        "precio_sugerido_max": Z,
        "referencias": [{"entidad": ..., "monto_adj": ..., "fecha": ...}],
        "ratio_promedio": 0.92,
        "num_referencias": 5,
    }
    """
    pass  # Implementar
```

### prep_bot/autofill/zip_builder.py — Expediente ZIP
```python
"""Compila todos los documentos en un ZIP listo para SEACE."""
import zipfile

async def build_expediente_zip(propuesta_id, documentos):
    """
    1. Listar todos los documentos generados para esta propuesta
    2. Organizar en carpetas:
       expediente/
       ├── 01_carta_presentacion.docx
       ├── 02_declaracion_jurada.docx
       ├── 03_propuesta_tecnica.docx
       ├── 04_propuesta_economica.xlsx
       ├── 05_experiencia.docx
       ├── 06_equipo_tecnico.docx
       ├── ...
       └── bases_referencia/
           └── bases_originales.pdf
    3. Crear ZIP
    4. Retornar path al ZIP
    """
    pass  # Implementar
```

### win_bot/monitor.py — Auto-detectar buena pro
```python
"""Monitorea SEACE para detectar si ganamos la buena pro."""

async def check_adjudicaciones():
    """
    Para cada propuesta con estado='enviado':
    1. Obtener la licitación de la DB
    2. Buscar en SEACE el estado actual del proceso
    3. Si el estado es 'adjudicado':
       a. Extraer RUC del ganador
       b. Comparar con nuestros RUCs (20490765507, 20610570420, 20602208070)
       c. Si coincide → registrar contrato + crear plazos + notificar
       d. Si no coincide → marcar como 'perdido' + notificar
    4. Usar la URL de la licitación guardada en DB para hacer GET
    """
    pass  # Implementar
```

### win_bot/invoice_generator.py — Generador de facturas
```python
"""Genera facturas electrónicas para cobro."""

async def generar_factura(contrato_id, concepto, monto):
    """
    Genera documento de factura en DOCX/PDF.
    Incluye: datos empresa, datos entidad, concepto, monto,
    IGV, total, número de contrato, fecha.
    
    Nota: No es factura electrónica SUNAT, es el documento
    de soporte para presentar a la entidad junto con la
    factura emitida por sistema de facturación.
    """
    pass  # Implementar
```

## COMANDOS DE TELEGRAM — ESPECIFICACIÓN COMPLETA

### 🔍 Bot 1: @LicitaRadarBot
```
/start          → Bienvenida + instrucciones + crea config en DB
/hoy            → Licitaciones nuevas del día (últimas 24h)
                   Formato: emoji_prioridad + tipo + entidad + objeto + monto + cierre
/semana         → Resumen semanal + las que cierran esta semana
/buscar [kw]    → Busca en DB por keyword en objeto y entidad
                   Acepta múltiples palabras: /buscar sistema académico
/region add X   → Agrega departamento al filtro
/region remove X → Quita departamento
/region list    → Muestra regiones activas
/keyword add X  → Agrega keyword de monitoreo
/keyword remove X → Quita keyword
/monto min X max Y → Configura rango de montos
/entidad [nombre] → Busca por entidad específica
/config         → Muestra toda la configuración actual
/licitar [id]   → DECIDE PARTICIPAR → crea propuesta → activa Bot 2
/descartar [id] → Marca licitación como descartada
/seguir [id]    → Sigue una licitación (notifica cambios de estado)
/competidores [id] → Quién compite/ganó en procesos similares
/precio [id]    → Estimación de precio basada en histórico
/stats          → Métricas: escaneadas, nuevas, viables, licitadas, ganadas
/export         → Exportar licitaciones filtradas a CSV
```

### 📝 Bot 2: @LicitaPrepBot
```
/start          → Bienvenida + instrucciones
/estado         → Lista de propuestas en preparación con estado
/r [id] [resp]  → Responder una pregunta pendiente del bot
                   La respuesta se guarda en KB automáticamente
/datos          → Ver datos de todas las empresas registradas
/editar_empresa [id] [campo] [valor] → Editar dato de empresa
/equipo         → Ver equipo técnico disponible
/equipo add     → Agregar nuevo profesional
/experiencia    → Ver experiencia registrada
/experiencia add → Agregar nuevo contrato previo
/revisar [id]   → Ver detalle de propuesta antes de aprobar
/corregir [id] [campo] [valor] → Corregir dato en una propuesta
/aprobar [id]   → Genera expediente ZIP final
/descargar [id] → Descargar expediente ZIP
/subir [id]     → Guía paso a paso para subir a SEACE
                   (incluye screenshots y URLs directas)
```

### 🏆 Bot 3: @LicitaWinBot
```
/start          → Bienvenida + instrucciones
/contratos      → Lista de contratos activos con estado
/plazos         → Próximas fechas límite (7 días)
/plazos [dias]  → Fechas límite en X días
/pagos          → Estado de pagos: pendientes y cobrados
/ganar [prop_id] [monto] → Registrar buena pro manualmente
                           (hasta que auto-detección funcione)
/entrega [cont_id]    → Registrar entrega de un contrato
/conformidad [cont_id] → Generar acta de conformidad
/factura [cont_id] [monto] → Generar factura para cobro
/pago [cont_id] [monto]   → Registrar pago recibido
/garantias      → Garantías y fianzas por vencer
/resumen        → Dashboard mensual: contratos, montos, cobrado
/email [cont_id] → Re-enviar notificación por email
```

## N8N WORKFLOWS (http://localhost:5678)

### Workflow 1: Scraping Pipeline (cada 60 min)
```
[CRON 60min] → [HTTP POST localhost:8100/api/scrape] → [IF nuevas > 0] → [Telegram Alert]
```
Ya existe: n8n-workflows/scraping-pipeline.json

### Workflow 2: Resumen Diario (8:00 AM)
```
[CRON 8AM] → [GET /api/daily-summary] + [POST /api/check-wins] + [GET /api/check-deadlines]
```
Ya existe: n8n-workflows/daily-summary.json

### Workflow 3: Checker de Buena Pro (cada 30 min) — 🔨 CREAR
```
[CRON 30min] → [GET propuestas estado=enviado] → [Para cada una: check SEACE] → [IF ganó → Telegram + Email]
```

### Workflow 4: Alertas de Plazos (cada 6 horas) — 🔨 CREAR
```
[CRON 6h] → [GET /api/check-deadlines] → [IF plazos ≤3 días] → [Telegram alerta urgente]
```

## CONFIGURACIÓN EMAIL (shared/email_sender.py)

```python
# Gmail: Usar App Password (no la contraseña normal)
# 1. Activar verificación en 2 pasos en Google
# 2. Ir a myaccount.google.com/apppasswords
# 3. Crear App Password para "Mail"
# 4. Pegar en .env como SMTP_PASSWORD

# Template HTML para email de buena pro:
# - Header verde con "🏆 ¡Buena Pro Ganada!"
# - Tabla con: licitación, entidad, objeto, monto, fecha
# - Sección de plazos próximos
# - Footer con "Enviado por LicitaPro Perú"
```

## DATOS DE LAS EMPRESAS (pre-configurados)

### Empresa 1: K & A Sistemas y Telecomunicaciones S.A.C.
```
RUC: 20490765507
Rubros: tecnología, telecomunicaciones, sistemas, redes, 
        cableado estructurado, videovigilancia, soporte técnico
Email: ventas@sisac.pe
Web: sisac.pe
Departamento: Madre de Dios
Empresa vinculada a Kevin Fabrizio Garcia Espiritu
```

### Empresa 2: Soluciones Informáticas MDD S.A.C.
```
Rubros: software, SISAC, consultoría TI, desarrollo web
Email: ventas@sisac.pe
WhatsApp: 982 683 041
Departamento: Madre de Dios (Puerto Maldonado)
Producto: SISAC (sistema de control de asistencia escolar)
```

### Empresa 3: COMERCIAL Y MULTISERVICIOS SAN JOSE S.A.C.
```
RUC: 20610570420
Rubros: multiservicios, suministros, equipos de cómputo, útiles de oficina
```

### Empresa 4: CUBS FAM S.A.C.
```
RUC: 20602208070
Rubros: servicios generales, mantenimiento, limpieza
```

### Experiencia pre-cargada:
```
1. IESPP "Gustavo Allende Llavería" - Tarma, Junín
   Sistema de gestión académica integral (enrollment, kardex, notas, SIA)
   Keywords: sistema académico, gestión académica, software educativo

2. IIE JEC José Gálvez Barrenechea - La Oroya
   Sistema de control de asistencia SISAC
   Keywords: control de asistencia, biométrico, SISAC, sistema escolar
```

## KEYWORDS DE MONITOREO (default)
```
sistemas, software, tecnología, telecomunicaciones, redes,
cableado estructurado, servidores, soporte técnico,
equipos de cómputo, desarrollo web, base de datos,
seguridad informática, cámaras, videovigilancia,
fibra óptica, UPS, biométrico, control de acceso,
mantenimiento preventivo, sistema académico, ERP,
gestión documental, aplicativo web, aplicativo móvil,
data center, cloud, hosting, dominio, correo electrónico
```

## 25 DEPARTAMENTOS DEL PERÚ (selector)
```
Amazonas, Áncash, Apurímac, Arequipa, Ayacucho, Cajamarca,
Callao, Cusco, Huancavelica, Huánuco, Ica, Junín,
La Libertad, Lambayeque, Lima, Loreto, Madre de Dios,
Moquegua, Pasco, Piura, Puno, San Martín, Tacna,
Tumbes, Ucayali

Regiones default de Kevin: Madre de Dios, Junín, Cusco
```

## 10 TIPOS DE ENTIDAD (filtro)
```
gore    — Gobiernos Regionales (25)
muni    — Municipalidades Provinciales y Distritales (1,874)
min     — Ministerios y organismos del Gobierno Central (19)
univ    — Universidades Públicas (51)
hosp    — Hospitales y redes de salud / EsSalud (400+)
pj      — Poder Judicial - distritos judiciales (34)
ffaa    — Fuerzas Armadas y PNP (30+)
emp     — Empresas estatales - PetroPerú, etc. (35+)
org     — Organismos autónomos - SBS, BCR, etc. (20+)
otro    — Otras entidades (500+)
```

## PRIORIDAD DE ALERTAS
```
🔴 ALTA:     score viabilidad >80% + cierre <7 días
🟡 MEDIA:    score 60-80% O cierre 7-15 días
🟢 BAJA:     score <60% O cierre >15 días
⚡ URGENTE:  contratos menores con cierre <3 días
```

## NOTAS DE IMPLEMENTACIÓN

### Sobre SEACE y firma digital:
La presentación formal a SEACE requiere login con credenciales RNP y 
firma digital. El sistema prepara TODO automáticamente (100% del trabajo),
pero el paso final de subir la oferta lo hace el usuario manualmente 
por seguridad legal. Para contratos menores (≤8 UIT) y cotizaciones
de GOREs, el proceso es más simple y se puede semi-automatizar más.

### Sobre rate limiting de scrapers:
- SEACE: máximo 1 request cada 5 segundos
- OCDS API: sin límite conocido, pero ser conservador
- Contratos Menores: máximo 1 request cada 3 segundos
- GOREs: máximo 1 request cada 10 segundos
- Usar tenacity con retry exponential backoff
- Rotar User-Agents si es necesario

### Sobre el knowledge_base:
Es la pieza MÁS IMPORTANTE del sistema. Cada respuesta del usuario
se guarda con categoría + clave y se reutiliza en todas las futuras
licitaciones. Después de 10-15 licitaciones preparadas, el bot
debería poder generar propuestas completas sin preguntar NADA.

### Sobre la comunicación entre bots:
Los 3 bots son procesos independientes que se comunican via PostgreSQL.
- Bot 1 inserta en tabla `licitaciones` y `propuestas`
- Bot 2 lee `propuestas` con estado='iniciado' y las procesa
- Bot 3 lee `propuestas` con estado='enviado' y monitorea SEACE
- Si un bot falla o se reinicia, los otros siguen funcionando

### Sobre N8N:
N8N está corriendo en http://localhost:5678 en la PC de Kevin (kevin-1).
Los workflows JSON se importan manualmente en N8N.
N8N llama a la API FastAPI interna (puerto 8100) para ejecutar scrapers.
Los bots de Telegram también tienen sus propios schedulers con APScheduler
como backup en caso de que N8N no esté corriendo.

### Sobre la infraestructura:
Todo corre local en la PC kevin-1 (Windows, RTX 4060).
PostgreSQL y Redis en Docker containers.
Los 3 bots como containers Docker.
La API FastAPI como container Docker.
Accesible remotamente via Tailscale si necesita.
Costo total de infraestructura: S/ 0.
Único costo variable: Claude API (~$3-5/mes para análisis de bases).
