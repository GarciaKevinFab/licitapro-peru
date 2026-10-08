# Cambios hechos directamente en el servidor

El código fuente vive en la PC de desarrollo y se sube al servidor por tar. Lo
que se toque aquí hay que llevarlo de vuelta a la PC, o se pierde en la
siguiente subida. Cada entrada dice qué se cambió, por qué y cómo llevarlo.

El árbol de `/opt/licitapro` NO es un checkout limpio: 117 archivos están en
CRLF (el índice en LF) y hay cambios sin commitear que vienen de la PC
(prep_bot, web/comprar.py, tests/test_compra.py). Por eso los cambios de aquí
se entregan como parche y no como commit.

## 2026-10-07 — regiones, vigilancia de OECE y fuentes de Cusco

Parche con SOLO estos cambios: `docs/parches/2026-10-07-regiones-y-fuentes.patch`
(diff entre el árbol tal como estaba antes de tocarlo y después; en LF). En la
PC, desde la raíz del proyecto:
`git apply --ignore-whitespace docs/parches/2026-10-07-regiones-y-fuentes.patch`
(o `patch -p1 < ...`). Si `ESTADO.md` o `DESPLIEGUE.md` no aplican porque allí
están más nuevos, copiarlos del servidor a mano: son texto.

Qué cambia:

1. **`radar_bot/scrapers/ocds_oece.py` — `_departamento` compara palabras
   enteras.** Antes `"ica" in texto` etiquetaba como Ica todo lo que dijera
   publICA/tecnICA/electrICA: 1.010 filas mal (el GORE Puno tenía 50 en Ica).
   Prueba nueva: `tests/test_ocds_departamento.py`.
2. **`shared/departamentos.py` (nuevo) — completa `departamento` por RUC de la
   entidad** cuando ese RUC apunta claramente a una sola región (≥90 % de sus
   filas, mínimo 2). Corre al final de cada pasada del orquestador. Ya se
   aplicó a la base con `tools/reparar_departamentos.py --aplicar`
   (1.010 corregidas, 538 completadas).
3. **`shared/vigilancia.py` — tercera avería, "rezago".** `horas_de_rezago`
   mide la edad de la convocatoria más reciente de OECE; avisa a las 72 h.
   El 2026-10-07 el puente leía 800 releases cada 4 h y el dato más nuevo
   tenía 5 días; nadie lo vio. `/salud` ahora devuelve `oece_rezago_horas`.
   Pruebas en `tests/test_vigilancia.py`.
4. **`radar_bot/scrapers/orchestrator.py` — portal de cotizaciones del GORE
   Cusco** en `GORE_COTIZACIONES_PORTALS` (lo cosecha el puente; ver
   DESPLIEGUE.md 12.bis). Y `completar_por_ruc` al final de `run_all_scrapers`.
5. **`radar_bot/scrapers/ima_cusco.py` (nuevo, APAGADO)** — scraper de
   compras <8 UIT del IMA (GORE Cusco), con prueba sobre su HTML real. Apagado
   en `FUENTES_APAGADAS` porque el IMA no publica desde el 09/02/2026.

6. **`shared/ubigeo.py` + `shared/ubigeo.csv` (nuevos)** — región de
   municipalidades y UGEL por la tabla oficial de ubigeos (INEI, 1.893
   distritos). La usan `ocds_oece._departamento` (que ahora mira la entidad
   antes que el objeto) y `orchestrator._detectar_depto` (gob.pe).
   Prueba: `tests/test_ubigeo.py`. Segunda pasada del reparador: 351 filas
   más con región; quedan 1.500 sin región, ~1.050 de entidades nacionales.
7. **`radar_bot/scrapers/gob_pe.py`** — consulta nueva "invitacion a cotizar"
   y `cotizar` en el filtro de compras (EPS de agua y municipalidades lo
   llaman así; 54 publicaciones/semana que no entraban).

8. **`radar_bot/scrapers/oece_menores.py` (nuevo, ACTIVO)** — contratos
   menores (<8 UIT) de las 25 regiones desde la herramienta digital del OECE
   (Ley 32069), vía su API pública sin sesión. Recorre los 25 departamentos
   (hasta 200 por página) y pide el detalle solo de las filas nuevas. 2.445
   vigentes el día del despliegue. Prueba: `tests/test_oece_menores.py`.
   Barrido del mismo día que lo encontró: subdominios de cotizaciones de los
   25 GORE (solo Madre de Dios, Cusco y Moquegua existen; Moquegua bloquea al
   VPS igual que los otros dos: candidato para el puente), servicios de
   gob.pe con "cotizaciones"/"8 UIT" (GORE Apurímac, GEREDU Cusco, UNIQ, San
   Gabán, ENOSA, EPS Grau, UNDC, PVN, CENARES, SUNARP, OTASS: portales
   pequeños o bloqueados; anotados en ESTADO.md) y municipalidades de
   capitales (solo Abancay tiene portal, y no responde al VPS).

