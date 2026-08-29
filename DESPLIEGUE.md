# Despliegue

Guía para poner LicitaPro en un servidor. Asume Docker y Docker Compose.

## 1. Preparar el `.env`

```bash
cp .env.example .env
```

Genera la clave maestra y la contraseña de la base:

```bash
python -c "import secrets; print('LICITAPRO_SECRET_KEY=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
```

**`LICITAPRO_SECRET_KEY` es la pieza más delicada del sistema.** Cifra las
credenciales que guardan tus clientes y firma las cookies de sesión. Si la
pierdes, todo lo cifrado queda irrecuperable y cada usuario tendrá que volver a
cargar sus credenciales. Guárdala en un gestor de secretos, no solo en el
servidor. Y **no la rotes sin re-cifrar**: el código detecta la credencial
ilegible y avisa, pero el dato no vuelve.

Fuera de desarrollo la aplicación **se niega a arrancar** sin ella. Es
deliberado: descubrir que falta después de migrar de servidor sería tarde.

Pon también `LICITAPRO_ENTORNO=produccion`, que además marca la cookie de
sesión como `https_only`.

## 2. El dominio

Va como **subdominio de `sisac.pe`**, que ya está activo en Cloudflare:

    licitapro.sisac.pe

No hace falta comprar nada. Y para Izipay es mejor que un dominio recién
registrado: verifican la web desde fuera y piden que corresponda al titular de
la cuenta — un subdominio de la web de la propia empresa lo cumple solo.

