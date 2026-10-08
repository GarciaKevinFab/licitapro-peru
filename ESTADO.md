# Estado de LicitaPro

Lo comprobado el 2026-08-29 contra el sistema real, no contra lo que deberia
ser. Cada afirmacion sale de haberla ejecutado; donde no pude, lo digo.

---

## 1. Vivo y verificado

| Pieza | Estado |
|---|---|
| `https://licitapro.sisac.pe` | HTTP 200, TLS valido (`*.sisac.pe`, Google Trust Services) |
| Tunel Cloudflare | Ruta `licitapro.sisac.pe -> http://localhost:8200` sobre el tunel `vueloradar` |
| Base de datos | Supabase `us-east-2`, via **transaction pooler** (6543, IPv4) |
| Migraciones | `0001` -> `0012` aplicadas |
| Servicios | `web` (healthy), bots `radar` / `prep` / `win`, `redis` |
| Cabeceras | HSTS, CSP con nonce por peticion, `X-Frame-Options: DENY` |
| `robots.txt` y `sitemap.xml` | Sirviendo |

---

## 2. Bloqueante para cobrar: las llaves de Culqi

**El codigo NO falta.** `shared/culqi.py` es un adaptador completo -- planes,
cliente, tarjeta, suscripcion, cancelacion y lectura de cargos --,
`web/webhooks_culqi.py` recibe los avisos y los COMPRUEBA contra la API antes
de dar un pago por bueno, y `tools/culqi_planes.py` crea los planes. Sin llaves
cae a modo `simulado` y el flujo entero se puede recorrer sin cobrar nada.

Por que Culqi y no Izipay: Izipay confirmo por escrito que la afiliacion admite
**solo pagos unicos**, no recurrentes, y que cada dominio necesita afiliacion
propia. Con eso, "suscripcion" era una palabra en la portada. Culqi cobra sola
cada periodo y avisa por webhook.

Lo que falta es de tu lado, y en este orden:

- [ ] **Llaves de prueba de Culqi** (`pk_test_…`, `sk_test_…`) en el `.env`, con
      `CULQI_MODO=prueba`.
- [ ] **Comprobar el POR_CONFIRMAR que de verdad importa**: que valor de
      `interval_unit_time` es mensual y cual anual. La documentacion publica
      solo ensena `1` y no dice a que unidad corresponde. Se comprueba en cinco
      minutos creando un plan sonda con cada valor; el `curl` exacto esta en
      `shared/culqi.py` -> `intervalo_de()`. **No hay valor por defecto a
      proposito**: adivinar no da error, da un plan mensual que cobra cada dia.
- [ ] `python tools/culqi_planes.py` y luego `--aplicar`.
- [ ] Dar de alta el webhook en el panel de Culqi:
      `https://licitapro.sisac.pe/webhooks/culqi`
- [ ] Comprar de punta a punta con tarjeta de prueba y comprobar que
      `/suscripcion` queda activa con vencimiento a un periodo vista.
- [ ] Cambiar las DOS llaves por las `pk_live_`/`sk_live_` y poner
      `CULQI_MODO=produccion`. Con las de prueba se niega a arrancar: Culqi las
      aceptaria y crearia suscripciones que no mueven dinero.

---

## 2.bis Izipay: el pago unico, que sigue vivo

Se conserva entero. La afiliacion sigue activa, hay cobros registrados con su
numero de orden y pagos manuales -- efectivo, Yape, transferencia -- en el mismo
historial. Mientras Culqi este en simulado, el checkout es el suyo y los textos
del sitio dicen "pago unico", que es lo que ocurriria.

Lo que falta para usarlo, en este orden:

- [ ] **Cuenta de comercio de Izipay aprobada.** Verifican la web desde fuera y
      exigen dominio propio con HTTPS valido. Eso ya lo tienes: era el requisito
      que bloqueaba empezar el tramite.
- [ ] Rellenar `IZIPAY_MERCHANT_CODE`, `IZIPAY_PUBLIC_KEY`, `IZIPAY_API_KEY`,
      `IZIPAY_HMAC_KEY` en el `.env` del servidor.