9. **Flujo de después de ganar** (`shared/notificaciones.py`, `shared/db.py`,
   `win_bot/main.py`; prueba `tests/test_posganar.py`). Revisado con la base
   real:
   - `detectar_adjudicaciones` filtraba `estado = 'enviado'` y ninguna ruta
     pone ese estado (el ciclo real es iniciado → listo → ganada): el
     detector corría cada 30 min sobre cero filas. Ahora `IN ('listo','enviado')`.
   - Los avisos "parece que ganaste" y "cobros con plazo vencido" salían solo
     por Telegram/WhatsApp. Ahora también por correo (el único canal que tenía
     el primer cliente).
   - Los plazos de contrato se mandaban todos a ADMIN_ID y se marcaban como
     avisados aunque nadie los recibiera. Ahora van al dueño del contrato por
     sus canales (`avisar_plazo_proximo`) y solo se marcan si salieron.
   - Lo que NO cambia y conviene saber: la fecha límite de pago (10 días
     hábiles, Ley 32069) solo corre si el usuario registra la **fecha de
     conformidad** en el cobro; sin ella el panel lo dice y no avisa. Y
     `/reclamaciones` es el Libro de Reclamaciones de LicitaPro, no una carta
     de reclamo a la entidad: eso no existe todavía.

10. **Autocompletar la empresa por RUC** (`shared/sunat.py` nuevo,
    `shared/ubigeo.py` con `por_ubigeo`, ruta `GET /empresas/ruc/{ruc}` en
    `web/empresas.py`, JS al final de `web/static/licitapro.js`, campo RUC del
    `empresa_form.html`). Al teclear 11 dígitos consulta el padrón de SUNAT
    vía OpenRUC (respaldo apis.net.pe, que limita por IP) y rellena razón
    social, dirección y departamento (por ubigeo) si están vacíos; muestra
    ACTIVO/HABIDO. Representante legal y RNP no se pueden rellenar (están
    detrás de captcha). Prueba: `tests/test_sunat.py`.

11. **Sin ventanas emergentes del navegador.** El único `window.confirm` de la
    app (`data-confirmar`: cancelar suscripción, cambiar de plan, desactivar
    empresa) se sustituye por una confirmación en línea dentro de la página
    (`licitapro.js` + estilo `.confirmar-en-linea` en `_base.html`). El
    `<dialog>` de borrar cuenta en `/admin` es un elemento de la propia página
    (no una ventana del navegador) y se conserva.

12. **Rediseño completo del frontend.** Sistema de diseño en una sola hoja
    externa `web/static/licitapro.css` (tokens, tipografía Fraunces + IBM
    Plex Sans/Mono **autoalojada** en `web/static/fonts/`, sin Google Fonts),
    shell de aplicación con barra lateral (`_nav.html`) y menú móvil
    (`licitapro.js`), `_base.html` con bloque `head`, y las 31 plantillas
    reescritas sobre el sistema (ver `docs/diseno/SISTEMA.md`; lo que cada
    grupo pidió está en `docs/diseno/PENDIENTE-*.md` y ya consolidado en la
    hoja). Cambios funcionales que vinieron con el rediseño: **paginación del
    panel** (`/panel` y `/parts/tabla`, 30 por página, `web/app.py`), el
    filtro "Solo vigentes" ahora se puede desmarcar (`_vigentes_efectivo`),
    `/empresas` pasa `hoy` para colorear el RNP, favicon alineado con la
    marca y nueva imagen `og.png` para WhatsApp. El parche NO incluye los
    binarios: copiar a la PC `web/static/fonts/*.woff2` (9 archivos) y
    `web/static/og.png`.
    Verificado con capturas reales (escritorio 1440 y móvil 390) de todas
    las pantallas, 419 pruebas en verde y sin `style=`/`on*=` en plantillas
    (la CSP sigue sin `unsafe-inline`).

