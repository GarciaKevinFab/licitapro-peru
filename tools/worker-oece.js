/**
 * PASARELA A OECE — Cloudflare Worker
 *
 * POR QUE EXISTE
 *
 *   OECE devuelve 403 a todo el trafico del VPS: sale por una IP de datacenter
 *   en Estados Unidos (Hostinger AS47583) y su borde la rechaza. Desde una
 *   conexion peruana la misma URL responde 200. La API no cambio; el problema
 *   es por donde salimos.
 *
 *   Este Worker pide la URL desde la red de Cloudflare, que tiene presencia en
 *   Lima, y devuelve la respuesta tal cual.
 *
 * POR QUE NO ES UN PROXY ABIERTO
 *
 *   Un Worker que reenvia a cualquier URL es un proxy publico con tu nombre y
 *   tu factura: lo encuentra un rastreador y lo usan para atacar a terceros
 *   desde tu cuenta. Aqui hay dos cierres, y los dos son necesarios:
 *
 *     1. HOST FIJO. El destino no se recibe: esta escrito abajo. Solo se acepta
 *        la RUTA y la query. Aunque alguien descubra el secreto, lo unico que
 *        consigue es leer datos publicos de OECE.
 *     2. SECRETO COMPARTIDO en una cabecera. Sin el, 401.
 *
 *   La comparacion del secreto es en tiempo constante. Comparar con === filtra
 *   informacion por el tiempo que tarda en fallar, y con suficientes intentos
 *   eso se adivina caracter a caracter.
 *
 * DESPLIEGUE (2 minutos, en el panel de Cloudflare)
 *
 *   1. Workers & Pages -> Create -> Start with Hello World -> Deploy
 *   2. Edit code: borra lo que trae y pega este archivo entero. Deploy.
 *   3. Settings -> Variables and Secrets -> Add:
 *        Type: Secret   Name: OECE_SECRETO   Value: (una cadena larga al azar)
 *   4. Copia la URL del Worker (algo.workers.dev) y pasamela con el secreto.
 */

const DESTINO = "https://contratacionesabiertas.oece.gob.pe";

// Solo lo que el scraper necesita. Si manana hace falta otra ruta, se anade
// aqui a proposito: una lista blanca que se amplia sola no es una lista blanca.
const RUTAS = [/^\/api\/v1\/releases\/?$/];

/** Comparacion en tiempo constante: no revela por cuanto acerto. */
function igual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let dif = 0;
  for (let i = 0; i < a.length; i++) dif |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return dif === 0;
}

export default {
  async fetch(peticion, entorno) {
    if (peticion.method !== "GET") {
      return json({ error: "solo GET" }, 405);
    }
    if (!entorno.OECE_SECRETO) {
      // Sin secreto configurado se cierra entero. Abrirse "mientras tanto" es
      // como acaban publicados los proxies que nadie queria publicar.
      return json({ error: "worker sin OECE_SECRETO configurado" }, 500);
    }
    if (!igual(peticion.headers.get("x-oece-secreto") || "", entorno.OECE_SECRETO)) {
      return json({ error: "no autorizado" }, 401);
    }

    const entrada = new URL(peticion.url);
    if (!RUTAS.some((r) => r.test(entrada.pathname))) {
      return json({ error: "ruta no permitida", ruta: entrada.pathname }, 403);
    }

    const destino = new URL(DESTINO);
    destino.pathname = entrada.pathname;
    destino.search = entrada.search;

    let respuesta;
    try {
      respuesta = await fetch(destino.toString(), {
        method: "GET",
        headers: {
          // Cabeceras de navegador: OECE ya rechazo peticiones pelonas antes,
          // y esto no cuesta nada.
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
          "Accept": "application/json,text/plain,*/*",
          "Accept-Language": "es-PE,es;q=0.9",
        },
        // El scraper recorre muchas paginas; que Cloudflare cachee 5 minutos
        // ahorra viajes sin servir nada rancio.
        cf: { cacheTtl: 300, cacheEverything: true },
      });
    } catch (e) {
      return json({ error: "fallo al alcanzar OECE", detalle: String(e).slice(0, 200) }, 502);
    }

    // Se devuelve el cuerpo y el codigo TAL CUAL. Si OECE responde 403 tambien
    // desde aqui, el scraper tiene que verlo: convertirlo en 200 con lista
    // vacia seria repetir exactamente el bug que acabamos de arreglar.
    const cuerpo = await respuesta.arrayBuffer();
    return new Response(cuerpo, {
      status: respuesta.status,
      headers: {
        "Content-Type": respuesta.headers.get("content-type") || "application/json",
        "X-Origen-Estado": String(respuesta.status),
      },
    });
  },
};

function json(obj, estado) {
  return new Response(JSON.stringify(obj), {
    status: estado,
    headers: { "Content-Type": "application/json" },
  });
}
