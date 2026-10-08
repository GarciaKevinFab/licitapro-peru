# Sistema de diseño de LicitaPro ("Sala de control")

Guía para rediseñar cualquier plantilla de `web/templates/`. La hoja de
estilos es **una sola**: `web/static/licitapro.css`. Las plantillas NO deben
redefinir lo que ya está ahí; solo añaden en `{% block estilos %}` lo
estrictamente propio de la página (y poco).

## Dirección

Grafito cálido casi negro, una sola señal de color (el verde menta de la
marca), titulares en una serif con carácter y datos siempre en mono. La
referencia es la terminal de un operador que lee cientos de licitaciones al
día: **densa, legible, jerárquica, sin adornos que compitan con el dato**, pero
con la gravedad editorial de un diario económico. Solo modo oscuro.

- Títulos de página y cifras grandes: **Fraunces** (`--f-display`).
- Interfaz y texto: **IBM Plex Sans** (`--f-ui`), 14.5px base.
- Montos, fechas, códigos, rótulos pequeños en mayúsculas: **IBM Plex Mono**
  (`--f-mono`) con cifras tabulares. Clases: `.mono .num .fecha .monto .codigo .rotulo`.
- Nada de emojis como iconos. Iconos SVG inline (trazo 1.8, viewBox 24, con
  `aria-hidden="true"`), estilo Lucide; tamaño 16–18 px dentro de botones y navegación.

## Tokens (CSS custom properties, ya definidos)

Superficies `--bg --bg-2 --panel --panel-2 --panel-3`; líneas `--line --line-soft --line-fuerte`;
tinta `--ink --ink-2 --ink-3 --ink-4`; acento `--ac --ac-2 --ac-3 --ac-soft --ac-glow`;
semánticos `--urg --warn --ok --info` (cada uno con `-soft`); `--btn-txt`;
radios `--r` (7) `--r-card` (12) `--r-chip` (4) `--r-pill`; sombras `--sombra-1 --sombra-2`;
movimiento `--dur --ease-out --ease`. **Nunca escribas un color en una plantilla: usa tokens.**

## Shell de la aplicación

`_base.html` envuelve todo en `<div class="app">`. `_nav.html` dibuja la barra
lateral (`aside.barra-lateral`), el telón y la barra superior móvil. Las
plantillas del panel incluyen `_nav.html` con `activo` y `usuario`:

```jinja
{% extends "_base.html" %}
{% set titulo_pagina = "Contratos" %}
{% set sub_pagina = "Lo que ganaste: estado, plazos y lo que falta por cobrar." %}
{% block titulo %}Contratos · LicitaPro{% endblock %}
{% block nav %}{% include "_nav.html" with context %}{% endblock %}   {# pasa activo/usuario #}
{% block acciones_pagina %}<a class="btn" href="/contratos/nuevo">Registrar</a>{% endblock %}
{% block contenido %} ... {% endblock %}
```

Si la plantilla escribe su propio `<main>` dentro de `{% block cuerpo %}`,
debe incluir `_nav.html` antes del `<main>` y puede usar `main.estrecho`
(860px) o `main.ancho`.

## Catálogo de componentes (clases de licitapro.css)

| Pieza | Clases | Notas |
|---|---|---|
| Cabecera de página | `.cab-pag` > `h1`, `.cab-sub`, `.cab-acc`, opcional `.miga` | La base la pinta sola con `titulo_pagina`/`sub_pagina` |
| Botones | `button`, `.btn`; variantes `.sec .fantasma .peligro`; tamaños `.chico .grande`; `.btn-grupo` | Primario = una sola acción principal por vista |
| Campos | `label.campo > span + input`; `.ayuda`; `.campo.mal` + `.error`; `.rejilla` (`.dos`, `.tres`); `fieldset/legend`; `.check`; `.fila` (`.entre`, `.derecha`) | Conservar todos los `name=` |
| Tarjeta | `.tarjeta` (`.realzada .compacta .sin-relleno`), `.cab-tarjeta`, `h2`, `.sub`, `.pie-tarjeta`; `.cuadricula` para varias | |
| Cifras clave | `.tarjetas > .t > .n + .l` (+ `.dato/.acento .alerta .aviso-k`, `.delta`) | El JS anima `.tarjetas .t .n`: no cambiar esa estructura |
| Pastillas | `.pill` (`.ok .urg .pend .info .ac .neutro`), `.chip` (`.venc .urg .pronto .ok`), `.aviso` (`.urg .warn`), `.marca.n1-3`, `.punto` | Todas mono mayúsculas 10.5px |
| Mensajes | `.mensaje` (`.ok .mal .aviso .info`) | Pruebas: `class="mensaje mal"` literal en reclamaciones |
| Estado vacío | `.vacio` > `svg.ilustracion` + `b` + `p` + `.btn` | Siempre con una acción |
| Tablas | `.tabla-caja` (`.suelta`) > `table.datos` (`.apilada` + `td[data-eti]` para móvil); `th.num/td.num`; `.ent .obj .desc .nd`; `.acciones` | Cabecera pegajosa incluida |
| Puntaje | `.score > .sv + .sbar > .sfill.pNN`; `details.desglose`, `.factores > .factor` | pNN de 5 en 5 |
| Filtros | `.filtros` > `.campo` (`.crece`), `.buscador`, `.acciones` | |
| Pestañas | `.tabs > a.activo` | |
| Paginación | `.paginacion > .paginas > a / span.actual` | |
| Ficha | `dl.datos-ficha > div > dt + dd` (`.num .grande`) | Para cabeceras de licitación/contrato |
| Pasos / línea de tiempo | `ul.pasos > li(.listo .actual) > .marcador + div + .btn`; `ul.timeline > li(.hecho .vence .pronto) > .cuando + b` | |
| Progreso | `.progreso > span` con `.w-NN`? No: usa `style` NO; usa `.sfill.pNN` dentro de `.sbar` o ancho por clase | |
| Arranque | `.arranque .arr-cab .arr-cuenta .arr-pasos li(.listo) .arr-marca .arr-txt` | Ya existe en dashboard |
| Planes | `.planes > .plan(.destacado) > .rotulo + h3 + .precio(+small) + .anual + ul(li.no) + .acciones + .nota` | Para precios y suscripción |
| Utilidades | `.mt-8…32 .mb-* .w-* .tenue .muy-tenue .texto-centro .oculto-movil .solo-movil .visualmente-oculto .en-linea .col-15 .fila-check` | Lista cerrada: no inventes utilidades nuevas |

