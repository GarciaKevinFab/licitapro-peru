# Pendiente del grupo público (portada y acceso)

Lo que salió del rediseño de `landing.html`, `entrar.html`, `recuperar.html`,
`_acceso_panel.html` y `_acceso_estilos.html` y que conviene subir a
`licitapro.css` o resolver en otro sitio. Nada de esto bloquea: todo funciona
hoy dentro del `<style nonce>` de cada plantilla.

## Para subir a `licitapro.css` (lo repiten dos o más páginas)

- **Marca como parcial.** El SVG del radar + "LicitaPro *Perú*" está copiado
  seis veces (barra lateral, topbar, pie de `_base`, nav y pie de la portada,
  panel de acceso, marca móvil del acceso). Un `_marca.html` con `class` y
  tamaño por parámetro acabaría con eso. Estilo ya unificado: Fraunces 600,
  `opsz 48`, "Perú" en itálica `--ink-3`, icono `--ac` con `drop-shadow`.
- **`.btn .fl`** (flecha que se desplaza 3 px al pasar el ratón). La portada y
  el acceso la usan; el panel la querría en "Siguiente"/"Continuar".
- **Rejilla de dos columnas del acceso (`.acceso`, `.pm`, `.lf`, `.caja`)**:
  si alguna otra página pública (p. ej. una invitación o un alta por enlace)
  necesita el mismo marco, moverlo a la hoja evitaría incluir
  `_acceso_estilos.html` en más plantillas.
- **Cifra editorial grande (`.cifra .n`)**: Fraunces 500, `opsz 144`,
  `clamp(3rem,6vw,4.8rem)`. Es `.tarjetas .t .n` a otra escala; podría ser un
  modificador `.tarjetas.grandes` para informes y la portada.
- **`.faq` con `<details>`**: borde superior, cruz que gira 45°, respuesta a
  68ch. Ayuda y Soporte dentro del panel pedirían lo mismo.
- **`.comparativa`** sobre `table.datos`: columnas centradas salvo la primera,
  cabecera destacada `--ac`, fila de precio en mono. Sirve para comparar planes
  en `/precios` y `/suscripcion`.
- **Rótulo con raya delante (`.tag`, `.kicker`, `.pm-kicker`)**: tres nombres
  para el mismo `.rotulo` con `::before` de 26–34 px en `--ac`. Un solo
  `.rotulo.con-raya` bastaría.

## Choques de nombres detectados

- `ul.pasos` / `.paso` (sistema) vs. la rejilla de tres etapas de la portada:
  renombrada a `.etapas` / `.etapa`. Si algún día `.pasos` del panel admite
  columnas, podría absorberla.
- `.punto` (sistema, 8 px) se usa en la portada con `.punto.ac`: encaja, no
  hace falta nada.

## Comportamiento (landing.js)

- Los contadores admiten ahora `data-prefijo` (además de `data-sufijo` y
  `data-miles`); los miles se formatean con punto a mano porque
  `toLocaleString("es-PE")` devuelve coma en algunos Chromium. `licitapro.js`
  anima `.tarjetas .t .n` con su propia rutina: valdría la pena que ambas
  compartieran el formateador.
- El canvas lee el color de `--ac` con `getComputedStyle`: si se cambian los
  tokens no hay que tocar el script.

## Contenido

- Los planes dicen "Avisos por correo y Telegram" (igual que `/precios`),
  mientras que el hero y las preguntas ya nombran WhatsApp. Cuando WhatsApp
  esté disponible en todos los planes con alertas, actualizar ambas plantillas
  (y el correo de bienvenida) a la vez.
- La cifra "+2.400 compras menores vigentes hoy" está escrita a mano en
  `landing.html` (`data-hasta="2400"`) y en `_acceso_panel.html`. Si existe un
  conteo real en la base, pasarlo en el contexto de `/` y de `/entrar` y
  pintarlo desde ahí para que no caduque.
