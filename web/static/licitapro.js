/* Comportamiento de la aplicacion.
 *
 * Todo esto vivia como atributos onclick= y onsubmit= dentro del HTML. Se saca
 * a un archivo por un motivo concreto: mientras la politica de seguridad tenga
 * que admitir 'unsafe-inline' en script-src, un XSS puede ejecutar cualquier
 * cosa y la CSP no protege de nada frente a eso. Sin manejadores embebidos, la
 * politica puede prohibir el script en linea y el hueco se cierra.
 *
 * Se usa delegacion en el documento y no un listener por elemento, porque
 * HTMX reemplaza trozos de la pagina: lo que se enganche al cargar desaparece
 * con el primer refresco de la tabla.
 */
(function () {
  "use strict";

  document.addEventListener("click", function (ev) {
    var boton = ev.target.closest("[data-ir-a]");
    if (boton) {
      ev.preventDefault();
      location.href = boton.dataset.irA;
    }
  });

  // Confirmacion antes de algo que no se deshace facilmente. El texto viaja en
  // el propio elemento para que quede junto a la accion que describe, y no en
  // una tabla de mensajes que se desincroniza al cambiar el formulario.
  document.addEventListener("submit", function (ev) {
    var form = ev.target.closest("[data-confirmar]");
    if (form && !window.confirm(form.dataset.confirmar)) {
      ev.preventDefault();
    }
  });
})();
