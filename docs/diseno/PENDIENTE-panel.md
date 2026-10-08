# Pendiente de consolidar en licitapro.css (grupo panel)

Lo que las plantillas del grupo panel (`dashboard.html`, `_tabla.html`,
`licitacion.html`, `propuestas.html`, `propuesta.html`) tuvieron que poner en
su `{% block estilos %}` porque la hoja compartida no lo cubría. Cada punto
dice qué plantilla lo lleva hoy y qué debería pasar a `licitapro.css` para
que el resto de grupos no lo repita.

## Clases y reglas que conviene mover a la hoja

- **`.factores` suelto.** La hoja solo define `.desglose .factores`; fuera de
  un `details.desglose` el contenedor no tiene `display:flex`. La ficha de
  licitación lo usa suelto (puntaje, banderas, análisis). Propuesta:
  `.factores{display:flex;flex-wrap:wrap;gap:6px}` sin el prefijo.
  (En `licitacion.html`.)
- **`table.apilada` en móvil: rótulo a la izquierda y valor a la derecha sin
  repartir.** Con `justify-content:space-between` una celda con dos hijos
  (fecha + chip) los separa a los extremos. Propuesta para la hoja:
  `table.apilada td{justify-content:flex-start;gap:10px;align-items:center}
  table.apilada td[data-eti]::before{margin-right:auto}`. También una celda
  "ancha" sin rótulo (`td.obj`) que se pinte en bloque:
  `table.apilada td.obj{display:block;padding:8px 14px}`.
  (En `dashboard.html` y `propuestas.html`.)
- **`.filtros` en móvil.** Los campos deben ocupar la fila entera y los dos
  botones repartirse la última: `.filtros .campo{flex:1 1 100%}`,
  `.filtros .acciones{width:100%;margin-left:0}`, `.filtros .acciones
  button{flex:1 1 0}`. (En `dashboard.html`.)
- **`.tarjetas` de dos en dos en móvil.** `minmax(180px,1fr)` deja una sola
  columna a 390 px; el panel fuerza `grid-template-columns:1fr 1fr` y
  `.n{font-size:28px}`. (En `dashboard.html`.)
- **Cabecera de ficha: `h1.ficha`.** Para títulos que son el objeto oficial
  (300 caracteres en mayúsculas): `clamp(19px,2.2vw,23px)`, `line-height:1.32`,
  17 px en móvil, y `.cab-pag .rotulo{display:block;margin-bottom:8px}` para
  la entidad encima. (En `licitacion.html` y `propuesta.html`, idéntico.)
- **`.cab-pag .miga` con separador:** `.miga span{color:var(--ink-4)}` para el
  "›". (En `licitacion.html` y `propuesta.html`.)
- **`.htmx-request .tabla-caja{opacity:.55}`**: la única pieza de HTMX; puede
  ir a la hoja como regla general de espera.
- **`.cab-pag .cab-sub b`** en mono y tinta plena, para la cuenta "500
  ordenadas por puntaje". (En `dashboard.html`.)

## Componentes de página que de momento se quedan en la plantilla

- `licitacion.html`: `.tarjeta.banderas` (borde izquierdo ámbar/rojo),
  `.lista-banderas`, `.analisis` (artículo con cabecera, `.puntaje`,
  `.procedencia`, `.porque`) y `.rejilla-ia`. Si otra vista muestra análisis
  de IA, pasarlos a la hoja.
- `propuesta.html`: `.pregunta` (tarjeta de pregunta pendiente/respondida),
  `.avance .txt` (texto bajo la barra), `.rango` (cuartiles de precio) y
  `.dos-firmas / .via` (las dos vías de firma).

## Notas para `_base.html`

- No hay bloque en `<head>`: la `<meta name="htmx-config">` del panel va al
  final del cuerpo, dentro de `{% block scripts %}`. Funciona (HTMX la lee con
  `querySelector` al llegar `DOMContentLoaded`), pero lo correcto sería un
  `{% block head %}{% endblock %}` en la base y moverla allí.

## Aviso sobre el filtro "Solo vigentes"

No es de diseño pero se vio al paginar: el servidor toma `vigentes=1` por
defecto, y una casilla desmarcada no viaja en el formulario, así que desde la
interfaz no se puede ver nunca las vencidas. Los enlaces de paginación
codifican siempre `vigentes=` explícito para no empeorarlo; arreglar el
defecto es cosa del backend (un `<input type="hidden" name="vigentes"
value="0">` delante de la casilla, o leer la ausencia como 0).