Desplegado: imágenes `web`, `radar` y `win` reconstruidas y levantadas el
2026-10-07 (12:12, 12:52, 13:15, 13:50, 15:20, 15:35 y 16:40 —rediseño—,
hora de Lima). `prep` y `win` siguen con la imagen
anterior: no usan nada de esto. El parche incluye `shared/ubigeo.csv` entero
(1.894 líneas): es un archivo nuevo, no un diff.

Pendiente en la PC puente: `git pull` (o aplicar el parche) para que empiece a
cosechar Cusco; comprobar en `data/traer_oece.log` la línea
`GORE cotizaciones: guardadas N nuevas`.

Nota del 2026-10-07 22:29: el parche se regeneró. La versión anterior
**borraba por error `web/static/culqi-checkout.js`** (el checkout de pagos)
porque la copia "después" no lo incluía; el archivo nunca se tocó y el parche
ya no lo menciona (68 archivos, 22 nuevos, 0 borrados; verificado aplicándolo
sobre la copia "antes" y comparando con el árbol actual).

Si la PC puente no tiene `git`, no hace falta el parche: basta copiar desde el
servidor las carpetas completas `radar_bot/`, `shared/` y `tools/` (son las que
usa `traer_oece.py`; no llevan trabajo ajeno sin commitear) encima de su clon,
y correr `tools\traer_oece.py` desde la raíz del proyecto. Para que esa PC
pueda hacer `scp` hay que autorizar su clave pública en
`/root/.ssh/authorized_keys` del servidor (solo entra por clave).

13. **`requirements-puente.txt` — añade `tzdata==2025.2`.** El 2026-10-07, al
    actualizar la PC puente con `shared/` y `tools/` del servidor, el puente
    murió al importar: `shared/fechas.py` hace `ZoneInfo("America/Lima")` y
    Windows no trae la base de zonas horarias. `tzdata` ya estaba en
    `requirements.txt` (servidor) pero no en el del puente. Verificado en un
    contenedor limpio `python:3.12-slim` con solo los requisitos del puente:
    `traer_oece.py` importa `orchestrator`, `ocds_oece` y `shared` sin fallos.
    En la PC puente: `.venv-tarea\Scripts\pip.exe install -r requirements-puente.txt`.
    Dato: el clon del puente es `E:\licitapro-peru-main` (zip de GitHub, sin
    git); su clave SSH quedó autorizada en el servidor ese día.

14. **Portales GORE: diagnóstico por portal + sonda para la PC puente**
    (`radar_bot/scrapers/orchestrator.py`, `tools/sondear_portal.py` nuevo,
    prueba `tests/test_gore_portales.py`). La primera pasada del puente con
    Cusco (2026-10-07 17:40 Lima) anotó "25 encontradas, 11 nuevas, sin
    error"; las 25 eran de Madre de Dios. Cusco devolvió una página sin
    `<table>` y el parser salió con 0 filas en silencio: una sola sonda para
    la fuente entera dejaba que el portal que rinde tape al que no. Ahora cada
    portal de `GORE_COTIZACIONES_PORTALS` tiene su sonda, su línea en el log
    del puente (`GORE Cusco: ...`) y su parte en `error_detalle`
    (`Cusco: SIN EXTRAER ...` / `Cusco: CAIDA ...`). Y `tools/sondear_portal.py`
    se corre en la PC puente para ver qué sirve un portal (código, URL final,
    título, tablas y sus columnas, scripts, enlaces) sin mandar el HTML entero;
    lo guarda en `data/sonda-<host>.html`.

15. **Cusco: el portal de cotizaciones no existe; pasa a
    `GORE_COTIZACIONES_APAGADOS`** (`orchestrator.py`, `ESTADO.md`,
    `DESPLIEGUE.md`, `tools/sondear_portal.py` los sondea también). Sondeado
    desde la PC puente el 2026-10-07 18:20 Lima: https redirige al login de
    Plesk (98 KB, "Plesk Obsidian 18.0.81", cero tablas) y http da la página
    por defecto; lo mismo que ve el VPS. DNS: CNAME a `regioncusco.gob.pe`
    (64.177.118.33), certificado de `mail.regioncusco.gob.pe`; en
    `www.regioncusco.gob.pe/cotizaciones` 404. La nota de gob.pe es de
    noviembre de 2021. No era bloqueo por origen: la aplicación ya no está
    desplegada. Las compras <8 UIT del GORE Cusco llegan por `oece_menores`.
