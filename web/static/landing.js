/* Animaciones de la portada.

   VIVE EN UN ARCHIVO APARTE, NO EN EL HTML

     Mientras script-src admitiera 'unsafe-inline', un XSS podria ejecutar lo
     que quisiera y la politica de seguridad no serviria de nada frente a eso.
     Sacar este bloque a /static es lo que permitio cerrarla.

   PRINCIPIO: el navegador anima, nosotros solo cambiamos clases.

     Todo lo que se mueve esta descrito en CSS con transiciones. Aqui solo se
     anaden y quitan clases y se escriben dos o tres propiedades. Asi el
     compositor hace el trabajo y el hilo principal queda libre, que es la
     diferencia entre 60fps y un scroll a tirones en el movil de gama media
     desde el que se va a abrir esto.
*/
(() => {
  "use strict";

  const menosMovimiento = matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ------------------------------------------------------------------ nav
     Fondo translucido solo cuando ya se ha bajado: arriba del todo la barra
     debe desaparecer sobre el hero. */
  const nav = document.getElementById("nav");
  if (nav) {
    const pintarNav = () => nav.classList.toggle("stuck", scrollY > 40);
    addEventListener("scroll", pintarNav, { passive: true });
    pintarNav();
  }

  /* ------------------------------------------------- titular del hero
     Ya NO se toca desde aqui. Antes se le ponia una clase con
     requestAnimationFrame, y rAF no corre en pestanas en segundo plano: quien
     abriera el enlace con ctrl+clic se encontraba el titular invisible, porque
     las lineas se quedaban desplazadas dentro de su mascara.
     Ahora es una animacion CSS con fill-mode forwards. Que el texto principal
     de la pagina dependa de un script para verse es un error, no una opcion. */

  /* RED DE SEGURIDAD PARA TODO LO DEMAS
     Si algo de aqui abajo lanza -- un navegador sin IntersectionObserver, una
     extension que rompe el DOM, un fallo mio --, lo peor que puede pasar es
     que se pierdan las animaciones. Lo que NO puede pasar es que la pagina se
     quede en blanco porque medio contenido esta a opacity 0 esperando una
     clase que nunca llega. */
  const revelarTodo = () => {
    document.querySelectorAll(".rv").forEach((el) => el.classList.add("in"));
    document.querySelectorAll(".cifra").forEach((c) => {
      c.classList.add("viva");
      const n = c.querySelector(".n");
      if (n) n.textContent = Number(c.dataset.hasta || 0).toLocaleString("es-PE") + (c.dataset.sufijo || "");
    });
  };
  /* Se registra ANTES del codigo que podria fallar, que es el unico orden en
     que sirve de algo. Es idempotente, asi que no importa que un error de
     carga de una fuente lo dispare tambien. */
  addEventListener("error", revelarTodo);

  /* ------------------------------------------------------------ revelados
     Un observador para toda la pagina. `unobserve` tras revelar: lo que ya
     entro no necesita seguir vigilado, y dejarlo cuesta trabajo en cada
     scroll para nada. */
  const revelador = new IntersectionObserver((entradas) => {
    for (const e of entradas) {
      if (!e.isIntersecting) continue;
      e.target.classList.add("in");
      revelador.unobserve(e.target);
    }
  }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });

  document.querySelectorAll(".rv").forEach((el) => {
    if (menosMovimiento) { el.classList.add("in"); return; }
    revelador.observe(el);
  });

  /* ------------------------------------------------------- contador suave
     Arranca rapido y frena al final (easeOutExpo). Un contador lineal parece
     una barra de progreso; este parece que "aterriza", que es lo que hace que
     apetezca mirarlo hasta el final. */
  const contar = (nodo, hasta, milesSep, sufijo) => {
    const DURACION = 1600;
    let t0 = null;
    const paso = (t) => {
      if (t0 === null) t0 = t;
      const p = Math.min((t - t0) / DURACION, 1);
      const suavizado = p === 1 ? 1 : 1 - Math.pow(2, -10 * p);
      const valor = Math.round(hasta * suavizado);
      nodo.textContent = (milesSep ? valor.toLocaleString("es-PE") : String(valor)) + sufijo;
      if (p < 1) requestAnimationFrame(paso);
    };
    requestAnimationFrame(paso);
  };

  /* ----------------------------------------------------------- las cifras
     Cada una se enciende y cuenta cuando entra en pantalla. Se hace con un
     observador y NO leyendo scrollY en cada evento: leer geometria dentro del
     manejador de scroll obliga al navegador a recalcular el diseno sesenta
     veces por segundo, que es como se consigue justo lo contrario de fluidez. */
  const cifras = document.querySelectorAll(".cifra");

  const encender = (cifra) => {
    cifra.classList.add("viva");
    const nodo = cifra.querySelector(".n");
    if (!nodo) return;
    const hasta = Number(cifra.dataset.hasta || 0);
    const sufijo = cifra.dataset.sufijo || "";
    const miles = cifra.dataset.miles === "1";
    if (menosMovimiento) {
      nodo.textContent = (miles ? hasta.toLocaleString("es-PE") : String(hasta)) + sufijo;
    } else {
      contar(nodo, hasta, miles, sufijo);
    }
  };

  if (menosMovimiento) {
    cifras.forEach(encender);
  } else {
    const obsCifras = new IntersectionObserver((entradas) => {
      for (const e of entradas) {
        if (!e.isIntersecting) continue;
        encender(e.target);
        obsCifras.unobserve(e.target);
      }
    }, { threshold: 0.55 });
    cifras.forEach((c) => obsCifras.observe(c));
  }

  /* ------------------------------------------------------ dial y barras
     El anillo se dibuja moviendo stroke-dashoffset, no redibujando el arco:
     una sola propiedad animable, sin recalcular la geometria del SVG. */
  const PUNTAJE = 71;
  const CIRCUNFERENCIA = 527.8;   // 2 * PI * r, con r = 84

  const dial = document.getElementById("ring");
  const dialval = document.getElementById("dialval");
  const barras = document.getElementById("bars");

  const dibujarPuntaje = () => {
    if (dial) dial.style.strokeDashoffset = String(CIRCUNFERENCIA * (1 - PUNTAJE / 100));
    if (dialval) {
      if (menosMovimiento) dialval.textContent = String(PUNTAJE);
      else contar(dialval, PUNTAJE, false, "");
    }
    document.querySelectorAll(".bar-fill").forEach((b) => {
      b.style.width = (b.dataset.pct || 0) + "%";
    });
  };

  if (menosMovimiento) {
    dibujarPuntaje();
  } else if (barras) {
    const obsPuntaje = new IntersectionObserver((entradas) => {
      for (const e of entradas) {
        if (!e.isIntersecting) continue;
        dibujarPuntaje();
        obsPuntaje.disconnect();
      }
    }, { threshold: 0.3 });
    obsPuntaje.observe(barras);
  }
})();
