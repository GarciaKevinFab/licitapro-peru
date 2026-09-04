"""Limite de peticiones por IP para el panel.

QUE HAY YA Y QUE FALTA

  shared/db.py cuenta los intentos fallidos de acceso en la tabla
  `intentos_acceso` y bloquea al que insiste. Eso frena la fuerza bruta contra
  UNA cuenta, y esta bien.

  Lo que no frena es el volumen. Cada intento -- acertado o no -- escribe en
  Postgres antes de que nadie decida nada, asi que el propio freno es un
  amplificador: mil peticiones por segundo contra /entrar son mil escrituras
  por segundo. Y el resto del panel (95 rutas: propuestas, contratos,
  empresas, informes) no tiene ningun limite en absoluto.

  Este modulo pone el techo que faltaba, antes de tocar la base de datos.

LO QUE NO SE LIMITA, Y POR QUE IMPORTA

  Los webhooks. Culqi avisa por /webhooks/culqi de que un cobro salio bien, y
  esos avisos llegan desde un punado de IPs suyas: limitarlos por IP es
  exactamente la forma de descartar la notificacion de un pago que si se
  cobro. Una suscripcion cobrada que el panel no registra es peor que
  cualquier abuso que este limite pudiera evitar. Van exentos a proposito.

POR QUE EN MEMORIA Y NO EN REDIS

  El contenedor `web` corre un solo proceso uvicorn. Con un unico proceso, un
  contador en memoria es exacto y no anade una pieza mas que mantener --
  aunque el compose levante un Redis, hoy no lo usa nadie. Si algun dia se
  levantan varios workers, esto hay que mover a un almacen compartido: cada
  proceso contaria por su cuenta y el limite real seria el configurado
  multiplicado por el numero de procesos.

POR QUE MIDDLEWARE ASGI Y NO BaseHTTPMiddleware

  Un middleware ASGI puro decide antes de llamar a la aplicacion y, si deja
  pasar, no toca la respuesta en absoluto: no interfiere con las descargas de
  informes ni con la cabecera CSP con nonce que app.py calcula por peticion.
"""

import time
from collections import defaultdict, deque


def ip_del_cliente(scope) -> str:
    """La IP real de quien pide, no la del proxy.

    Delante hay Caddy y, delante de Caddy, Cloudflare. El Caddyfile declara los
    rangos de Cloudflare como `trusted_proxies` precisamente para que el
    X-Forwarded-For que llega traiga la IP de verdad, y el contenedor arranca
    uvicorn con --proxy-headers para que la resuelva.

    Se lee la cabecera igualmente en vez de fiarlo todo a `scope["client"]`
    porque este middleware puede acabar montado por encima o por debajo del de
    uvicorn segun como se despliegue, y con la cabecera el resultado es el
    mismo en los dos casos.

    Si esto se rompe, el limite deja de ser por IP y pasa a ser global: el
    primer atacante bloquea a todos los clientes a la vez. Es el mismo fallo
    que ya se dio en CargoXprez, y no se parece a un problema de limites.
    """
    cabeceras = dict(scope.get("headers") or [])
    for nombre in (b"cf-connecting-ip", b"x-forwarded-for"):
        valor = cabeceras.get(nombre)
        if valor:
            # X-Forwarded-For puede traer una cadena; el cliente es el primero.
            return valor.decode("latin-1").split(",")[0].strip()
    cliente = scope.get("client")
    return cliente[0] if cliente else "desconocida"


# Reglas: (metodo o None para cualquiera, prefijo, cuantas, en cuantos segundos).
#
# Gana la PRIMERA que encaja, asi que lo estricto va arriba y el techo al final.
#
#   entrar        Quien se equivoca de contrasena reintenta tres o cuatro
#                 veces. Diez por minuto deja trabajar a esa persona y hace
#                 inviable la fuerza bruta -- y, sobre todo, corta el chorro de
#                 escrituras a `intentos_acceso` antes de que llegue a Postgres.
#   registro      Una empresa se da de alta una vez.
#   recuperar     El correo de recuperacion cuesta dinero y es un arma para
#                 inundar el buzon de otro.
#   comprar       Se compra una vez, no veinte.
#   reclamaciones El libro de reclamaciones es obligatorio y publico, o sea
#                 que es el buzon que cualquiera puede llenar de basura.
#   escrituras    Nadie rellena formularios a mas de dos por segundo.
#   techo         Diez por segundo sostenidos por IP. Una empresa entera sale
#                 por la misma IP publica y aun asi navegando no se acerca.
#                 Existe para que nadie pueda tumbar el panel, no para
#                 racionar el uso.
REGLAS = [
    ("POST", "/entrar", 10, 60),
    ("POST", "/registro", 5, 3600),
    ("POST", "/recuperar", 5, 3600),
    ("POST", "/comprar", 10, 3600),
    ("POST", "/reclamaciones", 5, 3600),
    ("POST", None, 120, 60),
    ("PUT", None, 120, 60),
    ("PATCH", None, 120, 60),
    ("DELETE", None, 120, 60),
    (None, None, 600, 60),
]

