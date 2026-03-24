# PROMPT PARA CLAUDE CODE — Expandir LicitaPro Perú
# Copia todo este archivo y pégalo en Claude Code

Tengo un proyecto LicitaPro Perú ya funcionando con esta estructura:
- 3 bots de Telegram (radar, prep, win)
- PostgreSQL con 14 tablas
- Scraper de SEACE 3.0 + OCDS API + Contratos Menores
- Orchestrator que ejecuta todos los scrapers
- Auto-fill engine con knowledge_base
- Generador de carta de presentación y declaración jurada

NECESITO QUE EXPANDAS estos módulos:

## 1. Scrapers faltantes (completar en radar_bot/scrapers/)

### peru_compras.py
- URL: https://buscadorcatalogos.perucompras.gob.pe/
- Scrapear catálogos electrónicos activos
- Buscar órdenes de compra relevantes
- Filtrar por keywords del usuario

### transparencia.py
- URL: https://www.transparencia.gob.pe/
- Scrapear PAC (Plan Anual de Contrataciones) de entidades
- Detectar futuras contrataciones planificadas

### essalud.py
- Combinar scraping de SEACE (filtro entidad=ESSALUD)
- Portal propio si existe API

### poder_judicial.py
- URL: https://sap.pj.gob.pe/portalabastecimiento-web/
- Es una app JavaScript, necesita Playwright
- Extraer contrataciones de los 34 distritos judiciales

## 2. Generador de documentos (completar en prep_bot/autofill/)

### proposal_generator.py
Usar Claude API para generar propuesta técnica adaptada a las bases:
- Input: bases PDF (texto extraído), datos empresa, experiencia
- Output: propuesta_tecnica.docx con:
  - Presentación de la empresa
  - Metodología propuesta
  - Plan de trabajo con cronograma
  - Equipo técnico asignado
  - Experiencia relevante

### pricing_calculator.py
Calcular precio económico competitivo:
- Consultar historico_precios en DB
- Buscar licitaciones similares adjudicadas
- Calcular ratio promedio adj/ref
- Aplicar margen del usuario (knowledge_base)
- Output: precio sugerido (min, recomendado, max)

### zip_builder.py
Compilar expediente completo:
- Listar todos los documentos generados
- Agregar bases descargadas como referencia
- Crear ZIP con estructura de carpetas SEACE
- Validar que no falte ningún documento obligatorio

## 3. Análisis IA con Claude API (shared/analyzer.py)

### analyze_bases(pdf_path) -> dict
- Extraer texto del PDF de bases
- Enviar a Claude API con prompt especializado
- Extraer: requisitos técnicos mínimos, criterios de evaluación,
  documentos obligatorios, plazos, garantías, penalidades
- Calcular score de viabilidad basado en match con empresa

### Prompt para Claude API:
```
Eres un experto en contrataciones del Estado peruano.
Analiza estas bases de licitación y extrae:

1. REQUISITOS TÉCNICOS MÍNIMOS (experiencia, personal, equipamiento)
2. CRITERIOS DE EVALUACIÓN (técnicos y económicos, puntajes)
3. DOCUMENTOS OBLIGATORIOS (lista de anexos requeridos)
4. PLAZOS (ejecución, garantías, pagos)
5. PENALIDADES
6. MONTO REFERENCIAL
7. TIPO DE PROCEDIMIENTO

Responde SOLO en JSON.
```

## 4. Monitor de adjudicaciones (win_bot/monitor.py)

### check_adjudicaciones_seace()
- Para cada propuesta con estado='enviado'
- Buscar en SEACE si ya salió la buena pro
- Comparar RUC ganador con nuestros RUCs
- Si ganamos → crear contrato automáticamente
- Si perdimos → marcar como 'perdido' y notificar

## 5. API HTTP interna (shared/api_server.py)

Crear un servidor FastAPI ligero (puerto 8100) que exponga:
- POST /api/scrape → ejecuta orchestrator
- GET /api/daily-summary → resumen del día
- POST /api/check-wins → verifica adjudicaciones
- GET /api/check-deadlines → plazos próximos

Esto permite que N8N llame a los scrapers via HTTP.

## CONTEXTO TÉCNICO
- Stack: Python 3.11, asyncpg, python-telegram-bot 21, httpx, BeautifulSoup
- DB: PostgreSQL (schema ya creado en shared/schema.sql)
- Los archivos existentes están en el proyecto, no los sobreescribas
- Cada módulo nuevo debe ser independiente y con manejo de errores
