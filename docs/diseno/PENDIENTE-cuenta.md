# Pendiente para `licitapro.css` — grupo Cuenta y compra

Lo que las plantillas de este grupo (empresas, empresa_form, configuracion,
suscripcion, precios, comprar, pagar) tuvieron que resolver en su
`{% block estilos %}` y que, por repetirse o por ser de sistema, debería
pasar a la hoja común. Mientras no esté, cada plantilla lleva su copia.

## Clases que conviene compartir

| Clase propuesta | Hoy vive en | Qué hace |
|---|---|---|
| `.estado` (> `.punto` + texto) | configuracion | Punto de color del sistema con su frase al lado, alineado a la primera línea. Sirve para "Conectado / Sin conectar", "Activo en …". |
| `.consiento` | configuracion | Casilla + párrafo de dos líneas (consentimiento WhatsApp, confirmar borrado). `.check` no vale: fija 38 px de alto y centra. |
| `.lineas` (+ `.total`) | comprar, pagar (copia idéntica) | Líneas del importe: concepto a la izquierda, cifra mono a la derecha, total grande en la serif. Es el "resumen del pedido" de cualquier cobro. |
| `.hero` (> `.rotulo` + `h1.display` + `p`) | precios | Cabecera editorial de página pública sin tarjeta. La portada y las legales podrían reusarla. |
| `.tarjeta .titularidad` | precios, comprar | La hoja solo estila `.pie-app .titularidad`; cuando `_titularidad.html` va dentro de una tarjeta (`titularidad_en_cuerpo`) hay que darle tamaño y línea superior. |
| `.img-caja` / `.img-vista` / `.img-form` | empresa_form | Caja de subida de imagen con vista previa sobre fondo claro. Admin de clientes podría necesitarla. |
| `.alta` | empresa_form | Formulario de alta al pie de una tabla, separado con línea y rótulo. Patrón "lista + agregar" que también usan contratos y propuestas. |
| `.lateral` (columna pegajosa de ayuda) | empresa_form | `grid 1fr 300px` desde 1100 px, `position:sticky`. Formularios largos del admin la querrán. |
| `.regiones` | configuracion | Rejilla 4/3/2 columnas de `.check` para las 25 regiones. Si se añade un filtro por regiones en el panel, es la misma pieza. |
| `.planes` 2×2 entre 640 y 1100 px | precios, suscripcion (copia) | `auto-fit` da 3+1 en tableta y deja al cuarto plan solo en una fila. La regla de 2 columnas podría ir en la hoja. |
| `.plan .acciones form` en fila | suscripcion | `select` + botón en una fila dentro de `.plan .acciones`; la hoja fuerza `width:100%` a los botones y hay que anularlo. |
| `input[type=file]::file-selector-button` | empresa_form | El botón nativo del selector de archivo sale con el tema del navegador; conviene estilarlo como `.btn.sec.chico` en la hoja. |
| `header.cab-antigua .brand / .ico / .pe` | precios, comprar (copia) | La hoja estila la marca solo bajo `body > header`; dentro de `.app` la cabecera pública es `header.cab-antigua` y el logo salía a tamaño natural. Basta añadir el selector a las tres reglas existentes y a la regla móvil (≤640) que manda `nav.menu` a su propia fila y quita el borde de `.sesion`. |
| `.peligro-zona` | configuracion | Tarjeta con borde rojo tenue y `h2` en rojo para "borrar cuenta". Admin tiene acciones parecidas. |

## Excepciones anotadas (se quedan en la plantilla)

- `table.datos td{vertical-align:middle}` en suscripcion y configuracion: tablas
  de una línea con pastilla; ver comentario en la plantilla.
- Los tres colores del comprobante impreso (`--papel --tinta --gris` en
  `.tk-papel`) son locales a propósito: es papel y no debe oscurecerse con el
  tema. Si se quiere, pueden pasar a `:root` como `--papel-*`.

## Datos que la ruta debería pasar

- `/empresas` (listado) no recibe `hoy`, así que el estado del RNP se pinta en
  neutro con la fecha; la plantilla ya está preparada: en cuanto
  `web/empresas.py::listar` pase `"hoy": fechas.hoy()` (como hace el
  formulario), la pastilla sale en `.ok / .pend / .urg`. Alternativa: registrar
  `hoy` como global de Jinja en `web/app.py`.