- [ ] **Confirmar los tres POR_CONFIRMAR** que el propio adaptador declara y que
      no se pueden deducir sondeando la API:
      - nombres exactos de los campos de `POST /security/v1/Token/Generate`
      - endpoint y cuerpo del cobro recurrente con tarjeta tokenizada
      - algoritmo exacto de firma del webhook (IPN)
- [ ] Dar de alta la URL del webhook en el panel de Izipay:
      `https://licitapro.sisac.pe/webhooks/izipay`
- [ ] Probar en **sandbox** (`https://sandbox-api-pw.izipay.pe`) antes de
      produccion.

> Riesgo a tener presente: IFS anuncio en 2025 la absorcion de Izipay dentro de
> Interbank. Que el contrato de la API cambie no es hipotetico. Por eso todo lo
> especifico del proveedor vive en un solo archivo.

---

## 3. Configuracion pendiente

- [ ] **`TELEGRAM_BOT_USERNAME`** — vacia. El enlace de vinculacion cae al
      default `LicitaRadar_SI_bot`. No da error: simplemente no vincula a nadie.
- [ ] **SMTP** — `smtp.hostinger.com:587` (STARTTLS) verificado abierto y
      respondiendo. Falta solo `SMTP_PASSWORD` del buzon `kevinfge@sisac.pe`.
      Sin esto el enlace de recuperacion de contrasena **no se envia**: queda
      escrito en el log del servidor.
- [ ] **Cuenta de admin** — registrar en `/registro` con `kevinfge@sisac.pe`,
      que es lo que `LICITAPRO_ADMIN_EMAIL` espera. Sin coincidencia exacta,
      `/admin/ia` responde 404 tambien a ti.
- [ ] **WhatsApp** (4 variables) — opcional. El webhook y las notificaciones
      estan escritos; falta el numero y el token permanente de Meta.

---

## 4. Seguridad y operacion

- [ ] **Los bots escriben su token de Telegram en cada linea de log.** `httpx`
      loguea la URL completa a nivel INFO, y el token va en la URL. Tres tokens
      en claro en los logs. Se corrige bajando `httpx` a WARNING.
- [ ] **Sin backup de la base.** VueloRadar ya tiene `pg_dump` diario a R2;
      LicitaPro no tiene ninguno. El dato vive solo en Supabase, cuyo plan
      gratuito no da restauracion a demanda.
- [ ] **Sin Sentry ni alertas.** Si `web` se cae de madrugada, nadie se entera
      hasta que un cliente lo dice.
- [ ] Rotar `LICITAPRO_SECRET_KEY` si alguna vez estuvo en un repo o un chat:
      de ella se deriva la clave que cifra las credenciales SEACE de tus
      clientes.

---

## 5. SEO y marca

- [x] `sitemap.xml` con las 4 paginas publicas, y `robots.txt` excluyendo el area privada
- [ ] **Enviar el sitemap en Search Console**: pegar `sitemap.xml` en
      *Indexacion -> Sitemaps*. Sigue vacio.
- [ ] **Favicon y logo** — no existen. El prompt esta en `docs/prompt-marca.json`
      con los 4 entregables y los colores exactos de la app.
- [ ] **`og:image`** — al pegar el enlace en WhatsApp no sale nada. En Peru eso
      es *el* canal por el que se va a compartir.
- [ ] Decidir sobre el `robots.txt` gestionado de Cloudflare, que hoy bloquea
      `GPTBot`, `ClaudeBot`, `CCBot` y `Google-Extended`. Google Search entra;
      las respuestas de IA no te citaran.

---

## 6. Calidad

- 27 pruebas en 5 archivos. `test_calculos.py` concentra 24; el area web y los
  bots estan practicamente sin cubrir.
- `pytest` no esta en la imagen de produccion (correcto), asi que las pruebas
  no se pueden correr en el servidor. Van en `requirements-dev.txt`.
- `HEAD /` devuelve 405 (`GET` va bien). Un monitor de uptime configurado con
  HEAD daria caida falsa.

---

