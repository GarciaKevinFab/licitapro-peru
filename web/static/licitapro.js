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


/* ------------------------------------------------------- cifras que suben
   Los cuatro numeros del panel y el puntaje de la ficha cuentan hasta su
   valor al cargar.

   POR QUE 700 ms Y NO 1600 COMO EN LA PORTADA
     En la portada el contador ES el contenido y se mira una vez. Aqui el
     numero es un dato que alguien vino a consultar, y hacerle esperar para
     leerlo es cobrarle un peaje por la animacion. 700 ms se percibe como
     "aparecio con vida", no como "todavia no puedo leerlo".

   POR QUE SE LEE EL VALOR DEL DOM Y NO DE UN data-*
     El servidor ya escribio el numero correcto ahi. Duplicarlo en un atributo
     es una segunda fuente de verdad que algun dia va a discrepar de la
     primera. Si el texto no es un entero -- un guion, un "-" de dato ausente --
     no se toca y se queda como esta.
*/
(() => {
  "use strict";
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const subirCifra = (nodo, escribir) => {
    const original = (nodo.textContent || "").trim();
    const bruto = original.replace(/[.,\s]/g, "");
    if (!/^\d+$/.test(bruto)) return;          // guiones y vacios se respetan
    const destino = parseInt(bruto, 10);
    if (destino <= 0) return;                   // un cero no gana nada contando

    // Si el servidor escribio "1.234", hay que devolverlo con su separador al
    // terminar. Sin esto la animacion "arregla" el numero a 1234 y deja el
    // panel peor formateado que antes de animarlo.
    const conFormato = original !== bruto;
    const pinta = (v) => (conFormato ? v.toLocaleString("es-PE") : String(v));

    const DURACION = 700;
    let t0 = null;
    const paso = (t) => {
      if (t0 === null) t0 = t;
      const p = Math.min((t - t0) / DURACION, 1);
      const suave = p === 1 ? 1 : 1 - Math.pow(2, -9 * p);
      escribir(pinta(Math.round(destino * suave)));
      if (p < 1) requestAnimationFrame(paso);
    };
    requestAnimationFrame(paso);
  };

  // Panel: las cuatro cifras de resumen.
  document.querySelectorAll(".tarjetas .t .n").forEach((n) => {
    subirCifra(n, (v) => { n.textContent = String(v); });
  });

  // Ficha: puntaje de cabecera y puntaje del analisis.
  document.querySelectorAll(".metas b").forEach((b) => {
    subirCifra(b, (v) => { b.textContent = String(v); });
  });
  document.querySelectorAll(".analisis .puntaje").forEach((el) => {
    // Lleva un <small>/100</small> dentro: se toca SOLO el primer nodo de
    // texto, o se borraria el sufijo al escribir.
    const nodo = el.firstChild;
    if (!nodo || nodo.nodeType !== 3) return;
    const falso = { textContent: nodo.nodeValue };
    subirCifra(falso, (v) => { nodo.nodeValue = String(v); });
  });
})();
