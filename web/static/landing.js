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

  /* =====================================================================
     TERRITORIO 3D — nube de puntos del Peru, proyeccion propia.

     Sin librerias y sin WebGL: la silueta va en lat/lon, se rellena por
     point-in-polygon y se proyecta a mano con una perspectiva sencilla. Meter
     three.js aqui serian 600 KB para dibujar dos mil cuadraditos.

     La rotacion y el zoom estan atados al SCROLL, no al reloj: el usuario
     mueve el territorio, no lo mira moverse. Esa es la diferencia entre un
     fondo animado y un fondo que responde.
     ===================================================================== */
  const cv = document.getElementById("territorio");
  if (!cv) return;
  const ctx = cv.getContext("2d", { alpha: true });

  // Silueta aproximada del Peru (lon, lat): norte, frontera oriental, sur, costa.
  const BORDE = [
    [-80.30,-3.40],[-78.30,-2.90],[-76.00,-2.30],[-75.20,-0.05],[-73.20,-1.60],
    [-70.05,-2.60],[-70.90,-4.30],[-72.90,-5.10],[-73.10,-7.50],[-73.80,-9.40],
    [-72.20,-9.50],[-70.60,-9.50],[-68.70,-12.60],[-69.40,-15.20],[-69.00,-16.50],
    [-69.60,-17.30],[-70.40,-18.35],[-71.40,-17.20],[-72.60,-16.70],[-74.20,-15.90],
    [-76.30,-14.10],[-77.00,-12.10],[-77.70,-10.70],[-78.50,-9.00],[-79.60,-7.00],
    [-81.30,-6.10],[-81.20,-5.00],[-80.90,-4.40],[-80.30,-3.40]
  ];

  // Capitales departamentales: pulsan como convocatorias detectadas.
  const CIUDADES = [
    [-77.03,-12.04],[-71.97,-13.52],[-75.20,-12.07],[-71.54,-16.40],
    [-69.19,-12.60],[-80.63,-5.19],[-73.25,-3.75],[-79.03,-8.11],
    [-70.25,-18.01],[-76.36,-13.06],[-78.52,-7.16],[-77.53,-9.53]
  ];

  const dentro = (x, y, poly) => {
    let hit = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
    }
    return hit;
  };

  // Muestreo determinista: el patron no cambia entre recargas. Si no, cada
  // visita veria un Peru distinto y dejaria de leerse como un mapa.
  let semilla = 20490765;
  const rnd = () => { semilla = (semilla * 1664525 + 1013904223) % 4294967296; return semilla / 4294967296; };

  const LON0 = -81.4, LON1 = -68.6, LAT0 = -18.5, LAT1 = 0.2;
  const LONC = (LON0 + LON1) / 2, LATC = (LAT0 + LAT1) / 2;
  const puntos = [];
  let intentos = 0;
  while (puntos.length < 2300 && intentos < 90000) {
    intentos++;
    const lon = LON0 + rnd() * (LON1 - LON0);
    const lat = LAT0 + rnd() * (LAT1 - LAT0);
    if (!dentro(lon, lat, BORDE)) continue;
    puntos.push({ x:(lon - LONC) / 6.4, y:-(lat - LATC) / 6.4, t:rnd() * 6.2832, ciudad:false });
  }
  for (const c of CIUDADES) {
    puntos.push({ x:(c[0] - LONC) / 6.4, y:-(c[1] - LATC) / 6.4, t:rnd() * 6.2832, ciudad:true });
  }

  let W = 0, H = 0, dpr = 1;
  const medir = () => {
    dpr = Math.min(devicePixelRatio || 1, 2);   // por encima de 2 no se nota y cuesta el doble
    W = innerWidth; H = innerHeight;
    cv.width = Math.round(W * dpr);
    cv.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  medir();
  addEventListener("resize", medir, { passive: true });

  /* Paralaje con el raton: el centro se desplaza un poco hacia el lado
     contrario, y el volumen se lee mejor porque cambia el punto de vista. Solo
     con puntero fino: en un movil no hay raton y el evento nunca llega. */
  let mx = 0, my = 0;
  if (matchMedia("(pointer:fine)").matches && !menosMovimiento) {
    addEventListener("mousemove", (e) => {
      mx = (e.clientX / innerWidth - 0.5) * -26;
      my = (e.clientY / innerHeight - 0.5) * -16;
    }, { passive: true });
  }

  const avance = () => Math.min(1, Math.max(0, scrollY / (innerHeight * 3.2)));

  // Se reutiliza entre fotogramas para no generar basura que el recolector
  // tenga que barrer sesenta veces por segundo.
  const proyectados = [];

  const dibujar = (tiempo) => {
    ctx.clearRect(0, 0, W, H);
    const p = avance();

    // Se desvanece antes de los pasos, donde estorbaria a la lectura.
    const op = Math.max(0, 1 - Math.max(0, p - 0.72) / 0.28) * 0.95;
    cv.style.opacity = String(op);
    if (op <= 0.01) return;

    const ang  = -0.55 + p * 2.5;
    const inc  = 0.30 - p * 0.16;
    const zoom = Math.min(W, H) * (0.78 + p * 0.55);
    const cx = W * (W > 900 ? 0.70 : 0.5) + mx;
    const cy = H * (W > 900 ? 0.52 : 0.44) + my;
    const ca = Math.cos(ang), sa = Math.sin(ang);
    const ci = Math.cos(inc), si = Math.sin(inc);

    proyectados.length = 0;
    for (const pt of puntos) {
      // Curvatura suave: el territorio se envuelve levemente sobre si mismo.
      const z0 = Math.cos(pt.x * 1.15) * 0.30 - 0.30;
      const X  = pt.x * ca + z0 * sa;
      const Zr = -pt.x * sa + z0 * ca;
      const Y  = pt.y * ci - Zr * si;
      const Z  = pt.y * si + Zr * ci;

      const persp = 2.35 / (2.35 + Z);
      const sx = cx + X * zoom * persp;
      const sy = cy + Y * zoom * persp;
      if (sx < -60 || sx > W + 60 || sy < -60 || sy > H + 60) continue;

      proyectados.push({
        sx, sy, persp, Z, t: pt.t, ciudad: pt.ciudad,
        prof: Math.max(0.15, (persp - 0.66) / 0.72)   // 0 lejos .. 1 cerca
      });
    }

    /* ORDEN POR PROFUNDIDAD (algoritmo del pintor).
       Sin esto, un punto lejano dibujado despues tapa a uno cercano y el
       volumen se deshace: el ojo deja de leer una forma con fondo y frente, y
       ve confeti. Cuesta ordenar 2300 elementos por fotograma, que es barato
       comparado con perder el efecto entero. */
    proyectados.sort((a, b) => b.Z - a.Z);

    /* Red entre capitales cercanas en pantalla: sugiere un sistema vigilando
       el pais, no una decoracion. Va antes que los puntos para que las lineas
       queden por debajo de ellos. */
    const ciudades = proyectados.filter((q) => q.ciudad);
    const ALCANCE = Math.min(W, H) * 0.26;
    ctx.lineWidth = 1;
    for (let i = 0; i < ciudades.length; i++) {
      for (let j = i + 1; j < ciudades.length; j++) {
        const a = ciudades[i], b = ciudades[j];
        const d = Math.hypot(a.sx - b.sx, a.sy - b.sy);
        if (d > ALCANCE) continue;
        const alfa = (1 - d / ALCANCE) * 0.16 * Math.min(a.prof, b.prof);
        ctx.strokeStyle = "rgba(52,224,180," + alfa.toFixed(3) + ")";
        ctx.beginPath();
        ctx.moveTo(a.sx, a.sy);
        ctx.lineTo(b.sx, b.sy);
        ctx.stroke();
      }
    }

    for (const q of proyectados) {
      if (q.ciudad) {
        const pulso = 0.55 + 0.45 * Math.sin(tiempo / 780 + q.t);
        const r = (1.9 + pulso * 2.5) * q.persp;
        ctx.beginPath();
        ctx.arc(q.sx, q.sy, r, 0, 6.2832);
        ctx.fillStyle = "rgba(52,224,180," + ((0.45 + pulso * 0.5) * q.prof).toFixed(3) + ")";
        ctx.fill();
        ctx.beginPath();
        ctx.arc(q.sx, q.sy, r * 3.4, 0, 6.2832);
        ctx.fillStyle = "rgba(52,224,180," + (0.05 * pulso * q.prof).toFixed(3) + ")";
        ctx.fill();
      } else {
        ctx.fillStyle = "rgba(150,205,215," + (0.10 + q.prof * 0.34).toFixed(3) + ")";
        ctx.fillRect(q.sx, q.sy, 1.5 * q.persp, 1.5 * q.persp);
      }
    }
  };

  if (menosMovimiento) {
    // Sin bucle continuo: se dibuja quieto y se redibuja solo al desplazarse.
    cv.style.transition = "none";
    dibujar(0);
    addEventListener("scroll", () => dibujar(0), { passive: true });
  } else {
    const bucle = (t) => { dibujar(t); requestAnimationFrame(bucle); };
    requestAnimationFrame(bucle);
  }
})();