## Reglas duras (las comprueban las pruebas o la CSP)

1. **Prohibido** `style="..."` y cualquier atributo `on*=` en el HTML. Nada de `<script>` en línea; si hace falta comportamiento, va en `web/static/licitapro.js` con `data-*`.
2. Conservar **todos los `name=`** de formularios, los `action=`, los `id=` y `hx-*` existentes, y los ganchos del JS: `data-ir-a`, `data-confirmar`, `data-dialogo`, `data-accion`, `data-correo`, `data-resumen`, `data-esperado`, `data-exige`, `data-cerrar`, `data-validar`, `data-cargando`, `data-autocompletar-ruc`, `data-ruc-estado`, `data-culqi-*`, `#tabla`, `#cuenta-lic`, `.tarjetas .t .n`, `.metas b`, `.analisis .puntaje`, `.rv` (landing), `#nav #ring #dialval #bars canvas#territorio .cifra[data-hasta]`, ids de sección de la portada.
3. Conservar los textos legales y de marca que buscan las pruebas: "es un servicio de" (exactamente UNA vez por página: lo pone `_titularidad.html`; nunca lo escribas a mano), "prestado bajo la marca", `<!--email_off-->`/`<!--/email_off-->` alrededor de cada `mailto:`, teléfono `tel:+5182573844` y "(082) 573844", "Indecopi", "29571", "29733", "doce meses anteriores", "Cercado de Lima", "Política incompleta", "No te pedimos el PIN", "No guardamos el número de tu tarjeta", "Izipay", "Culqi", "Telegram", "Meta", "Anthropic", "15 días hábiles", "S/", "Resumen del pedido", "Total a pagar", "Pagar S/ 99.00", "irreversible", "se renueva automáticamente", "pago único", "no deja cobros programados"/"de una sola vez".
4. Accesibilidad: labels asociados, `aria-current="page"` en el activo, foco visible (ya en la hoja), objetivos táctiles ≥44px en móvil (ya en la hoja), contraste ≥4.5 (los tokens lo cumplen: no uses `--ink-4` para texto de lectura).
5. Responsive en 390, 768, 1024 y 1440. Sin scroll horizontal de página; las tablas se deslizan dentro de `.tabla-caja` o se apilan con `.apilada`.
6. Texto en español peruano, con tildes. Sin emojis.
7. Mantén los comentarios Jinja `{# ... #}` que explican decisiones (son documentación del proyecto) y escribe los tuyos con el mismo tono cuando tomes una decisión no obvia.

## Cómo comprobar tu trabajo

- Compilación de plantillas (sin base de datos):
  `docker run --rm -u 0 -v /opt/licitapro:/src -w /src -e DATABASE_URL= -e LICITAPRO_SECRET_KEY=x licitapro-radar:latest python -c "from jinja2 import Environment,FileSystemLoader; e=Environment(loader=FileSystemLoader('web/templates')); [e.get_template(t) for t in ['empresas.html']]; print('ok')"`
- Vista previa viva con el código del disco (NO es producción): `http://127.0.0.1:8201`. Cuenta de prueba en `/tmp/claude-0/-root-vps/3518bdb0-c054-48af-9453-efe37cece997/scratchpad/cuenta_prueba.txt` (formato `email:password`).
- Capturas reales (escritorio 1440 y móvil 390), con login:
  `docker run --rm --network host -v /tmp/claude-0/-root-vps/3518bdb0-c054-48af-9453-efe37cece997/scratchpad:/w -w /w -e BASE=http://127.0.0.1:8201 licitapro-shots python3 shots.py /w/despues/<grupo> --login "$(cat /w/cuenta_prueba.txt)" /ruta1 /ruta2`
  (añade `-e SOLO_ARRIBA=1` para capturar solo la parte visible). Luego mira los PNG con la herramienta Read y corrige lo que se vea mal.
- Pruebas: `docker run --rm -u 0 --dns 1.1.1.1 -v /opt/licitapro:/src -w /src -e DATABASE_URL= licitapro-radar:latest sh -c "pip install -q -r requirements-dev.txt >/dev/null 2>&1; python -m pytest -q tests/test_aislamiento.py tests/test_compra.py tests/test_reclamaciones.py 2>&1 | tail -5"` (las 2 de `test_compra` sobre `_comercio` fallan desde antes; cualquier otra que falle es tuya).