Si más adelante quieres `licitapro.pe` a secas (mejor para decirlo por
teléfono), se registra en [punto.pe](https://punto.pe) por S/110 al año —
Cloudflare no vende `.pe`, solo gestiona su DNS — y se redirige aquí. No corre
prisa y no cambia nada de lo de abajo.

## 3. Publicar: Cloudflare Tunnel

El mismo montaje que ya corre en VueloRadar.

```
   visitante → Cloudflare (DNS · TLS · WAF) → túnel saliente → VPS
```

El VPS **no abre ningún puerto**. `cloudflared` abre la conexión hacia afuera,
así que no hay 80/443 en el cortafuegos, no hay IP de origen que alguien pueda
descubrir y atacar, y no hay certificados que renovar: el TLS termina en
Cloudflare.

Un mismo túnel sirve varios proyectos. Si el de VueloRadar ya está montado,
aquí solo se añade otro *hostname*.

1. Cloudflare → **Zero Trust → Networks → Tunnels**. Reutiliza el túnel
   existente o crea uno nuevo (*Create a tunnel → Docker*).
2. En *Public Hostnames*: `licitapro.sisac.pe` → y aquí **el servicio depende
   de dónde corra `cloudflared`**. Equivocarse no da un error legible: da un
   502 de Cloudflare, sin nada en los logs del panel.

   | `cloudflared` corre… | Servicio | Cómo se levanta |
   |---|---|---|
   | en el **host** (systemd) | `http://localhost:8200` | `…up -d` |
   | como **contenedor** | `http://web:8200` | `…--profile tunnel up -d` |

   `web:8200` solo resuelve dentro de la red de compose. Si `cloudflared` está
   en el host, ese nombre no existe para él; lo que sí alcanza es el puerto que
   `web` publica en `127.0.0.1:8200`.

   Compruébalo antes de elegir:

   ```bash
   systemctl is-active cloudflared
   ```

   **En este VPS corre en el host**: servicio `http://localhost:8200`, y el
   stack se levanta **sin** `--profile tunnel`.
3. Solo si `cloudflared` va como contenedor: copia el token al `.env`
   (`CLOUDFLARE_TUNNEL_TOKEN=...`). Con el servicio del host no hace falta.
4. El CNAME lo crea Cloudflare solo, ya proxeado.

En **SSL/TLS elige «Full (strict)»**, nunca «Flexible». Con Flexible, Cloudflare
habla http con el origen mientras la app manda HSTS, y el navegador del
visitante se queda atrapado.

### Sin Cloudflare: `--profile proxy`

Levanta Caddy con Let's Encrypt. Ahí sí hace falta que el DNS apunte a la IP
**antes** de levantar el compose —la validación entra desde fuera por el 80— y
que los puertos 80 y 443 estén abiertos. Comprueba la propagación primero:

```bash
dig +short licitapro.sisac.pe
```

El `Caddyfile` trae los rangos de Cloudflare como proxies de confianza, por si
lo pones detrás de la nube naranja: sin eso la app vería a todos los visitantes
con la IP del borde de Cloudflare, y el freno a la fuerza bruta bloquearía a
muchos usuarios de golpe por culpa de uno solo.

## 4. Levantar

### Antes, si la base está vacía: el esquema base

```bash
docker compose -f docker-compose.prod.yml run --rm -T --entrypoint python migraciones tools/crear_esquema.py
```

**Una sola vez, y solo sobre una base nueva.** `0001_baseline` no crea nada a
propósito — da por hecho que el esquema ya existe — así que sin este paso la
`0002` intenta `ALTER TABLE empresas` sobre una tabla que nadie creó y el
despliegue se para con `relation "empresas" does not exist`, que no apunta a
`shared/schema.sql` por ningún lado. Salta esto si la base ya tiene sus tablas.

### Y ya sí, el stack

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Añade `--profile tunnel` **solo** si `cloudflared` va como contenedor (ver §3).

Cinco servicios: `redis`, `migraciones` (corre `alembic upgrade head` y
termina), `web`, y los bots `radar`, `prep` y `win`. **No hay `postgres`**: en
producción la base es Supabase y vive fuera del servidor. La web espera a
que las migraciones terminen bien; si fallan, no arranca sobre un esquema a
medias.

Comprobar, en este orden:

```bash
docker compose -f docker-compose.prod.yml --profile tunnel ps
```

```bash
curl -fsS http://127.0.0.1:8200/salud
```

```bash
curl -fsSI https://licitapro.sisac.pe/salud | head -1
```

Si el segundo responde y el tercero no, el problema está en el túnel o en el
hostname del panel de Cloudflare:

```bash
docker compose -f docker-compose.prod.yml --profile tunnel logs tunnel | tail -30
```

## 5. Qué asoma a internet

Con el túnel, **nada**. Ni un puerto abierto. `web` publica en `127.0.0.1:8200`
para poder depurar desde el propio servidor, y eso no se alcanza desde fuera.

Cierra todo salvo SSH en el cortafuegos del proveedor. Si dejas el 5432
abierto, tarde o temprano alguien prueba `postgres/postgres` contra él.

## 6. Nada de caché sobre el HTML

El panel es por sesión. Si Cloudflare guardara `/panel`, un cliente vería las
licitaciones y los contratos de otro.

Por defecto Cloudflare no cachea HTML, así que **no hace falta ninguna regla**.
Lo que hace falta es **no** crear una de «Cache Everything» sobre este
hostname. Como segunda barrera, la app manda `Cache-Control: private, no-store`
en todo lo que no sea `/static`, para no depender de una configuración que vive
fuera de este repositorio.

(En VueloRadar es al revés, y con razón: es un sitio público que vive de que lo
rastreen. Aquí no hay nada público que cachear salvo la portada.)

## 7. Una trampa que cuesta horas: el `$` en las contraseñas

Docker Compose **interpola** las variables del `.env` que aparecen como
`${VAR}` — aquí son `POSTGRES_PASSWORD`, `POSTGRES_USER`, `POSTGRES_DB`,
`LICITAPRO_DOMINIO` y `CLOUDFLARE_TUNNEL_TOKEN`.

Un `$` dentro de la contraseña se come el resto sin avisar: Postgres arranca
con una contraseña distinta de la que tú crees, y el error no dice por qué.
Genera contraseñas sin `$` ni `%`:

```bash
python3 -c "import secrets,string; a=string.ascii_letters+string.digits+'!@#^&*(-_=+)'; print(''.join(secrets.choice(a) for _ in range(40)))"
```

`LICITAPRO_SECRET_KEY` no sufre esto: `secrets.token_urlsafe` solo produce
letras, dígitos, `-` y `_`.

## 8. Activar cobros con Izipay

Mientras `IZIPAY_MODO=simulado`, el flujo de suscripción funciona completo pero
no cobra nada. Para cobrar de verdad:

1. Consigue del panel de Izipay: `IZIPAY_MERCHANT_CODE`, `IZIPAY_PUBLIC_KEY`,
   `IZIPAY_API_KEY` y la clave de firma del webhook (`IZIPAY_HMAC_KEY`).
2. Pon `IZIPAY_MODO=sandbox` y prueba de punta a punta.
3. Registra la URL del webhook en su panel:
   `https://licitapro.sisac.pe/webhooks/izipay`
4. Cuando funcione en sandbox, cambia a `IZIPAY_MODO=produccion`.

Si faltan `MERCHANT_CODE` o `API_KEY`, el sistema **vuelve solo a simulado** en
vez de intentar cobrar a medias.

### Lo que falta confirmar con tu cuenta de comercio

`shared/izipay.py` está marcado con `POR_CONFIRMAR` en tres sitios. Sondeando su
API sin credenciales se verificó: los hosts, que el endpoint de token es
`POST /security/v1/Token/Generate`, y el envoltorio `{code, message, response}`.
Lo que **no** se pudo verificar, porque la API devuelve el mismo error genérico
ante cualquier cuerpo:

- Los nombres exactos de los campos del cuerpo de `Token/Generate`.
- El endpoint y el cuerpo del cobro recurrente con tarjeta tokenizada.
- El algoritmo de firma del webhook (se implementó HMAC-SHA256, lo habitual).

Reserva medio día para ajustar esos tres puntos contra su documentación real.
Están aislados en un solo archivo justamente para eso.

## 9. Renovaciones automáticas

Las suscripciones vencidas con tarjeta guardada se renuevan con un cron diario:

```cron
0 9 * * * cd /ruta/licitapro && docker compose -f docker-compose.prod.yml run --rm web python tools/renovar_suscripciones.py >> /var/log/licitapro-renovar.log 2>&1
```

Reintenta una vez al día, hasta 4 veces, antes de suspender. Entre el
vencimiento y la suspensión hay 7 días de gracia en los que el cliente sigue
teniendo acceso: una tarjeta rebota por mil motivos, y cortarle el servicio a
alguien que sí quiere pagar es la forma más cara de perderlo.

## 10. Respaldos

Sin esto, un disco perdido son todos tus clientes perdidos.

```cron
0 3 * * * docker compose -f /ruta/docker-compose.prod.yml exec -T postgres pg_dump -U licitapro licitapro | gzip > /respaldos/licitapro-$(date +\%F).sql.gz
```

**Prueba la restauración al menos una vez.** Un respaldo que nunca se restauró
no es un respaldo, es una suposición.

## 11. Comprobaciones de salud

```bash
docker compose -f docker-compose.prod.yml run --rm web python tools/auditar_sql.py
docker compose -f docker-compose.prod.yml run --rm web python tools/datos_dev.py --verificar-aislamiento
```

El primero valida cada consulta SQL del proyecto contra el esquema real; el
segundo comprueba que ninguna cuenta ve datos de otra. Ambos devuelven código
distinto de cero si algo va mal, así que sirven en CI.

## 12. Antes de abrir al público

- [ ] `LICITAPRO_SECRET_KEY` generada y guardada fuera del servidor
- [ ] `LICITAPRO_ENTORNO=produccion`
- [ ] `POSTGRES_PASSWORD` cambiada (la de desarrollo era `licitapro2026`)
- [ ] TLS delante del puerto 8200
- [ ] `TELEGRAM_BOT_USERNAME` con el bot real
- [ ] Respaldos programados **y una restauración probada**
- [ ] Webhook de Izipay registrado y con `IZIPAY_HMAC_KEY`
- [ ] Términos de servicio y política de privacidad publicados
      (Ley 29733: guardas RUC, DNI y firmas de terceros)

## Notas

**Zona horaria.** Los contenedores corren en `America/Lima` y el pool de la base
fija esa zona. No es cosmético: con el contenedor en UTC, un token de Telegram
nace caducado y una licitación se da por vencida cinco horas antes de cerrar.

**n8n se retiró.** Duplicaba a los planificadores de `radar_bot` y `win_bot`, y
lo hacía peor: `/api/daily-summary` usaba el flag global `licitaciones.notificado`,
la lógica de un solo inquilino que se reemplazó porque quemaba cada licitación
para todos los clientes menos el primero. Se borraron `shared/api_server.py`,
`n8n-workflows/` y el servicio del compose de desarrollo.

Si tu base de desarrollo es la de siempre, las ~40 tablas que n8n dejó
(`execution_entity`, `credentials_entity`, `workflow_entity`…) siguen ahí. Ya no
las usa nadie; borrarlas es opcional y conviene mirarlas antes.

**Una sola fuente de licitaciones.** Hoy solo `ocds_oece` entrega convocatorias
vigentes. Si OECE cambia su API, el producto se queda sin datos, y el fallo es
silencioso: el scraper no revienta, devuelve cero, y el panel sigue enseñando lo
viejo.

`shared/vigilancia.py` lo detecta y avisa por Telegram a `TELEGRAM_ADMIN_ID`
tras 12 corridas seguidas sin novedades, y luego una vez al día mientras dure.
El umbral está en `UMBRAL_CORRIDAS`: por debajo de 12 saltaría cada fin de
semana, y un aviso que salta cada sábado deja de leerse.

## Análisis con IA

Lo pagas tú, con una sola `ANTHROPIC_API_KEY`; el cliente no configura nada.

**Lo que cuesta, medido:** unos 500 tokens de entrada y 1.700 de salida por
análisis de viabilidad, ~US$0,046 (S/0,17) con `claude-opus-5`. Una propuesta
técnica sale más cara, ~4.900 tokens de salida, unos US$0,13 (S/0,47).

**El tope** vive en `planes.analisis_ia_mes` y se cambia sin desplegar:

```sql
SELECT codigo, precio_mensual, analisis_ia, analisis_ia_mes FROM planes;
UPDATE planes SET analisis_ia_mes = 120 WHERE codigo = 'pro';
```

Sale en 60 al mes para Pro y 300 para Empresa. Ese segundo número merece un
repaso: 300 análisis son ~S/51 de los S/199 del plan, un 26% del precio. Si un
cliente lo agota de verdad todos los meses, el margen no da lo que parece.

**Para gastar menos** sin tocar código: baja `LICITAPRO_IA_ESFUERZO` o cambia
`LICITAPRO_MODELO_IA` a `claude-sonnet-5`, unas 2,5 veces más barato por token.
Los análisis pierden finura — distinguir "consultoría de obra" de "ejecución de
obra" es justamente el tipo de matiz por el que se paga.

**Para saber el gasto real:** `shared.ia.gasto_del_mes()` devuelve análisis,
usuarios y tokens del mes en curso. No los convierte a dinero a propósito: la
tarifa cambia sin avisar a este código.

Sin `ANTHROPIC_API_KEY` el producto sigue funcionando. La ficha muestra el
análisis por reglas, marcado en pantalla como tal para que nadie crea que pagó
por el otro.

**Recuperación de contraseña.** Funciona por correo, así que **necesita SMTP
configurado** (`SMTP_USER` y `SMTP_PASSWORD`). Sin SMTP el enlace no se envía a
ningún lado: queda escrito en el log del servidor, lo cual sirve para desarrollar
pero deja a tus clientes sin poder recuperar su cuenta. Configúralo antes de abrir.

Los enlaces vencen en una hora, sirven una sola vez, y usar uno invalida los
demás del mismo usuario. Se guarda el SHA-256 del token, nunca el token: si
alguien se lleva un volcado de la base, los enlaces pendientes no le sirven.