# Aqui no hay prefijo /api que separe: el panel sirve paginas HTML desde la
# raiz. Asi que se cuenta TODO menos lo de abajo.
PREFIJOS = ("/",)

# Exentas:
#
#   /webhooks  Culqi y WhatsApp avisan desde sus propias IPs. Ver la cabecera.
#   /static    Una pagina arrastra varios ficheros; contarlos gastaria el cubo
#              sin que nadie haya pedido nada. Ademas Cloudflare los cachea
#              cuatro horas, asi que ni llegan aqui la mayoria de las veces.
#   /salud     El healthcheck del Dockerfile, cada pocos segundos desde dentro.
EXENTAS = ("/webhooks", "/static", "/salud")


class LimitePeticiones:
    """Middleware ASGI que cuenta peticiones por (IP, regla) en ventana deslizante."""

    def __init__(self, app, reglas=REGLAS, prefijos=PREFIJOS, exentas=EXENTAS):
        self.app = app
        self.reglas = reglas
        self.prefijos = tuple(prefijos)
        self.exentas = tuple(exentas)
        # (ip, indice de regla) -> deque de instantes
        self._marcas = defaultdict(deque)
        self._ultima_purga = time.monotonic()

    def _regla_para(self, metodo: str, ruta: str):
        for indice, (met, prefijo, cuantas, ventana) in enumerate(self.reglas):
            if met is not None and met != metodo:
                continue
            if prefijo is not None and not ruta.startswith(prefijo):
                continue
            return indice, cuantas, ventana
        return None

    def _purgar(self, ahora: float) -> None:
        """Tira los cubos que ya no cuentan nada.

        Sin esto, el diccionario crece con una entrada por cada IP que haya
        pasado alguna vez -- que en un panel publico es memoria que solo sube.
        Cada minuto basta: las ventanas mas largas son de una hora, y esas se
        vacian solas cuando les toca.
        """
        if ahora - self._ultima_purga < 60:
            return
        self._ultima_purga = ahora
        for clave in [c for c, marcas in self._marcas.items() if not marcas]:
            del self._marcas[clave]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        ruta = scope.get("path", "")
        if not ruta.startswith(self.prefijos) or ruta.startswith(self.exentas):
            return await self.app(scope, receive, send)

        regla = self._regla_para(scope.get("method", "GET"), ruta)
        if regla is None:
            return await self.app(scope, receive, send)
        indice, cuantas, ventana = regla

        clave = (ip_del_cliente(scope), indice)

        ahora = time.monotonic()
        marcas = self._marcas[clave]
        limite = ahora - ventana
        while marcas and marcas[0] <= limite:
            marcas.popleft()

        if len(marcas) >= cuantas:
            # Cuanto falta para que la mas antigua salga de la ventana.
            espera = max(1, int(marcas[0] + ventana - ahora) + 1)
            # El panel devuelve paginas, no JSON: quien se tope con esto es una
            # persona con un navegador delante, y merece una frase que se
            # entienda en vez de un volcado.
            cuerpo = (
                "<!doctype html><html lang=\"es\"><meta charset=\"utf-8\">"
                "<title>Demasiadas peticiones</title>"
                "<body style=\"font-family:system-ui,sans-serif;max-width:34rem;"
                "margin:15vh auto;padding:0 1.5rem;line-height:1.6\">"
                "<h1>Demasiadas peticiones</h1>"
                "<p>Se han hecho muchas peticiones seguidas desde esta conexion. "
                f"Vuelve a intentarlo en {espera} segundos.</p>"
                "</body></html>"
            ).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(cuerpo)).encode("latin-1")),
                    # Retry-After es la parte que un cliente educado puede
                    # obedecer sola. Sin ella, reintentar en bucle es lo
                    # razonable desde fuera y el 429 no calma nada.
                    (b"retry-after", str(espera).encode("latin-1")),
                ],
            })
            await send({"type": "http.response.body", "body": cuerpo})
            return

        marcas.append(ahora)
        self._purgar(ahora)
        return await self.app(scope, receive, send)