## 6.bis Fuentes y cobertura regional (revisado 2026-10-07)

Lo que entra hoy, medido contra la base:

| Fuente | Que trae | Desde donde | Estado |
|---|---|---|---|
| `ocds_oece` (API OCDS de SEACE) | Licitaciones de todo el pais | Puente peruano, cada 4 h | Lee bien, pero OECE lleva **5 dias sin publicar nada nuevo** en su API (ultima convocatoria del 02/10). El vigilante no lo veia: media la ultima lectura, no la fecha del dato. Ahora si (`horas_de_rezago`, aviso "rezago", `oece_rezago_horas` en `/salud`). |
| `gore_portals` Madre de Dios | Compras <8 UIT del GORE | Puente | 83 nuevas en 48 h, 68 vigentes. |
| `gore_portals` **Cusco** | Compras <8 UIT del GORE | — (apagado) | **No existe.** gob.pe enlaza `cotizaciones.regioncusco.gob.pe` (nota de 2021), pero el subdominio es un CNAME al servidor principal sin sitio configurado: desde Peru (puente, 2026-10-07) y desde el VPS da lo mismo, el login de Plesk. Queda en `GORE_COTIZACIONES_APAGADOS` con el motivo. Las compras menores del GORE Cusco entran por `oece_menores`. |
| `gob_pe` | Solicitudes de cotizacion nacionales | VPS, cada hora | 13 nuevas en 48 h. |
| **`oece_menores`** (herramienta del OECE para contratos menores, Ley 32069) | **Compras <8 UIT de las 25 regiones**: 2.445 vigentes el 2026-10-07 (Lima 714, Arequipa 281, La Libertad 195, Cusco 130, Junin 127, Puno 56, Madre de Dios 16, Tumbes 2); 356 entidades la usan | VPS, cada hora | **Nuevo.** API publica sin sesion (`prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico`), se recorre por departamento. Sin monto (no lo publica) ni bases (404 sin sesion). Es la fuente nacional de compras menores que los portales regionales solo cubrian a trozos. |
| `ima_cusco` | Compras <8 UIT del IMA (GORE Cusco) | VPS | Escrito y probado, **apagado**: la entidad no publica desde el 09/02/2026. |

Revisado y descartado para las regiones de los clientes (Madre de Dios,
Junin, Cusco, Puno): GORE Puno, GORE Junin y las municipalidades
provinciales de Huancayo, San Roman (Juliaca), Puno y Cusco no tienen portal
de cotizaciones. Peru Compras (catalogos) y los datos abiertos de OECE sirven
para inteligencia de competidores y PAC, no para alertas.

**Calidad de `departamento`.** El detector de OECE comparaba por subcadena y
"ica" esta dentro de publICA/tecnICA/electrICA: el GORE Puno tenia 50
procesos etiquetados como Ica. Corregido en `_departamento`; las filas
guardadas se arreglan con `tools/reparar_departamentos.py --aplicar`, que
ademas completa el departamento por RUC de la entidad (regla en
`shared/departamentos.py`, que corre tambien al final de cada pasada).

Segunda pasada el mismo dia, revisando las 25 regiones: 1) la entidad manda
sobre el objeto (51 filas tenian la region de la obra y no la del comprador);
2) las municipalidades y UGEL se ubican por la tabla oficial de ubigeos
(`shared/ubigeo.py` + `shared/ubigeo.csv`, 1.893 distritos): "MUNICIPALIDAD
PROVINCIAL DE ESPINAR" no nombra a Cusco y "SAN MARTIN DE PORRES" no es San
Martin. Resultado del dia: 1.010 filas corregidas, 538 + 351 completadas; sin
region quedan 1.500 de 8.300, y de esas ~1.050 son entidades nacionales
(Ejercito, EsSalud, CENARES, ONPE...) que no tienen region y se muestran a
todos a proposito.

**Candidatas revisadas para cosechar (2026-10-07), ademas de las de la tabla:**

