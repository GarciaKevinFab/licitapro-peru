# Pendiente para licitapro.css — grupo "Ganar y cobrar" + legales

Lo que estas plantillas necesitaron y hoy vive duplicado en sus
`{% block estilos %}`. Candidatos a subir a la hoja compartida; en cuanto
estén allí, borrar las copias locales.

Plantillas: `contratos.html`, `contrato.html`, `informes.html`,
`reclamaciones.html`, `terminos.html`, `privacidad.html`.

## Arreglos a reglas que ya existen

- **`label.campo > span.ayuda`** — la regla `label.campo>span` convierte en
  rótulo (mono, mayúsculas) a TODOS los span directos, incluida la ayuda bajo
  el campo. Hace falta la excepción:
  `label.campo > span.ayuda{font-family:var(--f-ui);font-size:12.5px;letter-spacing:0;text-transform:none;font-weight:400}`
  (copiada en contratos, contrato y reclamaciones).
- **`.vacio p`** — sin ancho máximo, en una tarjeta ancha el texto se estira a
  120 caracteres. Propuesta: `.vacio p{max-width:54ch;margin-inline:auto}`.
- **`.datos-ficha dd.hay`** — el "por cobrar" en una ficha debería ir ámbar
  igual que `td.cobrar.hay`: `.datos-ficha dd.hay{color:var(--warn)}`.
- **`table.datos td.monto, td.codigo, td.fecha`** — `white-space:nowrap`; un
  monto partido en dos líneas no se lee de un golpe.
- **`.mensaje p`** — `margin:0`; un párrafo dentro del mensaje hereda los
  márgenes del texto y descuadra la franja.
- **`.tarjetas .t .n small`** — prefijo de moneda pequeño dentro de la cifra:
  `font-family:var(--f-mono);font-size:14px;color:var(--ink-3);margin-right:4px;vertical-align:middle`.
  El JS no anima esas cifras (no son enteros puros), y está bien así.

## Línea de tiempo (`ul.timeline`) con acciones

- `.timeline li .hito{display:flex;justify-content:space-between;gap:10px 16px;flex-wrap:wrap}`
  con `> div{flex:1 1 220px;min-width:0}` y `form{flex:none}`: cada hito con
  su botón a la derecha.
- `.timeline li.hecho b` tachado en `--ink-3`; `.timeline li.vence .cuando`
  en `--urg` y `.timeline li.pronto .cuando` en `--warn` (hoy solo cambia el
  punto, y el punto se pierde en una lista larga).

## Página legal (`terminos` y `privacidad` comparten el bloque entero)

- `.legal{max-width:70ch;margin:0 auto}` con `h1.display` a
  `clamp(30px,5vw,42px)`, `.fecha` como rótulo mono, `.intro` a 16px.
- `.legal h2{font-family:var(--f-display);font-size:21px;display:flex;gap:12px;align-items:baseline;scroll-margin-top:80px}`
  y `.legal h2 .n` (número de sección en mono acento).
- `.indice` (tarjeta con `ol` a dos columnas, una en móvil) y `.indice a .n`.
- `.legal .titularidad` (caja tenue dentro del artículo) y `.legal .pie-legal`.
- `.legal p / ul / li` con `line-height:1.7` y negritas en `--ink`.

## Titularidad fuera del pie

- `main > .titularidad` (reclamaciones la incluye en el cuerpo): mismo tamaño
  y color que `.pie-app .titularidad`, con filo superior. Hoy esa regla solo
  existe bajo `.pie-app`.

## Libro de Reclamaciones

- `fieldset .opciones` (radios en fila con un `.etiqueta` arriba a ancho
  completo) podría ser genérico: `.opciones{display:flex;gap:6px 22px;flex-wrap:wrap;align-items:center}`
  y `.opciones .etiqueta{flex:1 1 100%}`.
- `.definiciones` (dos cajas "Reclamo / Queja") es propio de la página; no
  hace falta subirlo.
