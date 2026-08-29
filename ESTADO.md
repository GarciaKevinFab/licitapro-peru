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

## 2. Bloqueante para cobrar: Izipay

**El codigo NO falta.** `shared/izipay.py` es un adaptador completo: genera
token de sesion, procesa el webhook verificando la firma HMAC, y
`tools/renovar_suscripciones.py` gestiona las renovaciones. Sin credenciales
cae a modo `simulado`, y el flujo de suscripcion se puede recorrer entero.

Lo que falta es de tu lado, y en este orden:

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
