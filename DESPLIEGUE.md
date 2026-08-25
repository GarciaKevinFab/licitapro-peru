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

## 2. Levantar

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Levanta seis servicios: `postgres`, `redis`, `migraciones` (corre
`alembic upgrade head` y termina), `web`, y los bots `radar`, `prep` y `win`.
La web espera a que las migraciones terminen bien; si fallan, no arranca sobre
un esquema a medias.

Comprobar:

```bash
docker compose -f docker-compose.prod.yml ps
curl -fsS http://127.0.0.1:8200/salud
```

## 3. Proxy con TLS

El servicio `web` publica **solo en 127.0.0.1**, nunca en la interfaz pública.
Delante va un proxy con certificado. Con Caddy es todo lo que hace falta:

```caddyfile
licitapro.pe {
    reverse_proxy 127.0.0.1:8200
}
```

Sin TLS la cookie de sesión viaja en claro y cualquiera en la red puede
robarla. No es opcional.

## 4. Activar cobros con Izipay

Mientras `IZIPAY_MODO=simulado`, el flujo de suscripción funciona completo pero
no cobra nada. Para cobrar de verdad:

1. Consigue del panel de Izipay: `IZIPAY_MERCHANT_CODE`, `IZIPAY_PUBLIC_KEY`,
   `IZIPAY_API_KEY` y la clave de firma del webhook (`IZIPAY_HMAC_KEY`).
2. Pon `IZIPAY_MODO=sandbox` y prueba de punta a punta.
3. Registra la URL del webhook en su panel:
   `https://TU-DOMINIO/webhooks/izipay`
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

## 5. Renovaciones automáticas

Las suscripciones vencidas con tarjeta guardada se renuevan con un cron diario:

```cron
0 9 * * * cd /ruta/licitapro && docker compose -f docker-compose.prod.yml run --rm web python tools/renovar_suscripciones.py >> /var/log/licitapro-renovar.log 2>&1
```

Reintenta una vez al día, hasta 4 veces, antes de suspender. Entre el
vencimiento y la suspensión hay 7 días de gracia en los que el cliente sigue
teniendo acceso: una tarjeta rebota por mil motivos, y cortarle el servicio a
alguien que sí quiere pagar es la forma más cara de perderlo.

## 6. Respaldos

Sin esto, un disco perdido son todos tus clientes perdidos.

```cron
0 3 * * * docker compose -f /ruta/docker-compose.prod.yml exec -T postgres pg_dump -U licitapro licitapro | gzip > /respaldos/licitapro-$(date +\%F).sql.gz
```

**Prueba la restauración al menos una vez.** Un respaldo que nunca se restauró
no es un respaldo, es una suposición.

## 7. Comprobaciones de salud

```bash
docker compose -f docker-compose.prod.yml run --rm web python tools/auditar_sql.py
docker compose -f docker-compose.prod.yml run --rm web python tools/datos_dev.py --verificar-aislamiento
```

El primero valida cada consulta SQL del proyecto contra el esquema real; el
segundo comprueba que ninguna cuenta ve datos de otra. Ambos devuelven código
distinto de cero si algo va mal, así que sirven en CI.

## 8. Antes de abrir al público

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

**n8n comparte la base de datos.** El `docker-compose.yml` de desarrollo levanta
n8n contra la misma base `licitapro`, y sus tablas conviven con las del
producto. No rompe nada hoy, pero conviene separarlas antes de crecer. El
compose de producción no incluye n8n.

**Una sola fuente de licitaciones.** Hoy solo `ocds_oece` entrega convocatorias
vigentes. Si OECE cambia su API, el producto se queda sin datos. Vale la pena
vigilar `scraping_log` y alertar cuando una corrida no traiga nada nuevo.

**Recuperación de contraseña.** Todavía no existe. Si un cliente olvida la suya,
hoy hay que resetearla a mano en la base.
