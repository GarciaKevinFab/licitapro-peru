# Pendientes del grupo admin para `licitapro.css`

Lo que el panel del dueño (`admin_clientes`, `admin_cliente`,
`admin_cliente_form`, `admin_ia`) necesitó y la hoja no tiene. Hoy vive en
`web/templates/_admin_estilos.html` o en el `{% block estilos %}` de cada
plantilla; todo esto es compartible y conviene subirlo a la hoja cuando otro
grupo lo pida también. Ninguna regla escribe colores: todo son tokens.

## Candidatas a la hoja

| Regla | Dónde está hoy | Por qué compartirla |
|---|---|---|
| `.nota{display:block;font-size:12px;color:var(--ink-3);overflow-wrap:anywhere}` | `_admin_estilos.html` | Línea secundaria en minúsculas bajo un dato ("mensual · pagó el 25/08", el correo bajo el nombre). `.ent` es mayúsculas y sirve para rótulos, no para frases. La van a querer licitaciones, contratos y propuestas. Ojo: hoy existe `.plan .nota` dentro de la tarjeta de precios. |
| `.acciones{display:flex;flex-wrap:wrap;gap:6px;align-items:center}` + `.acciones form{display:contents}` | `_admin_estilos.html` | La hoja tiene el revelado al pasar el ratón (`table.datos tbody tr .acciones{opacity}`) pero no la disposición. Y los `<form>` de un botón dentro de una celda necesitan `display:contents` para repartirse como hermanos. `empresas.html` ya lo redefine por su cuenta. |
| `.rejilla .ancho{grid-column:1 / -1}` | `_admin_estilos.html` | `.rejilla` existe pero no hay forma de que un campo ocupe toda la fila (contraseña, casilla de "cuenta activa", textarea). |
| `.clave` (campo + botón en línea, input en mono) | `_admin_estilos.html` | Cualquier formulario "valor + botón" (contraseña, código, RUC con "consultar"). Un `.fila` genérico con `.campo{flex:1}` lo cubriría. |
| `.datos-ficha dd{min-width:0;overflow-wrap:anywhere}` | `_admin_estilos.html` | Un correo o razón social sin espacios se monta sobre la celda de al lado de la ficha. Debería ir en la propia `.datos-ficha`. |
| `.cab-pag .miga{flex-wrap:wrap}` + `.cab-pag .miga span{min-width:0;overflow-wrap:anywhere}` | `_admin_estilos.html` | En 390 px una miga con un nombre largo encogía "Editar" letra a letra. Debería ir en `.miga`. |
| `table.datos td.num,td.fecha,th.num{white-space:nowrap}` | `_admin_estilos.html` | Cuando un texto largo aprieta la tabla, "S/ 61.20" se partía en "S/" y "61.20". Montos y fechas nunca deberían partirse. |
| `@media (max-width:640px){.tarjetas{grid-template-columns:1fr 1fr}.tarjetas .t .n{font-size:28px}}` | `_admin_estilos.html` | Con `auto-fit minmax(180px)` en 390 px salen una debajo de otra: cinco cifras son una torre antes de llegar al contenido. Dos por fila caben bien a 28 px. El dashboard tendrá el mismo problema. |
| `@media (max-width:640px){.acciones .btn,.acciones button{min-height:44px;flex:1 1 auto}}` | `_admin_estilos.html` | La hoja deja `.chico` en 36 px en móvil; en una fila de acciones táctiles conviene el suelo de 44. |
| Filtros en móvil: `.filtros .campo{flex:1 1 100%}` `.filtros .acciones{width:100%;margin-left:0}` y botones `flex:1 1 auto` | `admin_clientes.html` | `.filtros` no tiene reglas de móvil: los selects quedan a medio ancho y el botón flotando a la derecha. Lo necesitarán licitaciones y contratos. |
| Celdas "ricas" en tablas apiladas: `table.apilada td.bloque{display:block}` + `td.bloque[data-eti]::before{display:block;margin-bottom:4px}` | `admin_clientes.html` (`td.cuenta`, `td.susc`, `td.acciones-c`) y `admin_cliente.html` (`td.empresa`) | En `.apilada` cada `td` es flex "rótulo a la izquierda, valor a la derecha". Una celda con nombre + correo + pastilla, o con seis botones, necesita pintarse en bloque con el rótulo encima. Un modificador genérico (`td.bloque`) evitaría repetirlo por página. |
| `.cuadricula{align-items:start}` como variante (`.cuadricula.arriba`) | `admin_cliente.html` | Dos tarjetas de alturas muy distintas: estirar la corta deja un vacío bajo sus botones. |
| `dialog.confirmar p` y `.dlg-correo` | `_admin_estilos.html` | El diálogo ya está en la hoja pero sin estilo para el párrafo ni para el dato que se va a borrar. |

## Observaciones sobre la hoja

- Una celda `<td class="plan">` recoge `.plan{...}` de la tarjeta de precios
  (fondo, borde, padding, flex). Por eso la columna de plan de la lista se
  llama `td.susc`. Convendría acotar `.plan` a `.planes > .plan`.
- `label.campo > span` pinta TODOS los spans directos como rótulo en mono
  mayúsculas: una ayuda dentro del label hay que escribirla como `<small
  class="ayuda">`. Mejor `label.campo > span:first-child`.
- Las tablas en `.apilada` requieren que cada celda con `data-eti` tenga UN
  solo hijo (envuelto en `<span>` o `<div>`): si el contenido es texto suelto
  más una pastilla, el flex los reparte por la fila. Vale la pena anotarlo en
  SISTEMA.md.
- El contador de `.tarjetas .t .n` no anima "S/ 1,240" (empieza con letras);
  correcto, pero conviene saberlo: las cifras con moneda no suben.
- Las capturas a página completa con Playwright dejan en `opacity:0` las
  `.tarjeta` bajo el pliegue (el `.rv-app` nunca recibe `.in`). Para capturar
  hay que abrir el contexto con `reduced_motion="reduce"`.
