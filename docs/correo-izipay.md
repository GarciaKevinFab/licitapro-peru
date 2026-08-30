# Correo a Izipay — registro de dominio, Anexo N° 1 y activación

Enviar desde el correo registrado del comercio. Adjuntar nada; si piden
evidencia técnica, se les manda después.

**Para:** el ejecutivo comercial asignado, con copia a soporte/mesa de ayuda de Izipay
**Asunto:** Comercio 5991076 — registro de dominio, Anexo N° 1 y activación de tienda 34481044

---

Estimados:

Escribo en relación al comercio **SOLUCIONES INFORMATICAS MDD (5991076)**, tienda
**34481044**. Necesito resolver tres puntos para poder iniciar operaciones.

**1. Registro del dominio de la plataforma**

Nuestro servicio opera en `https://licitapro.sisac.pe`. Al configurar la URL de la
tienda y las reglas de notificación en el Back Office, el sistema responde:

> "La URL de producción que ha introducido apunta a un dominio desconocido o
> inaccesible desde nuestra plataforma; Por favor, modifique esa URL."

Hemos verificado que el dominio es públicamente accesible:

- Responde HTTP 200 sobre HTTPS con certificado válido.
- Acepta TLS 1.0 a 1.3.
- `GET https://licitapro.sisac.pe/webhooks/izipay` devuelve HTTP 200.
- No existe filtro ni bloqueo para peticiones entrantes.

Adicionalmente, revisamos los registros de nuestro servidor y de nuestro CDN, y
**no figura ninguna petición proveniente de Izipay** en el momento de guardar la
configuración: ni permitida ni bloqueada.

Entendemos, conforme a la cláusula **CUARTA numeral 3** del Contrato de
Afiliación, que la actualización de la información declarada en la Solicitud de
Afiliación se realiza a través de los canales autorizados. Por ello solicitamos
**registrar / actualizar el dominio `licitapro.sisac.pe`** como sitio web del
comercio, para que la plataforma lo reconozca.

Si la verificación se realiza desde direcciones IP determinadas, agradeceré que
nos las indiquen, a fin de descartar de nuestro lado cualquier restricción.

**2. Anexo N° 1 — comisiones y condiciones económicas**

El Contrato de Afiliación remite las comisiones al Anexo N° 1, que no se
encuentra en la copia que recibimos. Solicito su envío antes de la suscripción.

Sobre ese anexo, dos consultas:

- ¿Las tasas indicadas **incluyen** los cargos de las marcas (Visa, Mastercard,
  entre otras) o estos se aplican adicionalmente?
- ¿Cuál es el plazo de abono de los fondos y la política de contracargos
  aplicable a **pagos recurrentes por suscripción**, que es nuestro modelo?

**3. Activación de la tienda**

Solicito confirmar qué requisitos quedan pendientes para habilitar la tienda
34481044 en modo producción, y si la verificación del sitio web forma parte de
ese proceso.

Nuestra integración ya se encuentra desarrollada y en pruebas contra el ambiente
sandbox, a la espera únicamente de que el dominio quede reconocido.

Quedo atento a su respuesta.

Saludos cordiales,

Kevin García
SOLUCIONES INFORMATICAS MDD
Comercio 5991076 — Tienda 34481044
