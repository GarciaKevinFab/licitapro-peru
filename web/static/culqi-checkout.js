/* Culqi Checkout: del boton de pagar al token de la tarjeta.
 *
 * QUE HACE Y QUE NO
 *
 *   Abre el formulario de tarjeta de Culqi, recoge el `tkn_…` que devuelve y
 *   lo mete en un campo oculto del formulario, que despues se envia al
 *   servidor. La tarjeta no pasa por aqui ni por nuestro servidor: viaja del
 *   navegador a Culqi. Eso es lo que mantiene a LicitaPro fuera del alcance de
 *   PCI-DSS, y por eso este archivo no toca ni un digito de la tarjeta.
 *
 * NADA EN LINEA
 *
 *   La CSP no admite 'unsafe-inline' en script-src. Todo el comportamiento
 *   vive aqui y la configuracion llega por data-* en el propio formulario, no
 *   por un <script> con variables incrustadas.
 *
 * SE ESCUCHA EN FASE DE CAPTURA, Y ES DELIBERADO
 *
 *   licitapro.js tiene un manejador de `submit` que deshabilita el boton
 *   ("data-cargando"). Si corriera primero, el boton se quedaria deshabilitado
 *   para siempre en cuanto el usuario cerrara el modal de Culqi sin pagar.
 *   Escuchando en captura, este preventDefault ocurre ANTES y aquel manejador
 *   se retira solo (comprueba `defaultPrevented`).
 *
 * SI EL SCRIPT DE CULQI NO CARGA
 *
 *   Puede pasar: un bloqueador, una caida suya, o una CSP mal puesta. Sin
 *   defensa, el boton de pagar no haria NADA y el usuario no sabria por que.
 *   Se comprueba que `window.Culqi` exista y, si no, se le dice y se le deja
 *   el boton utilizable.
 *
 * POR_CONFIRMAR
 *
 *   La forma exacta de la API de Culqi Checkout v4 (Culqi.publicKey,
 *   Culqi.settings, Culqi.open y la funcion global `culqi()` como callback)
 *   sale de su documentacion publica; no se ha podido ejecutar contra Culqi
 *   porque todavia no hay llaves. Con las de prueba se comprueba en dos
 *   minutos: abrir /comprar/pro, pulsar Pagar y ver si aparece el modal y si
 *   llega un token que empieza por "tkn_".
 */
(() => {
  "use strict";

  const form = document.querySelector("[data-culqi-llave]");
  if (!form) return;

  const oculto = form.querySelector('input[name="token_id"]');
  const boton = form.querySelector('button[type="submit"]');
  if (!oculto || !boton) return;

  const textoOriginal = boton.textContent;
  let abierto = false;

  const avisar = (texto) => {
    // El aviso va donde ya se pintan los errores del checkout, para que el
    // usuario lo lea donde mira. Sin HTML: es texto del navegador o nuestro.
    let caja = document.querySelector(".mensaje.mal.js-culqi");
    if (!caja) {
      caja = document.createElement("div");
      caja.className = "mensaje mal js-culqi";
      caja.setAttribute("role", "alert");
      const main = document.querySelector("main");
      if (main) main.insertBefore(caja, main.firstChild);
    }
    caja.textContent = texto;
    caja.scrollIntoView({ block: "nearest" });
  };

  const soltarBoton = () => {
    abierto = false;
    boton.disabled = false;
    boton.removeAttribute("aria-busy");
    boton.textContent = textoOriginal;
  };

  /* Culqi v4 llama a una funcion GLOBAL llamada `culqi` cuando termina, con el
     resultado en Culqi.token o en Culqi.error. No es un callback que se pase
     por parametro: tiene que estar en window con ese nombre exacto. */
  window.culqi = function () {
    const C = window.Culqi;
    if (C && C.token && C.token.id) {
      oculto.value = C.token.id;
      boton.textContent = "Activando tu plan…";
      /* form.submit() y no requestSubmit(): aqui ya se valido y ya se
         intercepto una vez. requestSubmit volveria a disparar el evento
         `submit` y entrariamos otra vez en este mismo manejador. */
      form.submit();
      return;
    }
    // Cancelar el modal no es un error y no merece un aviso rojo.
    const error = C && C.error;
    if (error && error.user_message) avisar(error.user_message);
    else if (error) avisar("No se pudo procesar la tarjeta. Intenta de nuevo.");
    soltarBoton();
  };

  document.addEventListener("submit", (ev) => {
    if (ev.target !== form) return;
    // Un token ya puesto significa que venimos de `form.submit()`: se deja
    // pasar, que es justo el envio bueno.
    if (oculto.value) return;

    ev.preventDefault();
    if (abierto) return;

    // La validacion nativa primero: el correo y la contrasena se comprueban
    // ANTES de abrir el formulario de tarjeta. Al reves, alguien metaria su
    // tarjeta y despues descubriria que su contrasena es corta. `invalid`
    // lo pinta en linea (licitapro.js), no con el globo del navegador.
    if (!form.reportValidity()) return;

    const C = window.Culqi;
    if (!C) {
      avisar("No pudimos cargar el formulario de pago. Revisa tu conexión o " +
             "desactiva el bloqueador de anuncios e inténtalo de nuevo.");
      return;
    }

    abierto = true;
    boton.disabled = true;
    boton.setAttribute("aria-busy", "true");
    boton.textContent = "Abriendo el pago…";

    const d = form.dataset;
    C.publicKey = d.culqiLlave;
    C.settings({
      title: d.culqiTitulo || "LicitaPro",
      currency: "PEN",
      /* En centimos, igual que la API. El servidor NO se fia de este numero:
         vuelve a leer el precio de la tabla `planes` y cobra el del PLAN de
         Culqi, que es donde vive el importe de verdad. Tocar esto aqui no
         compra mas barato. */
      amount: parseInt(d.culqiCentimos, 10),
      description: d.culqiDescripcion || "",
    });
    C.open();
  }, true);  // captura: ver la cabecera
})();
