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


/* ---------------------------------------------------------------- entradas
   Las tarjetas aparecen escalonadas al entrar en pantalla.

   El estado oculto se pone DESDE AQUI y no desde el CSS: si este script no
   llega a ejecutarse, no hay animacion y se ve todo. Al reves -- esconder en
   CSS y revelar en JS -- un fallo del script dejaria al usuario mirando un
   panel vacio donde deberia estar su licitacion.

   Solo se anima lo que esta por debajo del pliegue. Animar lo que ya se ve al
   cargar retrasa la lectura de lo primero, que es justo lo que la persona
   venia a mirar. */
(() => {
  "use strict";
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  if (!("IntersectionObserver" in window)) return;

  const tarjetas = [...document.querySelectorAll(".tarjeta")]
    .filter((el) => el.getBoundingClientRect().top > innerHeight * 0.9);
  if (!tarjetas.length) return;

  tarjetas.forEach((el, i) => {
    el.classList.add("rv-app");
    el.style.transitionDelay = Math.min(i, 4) * 45 + "ms";
  });

  const obs = new IntersectionObserver((entradas) => {
    for (const e of entradas) {
      if (!e.isIntersecting) continue;
      e.target.classList.add("in");
      obs.unobserve(e.target);
    }
  }, { threshold: 0.08, rootMargin: "0px 0px -4% 0px" });

  tarjetas.forEach((el) => obs.observe(el));
})();
