/* Animaciones de la portada.
   Vive en un archivo aparte y no dentro del HTML para que la politica de
   seguridad pueda prohibir el script embebido: mientras script-src admita
   'unsafe-inline', un XSS puede ejecutar lo que quiera y la CSP no sirve
   de nada frente a eso. */
(() => {
  "use strict";
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- nav ---------- */
  const nav = document.getElementById("nav");
  const onScrollNav = () => nav.classList.toggle("stuck", scrollY > 40);
  addEventListener("scroll", onScrollNav, {passive:true});
  onScrollNav();

  /* ---------- reveals ---------- */
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      e.target.classList.add("in");
      io.unobserve(e.target);
    }
  }, {threshold:0.16, rootMargin:"0px 0px -8% 0px"});
  document.querySelectorAll(".rv").forEach(el => io.observe(el));

  /* ---------- counters ---------- */
  const easeOut = t => 1 - Math.pow(1 - t, 3);
  const runCount = (el) => {
    const to = +el.dataset.to, suffix = el.dataset.suffix || "";
    if (reduce) { el.textContent = to.toLocaleString("es-PE") + suffix; return; }
    const dur = 1700, t0 = performance.now();
    const tick = (now) => {
      const p = Math.min(1, (now - t0) / dur);
      el.textContent = Math.round(to * easeOut(p)).toLocaleString("es-PE") + suffix;
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };
  const ioCount = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      runCount(e.target);
      ioCount.unobserve(e.target);
    }
  }, {threshold:0.6});
  document.querySelectorAll("[data-to]").forEach(el => ioCount.observe(el));

  /* ---------- score dial + bars ---------- */
  const ioScore = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const R = 84, C = 2 * Math.PI * R, score = 71;
      const ring = document.getElementById("ring");
      if (ring) ring.style.strokeDashoffset = String(C * (1 - score / 100));
      const val = document.getElementById("dialval");
      if (val) {
        if (reduce) val.textContent = score;
        else {
          const t0 = performance.now();
          const tick = (now) => {
            const p = Math.min(1, (now - t0) / 1600);
            val.textContent = Math.round(score * easeOut(p));
            if (p < 1) requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
        }
      }
      document.querySelectorAll(".bar-fill").forEach((b, i) => {
        setTimeout(() => { b.style.width = b.dataset.pct + "%"; }, reduce ? 0 : i * 110);
      });
      ioScore.unobserve(e.target);
    }
  }, {threshold:0.35});
  const dial = document.querySelector(".dial");
  if (dial) ioScore.observe(dial);

  /* ---------- 3D tilt on bot cards ---------- */
  if (!reduce && matchMedia("(hover: hover)").matches) {
    document.querySelectorAll(".bot").forEach(card => {
      card.addEventListener("pointermove", (ev) => {
        const r = card.getBoundingClientRect();
        const px = (ev.clientX - r.left) / r.width;
        const py = (ev.clientY - r.top) / r.height;
        card.style.setProperty("--mx", (px * 100) + "%");
        card.style.setProperty("--my", (py * 100) + "%");
        card.style.transform =
          `rotateY(${(px - .5) * 11}deg) rotateX(${(.5 - py) * 11}deg) translateZ(22px)`;
      });
      card.addEventListener("pointerleave", () => { card.style.transform = ""; });
    });
  }

  /* =========================================================
     Territorio 3D - nube de puntos del Peru, proyeccion propia.
     Sin librerias: silueta en lat/lon, relleno por point-in-polygon,
     rotacion y zoom atados al scroll.
     ========================================================= */
  const cv = document.getElementById("territorio");
  const ctx = cv.getContext("2d", {alpha:true});

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

  const inside = (x, y, poly) => {
    let hit = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
    }
    return hit;
  };

  // Muestreo determinista: el patron no cambia entre recargas.
  let seed = 20490765;
  const rnd = () => { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; };

  const LON0 = -81.4, LON1 = -68.6, LAT0 = -18.5, LAT1 = 0.2;
  const LONC = (LON0 + LON1) / 2, LATC = (LAT0 + LAT1) / 2;
  const pts = [];
  let guard = 0;
  while (pts.length < 2300 && guard < 90000) {
    guard++;
    const lon = LON0 + rnd() * (LON1 - LON0);
    const lat = LAT0 + rnd() * (LAT1 - LAT0);
    if (!inside(lon, lat, BORDE)) continue;
    pts.push({x:(lon - LONC) / 6.4, y:-(lat - LATC) / 6.4, t:rnd() * 6.2832, city:false});
  }
  for (const c of CIUDADES) {
    pts.push({x:(c[0] - LONC) / 6.4, y:-(c[1] - LATC) / 6.4, t:rnd() * 6.2832, city:true});
  }

  let W = 0, H = 0, dpr = 1;
  const resize = () => {
    dpr = Math.min(devicePixelRatio || 1, 2);
    W = innerWidth; H = innerHeight;
    cv.width = Math.round(W * dpr);
    cv.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  resize();
  addEventListener("resize", resize, {passive:true});

  // Progreso 0..1 sobre hero + contadores.
  const progreso = () => Math.min(1, Math.max(0, scrollY / (innerHeight * 3.2)));

  const draw = (time) => {
    ctx.clearRect(0, 0, W, H);
    const p = progreso();

    const op = Math.max(0, 1 - Math.max(0, p - 0.72) / 0.28) * 0.95;
    cv.style.opacity = String(op);
    if (op <= 0.01) return;

    const ang  = -0.55 + p * 2.5;            // giro atado al scroll
    const tilt = 0.30 - p * 0.16;
    const zoom = Math.min(W, H) * (0.78 + p * 0.55);
    const cx = W * (W > 900 ? 0.70 : 0.5);
    const cy = H * (W > 900 ? 0.52 : 0.44);
    const ca = Math.cos(ang), sa = Math.sin(ang);
    const ct = Math.cos(tilt), st = Math.sin(tilt);

    for (const pt of pts) {
      // Curvatura suave: el territorio se envuelve levemente sobre si mismo.
      const z0 = Math.cos(pt.x * 1.15) * 0.30 - 0.30;
      const X = pt.x * ca + z0 * sa;
      const Zr = -pt.x * sa + z0 * ca;
      const Y = pt.y * ct - Zr * st;
      const Z = pt.y * st + Zr * ct;

      const persp = 2.35 / (2.35 + Z);
      const sx = cx + X * zoom * persp;
      const sy = cy + Y * zoom * persp;
      if (sx < -60 || sx > W + 60 || sy < -60 || sy > H + 60) continue;

      const prof = Math.max(0.15, (persp - 0.66) / 0.72);   // 0 lejos .. 1 cerca
      if (pt.city) {
        const pulso = 0.55 + 0.45 * Math.sin(time / 780 + pt.t);
        const r = (1.9 + pulso * 2.5) * persp;
        ctx.beginPath();
        ctx.arc(sx, sy, r, 0, 6.2832);
        ctx.fillStyle = "rgba(52,224,180," + ((0.45 + pulso * 0.5) * prof).toFixed(3) + ")";
        ctx.fill();
        ctx.beginPath();
        ctx.arc(sx, sy, r * 3.4, 0, 6.2832);
        ctx.fillStyle = "rgba(52,224,180," + (0.05 * pulso * prof).toFixed(3) + ")";
        ctx.fill();
      } else {
        ctx.fillStyle = "rgba(150,205,215," + (0.10 + prof * 0.34).toFixed(3) + ")";
        ctx.fillRect(sx, sy, 1.5 * persp, 1.5 * persp);
      }
    }
  };

  if (reduce) {
    cv.style.transition = "none";
    draw(0);
    addEventListener("scroll", () => draw(0), {passive:true});
  } else {
    const loop = (t) => { draw(t); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }
})();