- PNSR (Programa Nacional de Saneamiento Rural, Vivienda):
  `aplicacionespnsr.vivienda.gob.pe/spsPNSR/consulta`, compras <8 UIT con
  filtro por departamento. La tabla la carga `POST
  Consulta/jsonListadoProcesos?dpto=&estado=1`, pero desde el VPS responde 500
  tras un WAF (cookies HWWAF). Pendiente de probar desde el puente.
- DIRESA Madre de Dios: `cotizaciones.diresamdd.gob.pe` es una app React cuya
  API es `https://cotizador.snipmania.tech/api/v1/convocatorias/public` (JSON
  limpio, responde al VPS). Tiene UNA convocatoria, de junio 2026, finalizada.
  No se cosecha hasta que publiquen; son 60 lineas cuando toque.
- gob.pe: se anadio la consulta "invitacion a cotizar" (54 publicaciones en 7
  dias que no entraban: EMSAPUNO, SEDAM Huancayo, municipalidades).
- Portales 8 UIT de SUNARP, MINSA, SENAMHI, Poder Judicial: bloquean al VPS
  (Cloudflare) o cargan por JavaScript sin API visible. UNSAAC, UNCP, UNAMAD,
  DIRESA Puno/Junin, SEDAM: no publican compras menores en su web.

## 6.ter El flujo de despues de ganar (revisado 2026-10-07)

Como funciona hoy, de punta a punta:

1. **Alerta** de la licitacion (correo / Telegram / WhatsApp segun el usuario).
2. **Propuesta**: `/postular` la abre ('iniciado'); el prep_bot o la web generan
   el expediente ('listo'). No hay un paso "la presente": 'listo' es lo mas
   cerca que se sabe.
3. **Parece que ganaste**: `win_bot` cruza cada 30 min el nombre del
   adjudicatario (API OCDS; el RUC no viene) con las empresas del usuario que
   tengan propuesta 'listo'/'enviado', y avisa por sus canales. No crea el
   contrato: lo confirma el usuario en `/contratos/ganar`.
4. **Contrato**: timeline de plazos (firma, fianza, entrega...) y aviso 3 dias
   antes de cada uno al dueno del contrato.
5. **Cobro**: se registra la factura y la **fecha de conformidad**; la fecha
   limite legal son 10 dias habiles desde la conformidad (feriados calculados).
   Pasado el plazo (y la prorroga de 5 dias), aviso diario "ya puedes
   reclamar"; el usuario marca "cobrado" cuando entra el dinero.

Lo que falta para que esto sea redondo:

- [ ] Una **carta de requerimiento de pago** a la entidad (hoy el aviso dice
      "ya puedes reclamar" y ahi acaba; `/reclamaciones` es otra cosa).
- [ ] Un boton "**la presente**" en la propuesta, para no depender de 'listo'.
- [ ] Para contratos menores (<8 UIT) la API publica no trae ganador: el
      "gane" lo marca el usuario a mano.

## 7. Frente a la competencia

Las dos referencias en Peru son **LicitaLAB** y **LiciSoft**.

Lo que ellos tienen y aqui no:

- [ ] **Inteligencia de competidores**: quien gana que, a que precio, con que
      entidad. Es lo que mas venden ellos y lo que un proveedor mas valora,
      porque decide a que no presentarse. La tabla `historico_precios` ya existe.
- [ ] **Perú Compras / Catalogos Electronicos** como segunda fuente. Hoy solo
      SEACE. Muchas PYMES venden mas por catalogo que por licitacion.
- [ ] **Informes de entidades compradoras**: cuanto compra cada una, cada cuanto,
      a quien.

Donde LicitaPro ya gana:

- Alertas por **Telegram** con explicacion del porque de cada una. Ellos mandan
  correo y WhatsApp; el correo se ignora.
- **Preparacion del expediente** y **cobro del contrato**: ellos se paran en
  avisar. Aqui hay `prep_bot` y `win_bot`, que cubren desde el aviso hasta que
  te pagan. Ese es el argumento diferencial, y hoy no se cuenta en la portada.

> Idea barata y de alto impacto: la portada explica que detecta licitaciones.
> No explica que ademas prepara la propuesta y persigue el cobro, que es
> justo lo que nadie mas hace.
