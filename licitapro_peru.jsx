import { useState } from "react";

const BOTS = [
  {
    id: "radar",
    name: "🔍 LicitaRadar",
    handle: "@LicitaRadarBot",
    color: "#1D9E75",
    darkBg: "#04342C",
    role: "Busca, filtra, descarga bases y analiza",
    description: "Escanea 12 fuentes cada hora a nivel nacional. Descarga bases automáticamente. Analiza con IA. Te muestra solo las que te convienen.",
    features: [
      "Escaneo automático 12 fuentes / 25 departamentos",
      "Descarga automática de bases PDF",
      "Análisis IA de requisitos y viabilidad",
      "Score 0-100 de probabilidad de ganar",
      "Histórico de precios y competidores",
      "Resumen diario y semanal automático",
    ],
    commands: [
      ["/hoy", "Licitaciones nuevas del día"],
      ["/semana", "Resumen semanal + cierres próximos"],
      ["/buscar [keyword]", "Buscar en todas las fuentes"],
      ["/region [dept]", "Filtrar por departamento"],
      ["/analizar [id]", "Análisis IA profundo de una licitación"],
      ["/competidores [id]", "Quién compite en este proceso"],
      ["/precio [id]", "Estimación de precio ganador"],
      ["/licitar [id]", "Decide participar → activa LicitaPrep"],
      ["/config", "Configurar regiones, keywords, montos"],
    ],
  },
  {
    id: "prep",
    name: "📝 LicitaPrep",
    handle: "@LicitaPrepBot",
    color: "#534AB7",
    darkBg: "#26215C",
    role: "Prepara propuestas y te pregunta lo que falta",
    description: "Cuando decides licitar, este bot toma el control. Llena todos los anexos automáticamente con tus datos. Si algo falta, TE PREGUNTA y aprende la respuesta para siempre.",
    features: [
      "Auto-fill de TODOS los anexos con datos de tu empresa",
      "Genera propuesta técnica con IA adaptada a las bases",
      "Calcula precio económico competitivo",
      "Si falta info → TE PREGUNTA y aprende para siempre",
      "Arma expediente ZIP completo listo para subir",
      "Valida que no falte ningún documento antes de enviar",
      "Te da el paso a paso para subir a SEACE",
    ],
    commands: [
      ["/estado", "Ver estado de propuestas en preparación"],
      ["/responder", "Contestar pregunta pendiente"],
      ["/revisar [id]", "Ver propuesta antes de enviar"],
      ["/corregir [id] [campo]", "Corregir un dato específico"],
      ["/aprobar [id]", "Aprobar y generar expediente final"],
      ["/datos empresa", "Ver/editar datos de tus empresas"],
      ["/experiencia add", "Agregar nueva experiencia/contrato"],
      ["/equipo add", "Agregar profesional al equipo técnico"],
      ["/subir [id]", "Guía paso a paso para subir a SEACE"],
    ],
  },
  {
    id: "win",
    name: "🏆 LicitaWin",
    handle: "@LicitaWinBot",
    color: "#D85A30",
    darkBg: "#4A1B0C",
    role: "Ganaste — ahora trackea plazos, pagos y entregas",
    description: "Monitorea SEACE para detectar si ganaste la buena pro. Te avisa por Telegram + email. Trackea plazos de entrega, firma de contrato, conformidades y pagos.",
    features: [
      "Detecta automáticamente si ganaste la buena pro",
      "Notifica por Telegram + envía email formal",
      "Timeline de plazos: firma contrato, entrega, conformidad",
      "Alertas antes de cada fecha límite",
      "Seguimiento de pagos pendientes y recibidos",
      "Recordatorio de garantías y fianzas por vencer",
      "Resumen mensual de contratos activos",
      "Genera actas de conformidad y facturas",
    ],
    commands: [
      ["/contratos", "Ver todos los contratos activos"],
      ["/plazos", "Próximas fechas límite"],
      ["/pagos", "Estado de pagos: pendientes y recibidos"],
      ["/entrega [id]", "Registrar entrega/avance"],
      ["/conformidad [id]", "Generar acta de conformidad"],
      ["/factura [id]", "Generar factura para cobro"],
      ["/garantias", "Garantías y fianzas por vencer"],
      ["/resumen", "Dashboard mensual de contratos"],
      ["/email [id]", "Re-enviar notificación por email"],
    ],
  },
];

const LIFECYCLE = [
  { step: "1", label: "Detectar", bot: "radar", icon: "🔍", desc: "12 fuentes escaneadas cada hora. Filtro por región + rubro + monto. Score de viabilidad IA." },
  { step: "2", label: "Analizar", bot: "radar", icon: "📊", desc: "Descarga bases PDF. Claude analiza requisitos. Compara con tu perfil. Precio estimado." },
  { step: "3", label: "Decidir", bot: "radar", icon: "✅", desc: "Tú decides: /licitar [id]. El sistema activa LicitaPrep automáticamente." },
  { step: "4", label: "Preparar", bot: "prep", icon: "📝", desc: "Auto-fill de todos los anexos. Propuesta técnica IA. Si algo falta → te pregunta." },
  { step: "5", label: "Revisar", bot: "prep", icon: "👁️", desc: "Expediente completo en ZIP. Tú revisas y apruebas. Guía para subir a SEACE." },
  { step: "6", label: "Presentar", bot: "prep", icon: "🚀", desc: "Tú subes a SEACE (requiere RNP + firma digital). El bot monitorea el estado." },
  { step: "7", label: "Ganar", bot: "win", icon: "🏆", desc: "Detecta buena pro automáticamente. Telegram + email. Timeline de plazos." },
  { step: "8", label: "Ejecutar", bot: "win", icon: "📦", desc: "Trackea entregas, conformidades, pagos. Alertas antes de cada deadline." },
];

const LEARNING_EXAMPLES = [
  {
    question: "No encuentro el nombre del representante legal de K&A Sistemas. ¿Cuál es?",
    answer: "Kevin Fabrizio Garcia Espiritu",
    learned: "Representante legal de K&A guardado. No volveré a preguntar.",
  },
  {
    question: "La licitación requiere experiencia en 'implementación de sistemas de control de asistencia'. ¿Tienes contratos similares?",
    answer: "Sí, SISAC en IIE JEC José Gálvez Barrenechea, La Oroya. Contrato 2024.",
    learned: "Experiencia en control de asistencia agregada. Se usará en futuras propuestas.",
  },
  {
    question: "Necesito el número de partida registral de K&A en SUNARP para el Anexo 1. ¿Lo tienes?",
    answer: "Partida N° 11234567 de la Zona Registral de Madre de Dios",
    learned: "Partida registral guardada permanentemente.",
  },
  {
    question: "¿Cuál es tu margen mínimo aceptable para licitaciones de mantenimiento de equipos?",
    answer: "20% sobre costo",
    learned: "Margen mínimo para mantenimiento = 20%. Se usará en cálculo de precios.",
  },
];

function BotChat({ bot, messages }) {
  return (
    <div style={{
      background: "#0e1621", borderRadius: "14px", overflow: "hidden",
      fontFamily: "-apple-system, sans-serif", maxWidth: "420px", margin: "0 auto",
    }}>
      <div style={{
        background: "#17212b", padding: "10px 14px",
        display: "flex", alignItems: "center", gap: "10px",
        borderBottom: "1px solid #1e2c3a",
      }}>
        <div style={{
          width: "36px", height: "36px", borderRadius: "50%",
          background: `linear-gradient(135deg, ${bot.color}, ${bot.darkBg})`,
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: "16px",
        }}>{bot.name.split(" ")[0]}</div>
        <div>
          <div style={{ color: "#fff", fontSize: "13px", fontWeight: "600" }}>{bot.name.slice(2)}</div>
          <div style={{ color: "#6d8ca0", fontSize: "10px" }}>{bot.handle} · en línea</div>
        </div>
      </div>
      <div style={{ padding: "10px", display: "flex", flexDirection: "column", gap: "6px", maxHeight: "400px", overflowY: "auto" }}>
        {messages.map((m, i) => {
          if (m.type === "buttons") return (
            <div key={i} style={{ display: "flex", flexWrap: "wrap", gap: "3px" }}>
              {m.opts.map((o, j) => (
                <div key={j} style={{
                  background: "#17212b", border: "1px solid #2b5278", borderRadius: "7px",
                  padding: "5px 9px", color: "#6ab3f3", fontSize: "11px", cursor: "pointer",
                  flex: "1 1 calc(50% - 3px)", textAlign: "center",
                }}>{o}</div>
              ))}
            </div>
          );
          const isUser = m.type === "user";
          return (
            <div key={i} style={{ alignSelf: isUser ? "flex-end" : "flex-start", maxWidth: "92%" }}>
              <div style={{
                background: isUser ? "#2b5278" : "#182533",
                borderRadius: isUser ? "12px 4px 12px 12px" : "4px 12px 12px 12px",
                padding: "7px 11px", color: "#e4ecf2", fontSize: "12px",
                lineHeight: "1.4", whiteSpace: "pre-line",
              }}>
                {m.text.split("**").map((p, k) =>
                  k % 2 === 1 ? <strong key={k}>{p}</strong> : <span key={k}>{p}</span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function LicitaPro() {
  const [tab, setTab] = useState("vision");
  const [activeBot, setActiveBot] = useState(0);

  const tabs = [
    { id: "vision", label: "La Visión" },
    { id: "bots", label: "3 Bots" },
    { id: "lifecycle", label: "Ciclo Completo" },
    { id: "learning", label: "Bot que Aprende" },
    { id: "vs", label: "vs LicitaLAB" },
    { id: "prompt", label: "Mega Prompt" },
  ];

  const radarMsgs = [
    { type: "bot", text: "🔍 **RESUMEN DIARIO — 24 Mar 2026**\n\n📊 Escaneadas: 312 licitaciones\n✅ Relevantes para ti: 8\n🔴 Alta viabilidad: 3\n⏰ Cierran esta semana: 5" },
    { type: "bot", text: "🔴 **VIABILIDAD 91%** — ¡Muy buena!\n\n📋 AS-SM-023-2026-GORE CUSCO\n🏛️ Gobierno Regional de Cusco\n📦 Implementación de sistema de gestión académica para 12 IESPP\n💰 S/ 385,000.00\n📅 Cierre: 31/03/2026 (7 días)\n\n🧠 **¿Por qué 91%?**\n✅ Experiencia directa: sistema-academico IESPP Allende\n✅ RNP K&A vigente\n✅ Precio historial: S/320K-400K\n✅ Solo 2-3 competidores esperados\n⚠️ Requiere equipo de 3 profesionales" },
    { type: "buttons", opts: ["🚀 /licitar", "📄 Ver bases", "💰 Precio sugerido", "❌ Pasar"] },
    { type: "user", text: "/licitar AS-SM-023-2026" },
    { type: "bot", text: "✅ **¡ACTIVADO!** Enviando a @LicitaPrepBot\n\n📥 Bases descargadas (3 PDFs, 47 páginas)\n📋 14 anexos identificados\n🤖 Preparando auto-fill con datos de K&A...\n\n👉 Revisa @LicitaPrepBot para el progreso" },
  ];

  const prepMsgs = [
    { type: "bot", text: "📝 **NUEVA PROPUESTA EN PREPARACIÓN**\n\n📋 AS-SM-023-2026-GORE CUSCO\n📦 Sistema de gestión académica 12 IESPP\n🏢 Empresa: K&A Sistemas (RUC 20490765507)\n\n⏳ Llenando 14 anexos automáticamente..." },
    { type: "bot", text: "✅ **PROGRESO: 11/14 anexos completados**\n\n✅ Anexo 1: Carta de presentación\n✅ Anexo 2: Declaración jurada\n✅ Anexo 3: Declaración jurada de plazo\n✅ Anexo 4: Experiencia del postor\n✅ Anexo 5: Carta de compromiso del personal\n✅ Anexo 6: Promesa de consorcio (N/A)\n✅ Anexo 7: Propuesta económica\n✅ Anexo 8: RNP + Vigencia de poder\n✅ Anexo 9: Constancia no impedido\n✅ Anexo 10: Pacto de integridad\n✅ Propuesta técnica (32 páginas)\n\n❓ **NECESITO 3 RESPUESTAS TUYAS:**" },
    { type: "bot", text: "❓ **Pregunta 1 de 3:**\n\nLas bases piden un Jefe de Proyecto con título en Ing. de Sistemas + 5 años de experiencia en gestión académica.\n\n¿Quién será? Necesito: nombre, título profesional, colegiatura, y años de experiencia." },
    { type: "user", text: "Kevin Garcia Espiritu, Bachiller en Ing. de Sistemas (en proceso de titulación), 4 años de experiencia en sistema-academico IESPP Allende" },
    { type: "bot", text: "⚠️ Ojo: las bases piden TÍTULO, no bachiller. Y 5 años, tú tienes 4. Opciones:\n\n1️⃣ Buscar otro profesional que cumpla\n2️⃣ Presentar así (riesgo de descalificación)\n3️⃣ Incluir a otro profesional como jefe y tú como soporte\n\n¿Qué prefieres?" },
    { type: "user", text: "3, busca en mi equipo si hay alguien" },
    { type: "bot", text: "📋 En tus datos tengo registrado:\n- Ing. Carlos Mendoza (titulado, CIP 123456, 7 años exp. en sistemas académicos)\n\n¿Lo asigno como Jefe de Proyecto y a ti como Especialista Técnico?" },
  ];

  const winMsgs = [
    { type: "bot", text: "🏆🏆🏆 **¡GANASTE LA BUENA PRO!** 🏆🏆🏆\n\n📋 AS-SM-023-2026-GORE CUSCO\n📦 Sistema de gestión académica 12 IESPP\n🏢 K&A Sistemas y Telecomunicaciones\n💰 Monto adjudicado: S/ 367,500.00\n📅 Adjudicación: 15/04/2026\n\n📧 Email enviado a: ventas@sisac.pe\n\n⏰ **PLAZOS CRÍTICOS:**" },
    { type: "bot", text: "📅 **TIMELINE DEL CONTRATO**\n\n🔴 22/04 — Firma de contrato (7 días)\n🔴 22/04 — Presentar carta fianza 10%\n🟡 15/05 — Primer entregable (30 días)\n🟡 15/06 — Segundo entregable (60 días)\n🟢 15/07 — Entrega final + conformidad\n💰 15/08 — Fecha estimada de pago\n\n⏰ Te avisaré 3 días antes de cada fecha" },
    { type: "bot", text: "⚡ **RECORDATORIO** — Faltan 3 días\n\n📋 Firma de contrato: 22/04/2026\n📎 Documentos necesarios:\n✅ Contrato firmado (ya generado)\n✅ Carta fianza 10% (S/ 36,750)\n⚠️ Vigencia de poder actualizada\n❌ Póliza de seguro (falta contratar)\n\n¿Necesitas ayuda con algún documento?" },
    { type: "buttons", opts: ["📄 Ver contrato", "💰 Estado de pagos", "📊 Resumen mes", "📧 Reenviar email"] },
  ];

  return (
    <div style={{ fontFamily: "'Segoe UI', -apple-system, sans-serif", color: "var(--color-text-primary)" }}>
      {/* Hero */}
      <div style={{
        background: "linear-gradient(135deg, #1a0a2e 0%, #2d1b4e 25%, #0F6E56 50%, #D85A30 100%)",
        borderRadius: "16px", padding: "24px 20px", marginBottom: "14px",
        position: "relative", overflow: "hidden",
      }}>
        <div style={{ position: "relative" }}>
          <h1 style={{ fontSize: "22px", fontWeight: "800", color: "#fff", margin: "0 0 2px", letterSpacing: "-0.5px" }}>
            LicitaPro Perú
          </h1>
          <p style={{ fontSize: "12px", color: "rgba(255,255,255,0.65)", margin: "0 0 14px" }}>
            3 bots · ciclo completo · auto-fill · aprende de ti · trackea pagos
          </p>
          <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
            {BOTS.map(b => (
              <div key={b.id} style={{
                background: "rgba(255,255,255,0.12)", borderRadius: "10px",
                padding: "8px 14px", flex: "1 1 auto", backdropFilter: "blur(8px)",
                borderLeft: `3px solid ${b.color}`,
              }}>
                <div style={{ fontSize: "13px", fontWeight: "700", color: "#fff" }}>{b.name}</div>
                <div style={{ fontSize: "10px", color: "rgba(255,255,255,0.55)", marginTop: "1px" }}>{b.role}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div style={{
        display: "flex", gap: "3px", marginBottom: "14px", overflowX: "auto",
        padding: "3px", background: "var(--color-background-secondary)", borderRadius: "10px",
      }}>
        {tabs.map(t => (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            padding: "7px 11px", borderRadius: "7px", border: "none", cursor: "pointer",
            fontSize: "12px", fontWeight: tab === t.id ? "600" : "400",
            background: tab === t.id ? "var(--color-background-primary)" : "transparent",
            color: tab === t.id ? "var(--color-text-primary)" : "var(--color-text-secondary)",
            boxShadow: tab === t.id ? "0 1px 2px rgba(0,0,0,0.06)" : "none",
            whiteSpace: "nowrap",
          }}>{t.label}</button>
        ))}
      </div>

      {/* VISION */}
      {tab === "vision" && (
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          <div style={{
            background: "var(--color-background-primary)", borderRadius: "14px",
            border: "0.5px solid var(--color-border-tertiary)", padding: "18px",
          }}>
            <h3 style={{ fontSize: "16px", fontWeight: "700", margin: "0 0 14px" }}>¿Cómo funciona?</h3>
            <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              {[
                { n: "1", title: "Tú configuras UNA VEZ", desc: "Datos de tus empresas (RUC, representante, RNP, experiencia, equipo técnico), departamentos que te interesan, rubros, montos. El sistema aprende todo.", color: "#534AB7" },
                { n: "2", title: "LicitaRadar busca 24/7", desc: "12 fuentes escaneadas cada hora en los 25 departamentos. Solo te muestra las relevantes con score de viabilidad. Tú eliges: /licitar o /pasar.", color: "#1D9E75" },
                { n: "3", title: "LicitaPrep prepara TODO", desc: "Auto-llena TODOS los anexos con tus datos. Genera propuesta técnica con IA. Calcula precio. Si algo falta → TE PREGUNTA y nunca más vuelve a preguntar eso.", color: "#534AB7" },
                { n: "4", title: "Tú solo revisas y subes", desc: "Expediente ZIP completo. Revisas 5 minutos. Apruebas. El bot te da el paso a paso para subir a SEACE. Tú firmas digitalmente y listo.", color: "#D85A30" },
                { n: "5", title: "LicitaWin trackea todo", desc: "Detecta si ganaste. Te avisa por Telegram + email. Timeline de plazos. Alertas 3 días antes de cada fecha. Seguimiento de pagos. Generador de facturas.", color: "#D85A30" },
              ].map((s, i) => (
                <div key={i} style={{ display: "flex", gap: "12px" }}>
                  <div style={{
                    minWidth: "30px", height: "30px", borderRadius: "50%",
                    background: s.color, color: "#fff", display: "flex",
                    alignItems: "center", justifyContent: "center",
                    fontSize: "13px", fontWeight: "700", flexShrink: 0,
                  }}>{s.n}</div>
                  <div>
                    <div style={{ fontSize: "13px", fontWeight: "700" }}>{s.title}</div>
                    <div style={{ fontSize: "12px", color: "var(--color-text-secondary)", lineHeight: "1.5", marginTop: "2px" }}>{s.desc}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div style={{
            background: "var(--color-background-primary)", borderRadius: "14px",
            border: "0.5px solid var(--color-border-tertiary)", padding: "18px",
          }}>
            <h3 style={{ fontSize: "14px", fontWeight: "700", margin: "0 0 10px" }}>Lo que LicitaLAB NO tiene y tú SÍ</h3>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))", gap: "8px" }}>
              {[
                "Auto-llenado completo de TODOS los anexos",
                "Bot que te PREGUNTA lo que falta y APRENDE",
                "Generación de propuesta técnica con IA",
                "Cálculo de precio competitivo con histórico",
                "Expediente ZIP listo para subir a SEACE",
                "Tracking post-adjudicación de plazos y pagos",
                "Notificación de buena pro por Telegram + email",
                "Generador de facturas y actas de conformidad",
                "Base de conocimiento que CRECE con cada licitación",
                "Multi-empresa (K&A, Soluciones MDD, San José, Cubs Fam)",
                "3 bots especializados vs 1 plataforma genérica",
                "GRATIS (self-hosted) vs S/240-325/mes",
              ].map((f, i) => (
                <div key={i} style={{
                  fontSize: "12px", padding: "8px 10px", borderRadius: "8px",
                  background: "var(--color-background-secondary)",
                  display: "flex", gap: "6px", alignItems: "flex-start",
                }}>
                  <span style={{ color: "#1D9E75", fontWeight: "700", flexShrink: 0 }}>✓</span>
                  <span style={{ color: "var(--color-text-primary)" }}>{f}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* 3 BOTS */}
      {tab === "bots" && (
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          <div style={{ display: "flex", gap: "6px" }}>
            {BOTS.map((b, i) => (
              <button key={i} onClick={() => setActiveBot(i)} style={{
                flex: 1, padding: "10px 8px", borderRadius: "10px", border: "none", cursor: "pointer",
                background: activeBot === i ? b.color : "var(--color-background-secondary)",
                color: activeBot === i ? "#fff" : "var(--color-text-secondary)",
                fontSize: "12px", fontWeight: "600", transition: "all 0.15s",
              }}>
                <div>{b.name}</div>
              </button>
            ))}
          </div>

          {(() => {
            const b = BOTS[activeBot];
            const msgs = activeBot === 0 ? radarMsgs : activeBot === 1 ? prepMsgs : winMsgs;
            return (
              <>
                <div style={{
                  background: "var(--color-background-primary)", borderRadius: "14px",
                  border: "0.5px solid var(--color-border-tertiary)", padding: "18px",
                  borderLeft: `3px solid ${b.color}`,
                }}>
                  <h3 style={{ fontSize: "16px", fontWeight: "700", color: b.color, margin: "0 0 4px" }}>{b.name}</h3>
                  <p style={{ fontSize: "12px", color: "var(--color-text-secondary)", margin: "0 0 12px", lineHeight: "1.5" }}>{b.description}</p>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: "4px", marginBottom: "14px" }}>
                    {b.features.map((f, i) => (
                      <div key={i} style={{
                        fontSize: "11.5px", padding: "6px 10px", borderRadius: "6px",
                        background: `${b.color}10`, color: "var(--color-text-primary)",
                      }}>✓ {f}</div>
                    ))}
                  </div>
                  <h4 style={{ fontSize: "12px", fontWeight: "600", margin: "0 0 6px", textTransform: "uppercase", letterSpacing: "0.5px", color: "var(--color-text-secondary)" }}>Comandos</h4>
                  <div style={{ display: "grid", gap: "3px" }}>
                    {b.commands.map(([cmd, desc], i) => (
                      <div key={i} style={{ display: "flex", gap: "10px", padding: "4px 0" }}>
                        <code style={{ fontSize: "11px", color: b.color, fontWeight: "600", minWidth: "155px" }}>{cmd}</code>
                        <span style={{ fontSize: "11px", color: "var(--color-text-secondary)" }}>{desc}</span>
                      </div>
                    ))}
                  </div>
                </div>

                <BotChat bot={b} messages={msgs} />
              </>
            );
          })()}
        </div>
      )}

      {/* LIFECYCLE */}
      {tab === "lifecycle" && (
        <div style={{
          background: "var(--color-background-primary)", borderRadius: "14px",
          border: "0.5px solid var(--color-border-tertiary)", padding: "18px",
        }}>
          <h3 style={{ fontSize: "15px", fontWeight: "700", margin: "0 0 14px" }}>Ciclo completo de una licitación</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: "0" }}>
            {LIFECYCLE.map((s, i) => {
              const bot = BOTS.find(b => b.id === s.bot);
              return (
                <div key={i}>
                  <div style={{ display: "flex", gap: "14px", padding: "12px 0" }}>
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px" }}>
                      <div style={{
                        width: "36px", height: "36px", borderRadius: "50%",
                        background: bot.color, color: "#fff",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: "16px", flexShrink: 0,
                      }}>{s.icon}</div>
                      {i < LIFECYCLE.length - 1 && (
                        <div style={{ width: "2px", height: "100%", minHeight: "20px", background: "var(--color-border-tertiary)" }} />
                      )}
                    </div>
                    <div style={{ flex: 1 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                        <span style={{ fontSize: "14px", fontWeight: "700" }}>Paso {s.step}: {s.label}</span>
                        <span style={{
                          fontSize: "9px", padding: "2px 8px", borderRadius: "4px",
                          background: `${bot.color}15`, color: bot.color, fontWeight: "600",
                        }}>{bot.name.slice(2)}</span>
                      </div>
                      <div style={{ fontSize: "12px", color: "var(--color-text-secondary)", lineHeight: "1.5", marginTop: "3px" }}>{s.desc}</div>
                    </div>
                  </div>
                  {(i === 2 || i === 5) && (
                    <div style={{
                      margin: "4px 0 4px 50px", padding: "6px 12px", borderRadius: "8px",
                      background: "var(--color-background-warning)", border: "0.5px solid var(--color-border-warning)",
                      fontSize: "11px", color: "var(--color-text-warning)",
                    }}>
                      {i === 2 ? "🔀 Cambio de bot: LicitaRadar → LicitaPrep" : "🔀 Monitoreo: LicitaPrep → LicitaWin (post-adjudicación)"}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* LEARNING */}
      {tab === "learning" && (
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          <div style={{
            background: "var(--color-background-primary)", borderRadius: "14px",
            border: "0.5px solid var(--color-border-tertiary)", padding: "18px",
          }}>
            <h3 style={{ fontSize: "15px", fontWeight: "700", margin: "0 0 4px" }}>LicitaPrep aprende de ti</h3>
            <p style={{ fontSize: "12px", color: "var(--color-text-secondary)", margin: "0 0 14px", lineHeight: "1.5" }}>
              Cada vez que el bot te pregunta algo y tú respondes, esa información se guarda para siempre.
              La primera licitación te preguntará mucho. La segunda, menos. La tercera, casi nada.
              Eventualmente el bot sabe TODO y prepara propuestas sin preguntarte nada.
            </p>

            <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
              {LEARNING_EXAMPLES.map((ex, i) => (
                <div key={i} style={{
                  borderRadius: "10px", overflow: "hidden",
                  border: "0.5px solid var(--color-border-tertiary)",
                }}>
                  <div style={{ padding: "10px 12px", background: "#534AB715" }}>
                    <div style={{ fontSize: "10px", color: "#534AB7", fontWeight: "600", marginBottom: "4px" }}>❓ BOT PREGUNTA:</div>
                    <div style={{ fontSize: "12px", color: "var(--color-text-primary)" }}>{ex.question}</div>
                  </div>
                  <div style={{ padding: "10px 12px", background: "#185FA510" }}>
                    <div style={{ fontSize: "10px", color: "#185FA5", fontWeight: "600", marginBottom: "4px" }}>💬 TÚ RESPONDES:</div>
                    <div style={{ fontSize: "12px", color: "var(--color-text-primary)" }}>{ex.answer}</div>
                  </div>
                  <div style={{ padding: "8px 12px", background: "#1D9E7510" }}>
                    <div style={{ fontSize: "11px", color: "#1D9E75", fontWeight: "600" }}>🧠 {ex.learned}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div style={{
            background: "var(--color-background-primary)", borderRadius: "14px",
            border: "0.5px solid var(--color-border-tertiary)", padding: "18px",
          }}>
            <h3 style={{ fontSize: "14px", fontWeight: "700", margin: "0 0 8px" }}>Base de conocimiento que crece</h3>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: "8px" }}>
              {[
                { cat: "Datos legales", items: "RUC, partida registral, poderes, representantes", n: "~25 campos" },
                { cat: "Experiencia", items: "Contratos previos, conformidades, montos", n: "Crece cada mes" },
                { cat: "Equipo técnico", items: "CVs, títulos, colegiatura, experiencia", n: "~10-20 personas" },
                { cat: "Equipamiento", items: "Servidores, herramientas, licencias software", n: "~30-50 items" },
                { cat: "Precios base", items: "Costos unitarios, márgenes por rubro", n: "Se actualiza" },
                { cat: "Plantillas", items: "Propuestas ganadoras anteriores, formatos", n: "Crece cada licitación" },
              ].map((c, i) => (
                <div key={i} style={{
                  padding: "10px 12px", borderRadius: "8px",
                  background: "var(--color-background-secondary)",
                }}>
                  <div style={{ fontSize: "12px", fontWeight: "600" }}>{c.cat}</div>
                  <div style={{ fontSize: "11px", color: "var(--color-text-secondary)", marginTop: "2px" }}>{c.items}</div>
                  <div style={{ fontSize: "10px", color: "#534AB7", marginTop: "4px", fontWeight: "600" }}>{c.n}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* VS LICITALAB */}
      {tab === "vs" && (
        <div style={{
          background: "var(--color-background-primary)", borderRadius: "14px",
          border: "0.5px solid var(--color-border-tertiary)", padding: "18px",
        }}>
          <h3 style={{ fontSize: "15px", fontWeight: "700", margin: "0 0 14px" }}>LicitaPro vs LicitaLAB</h3>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px" }}>
              <thead>
                <tr>
                  <th style={{ textAlign: "left", padding: "8px 10px", borderBottom: "1px solid var(--color-border-tertiary)", color: "var(--color-text-secondary)", fontWeight: "600" }}>Función</th>
                  <th style={{ textAlign: "center", padding: "8px 10px", borderBottom: "1px solid var(--color-border-tertiary)", color: "#534AB7", fontWeight: "700" }}>LicitaPro (tuyo)</th>
                  <th style={{ textAlign: "center", padding: "8px 10px", borderBottom: "1px solid var(--color-border-tertiary)", color: "var(--color-text-secondary)", fontWeight: "600" }}>LicitaLAB</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ["Precio", "S/ 0 (self-hosted)", "S/ 240-325/mes"],
                  ["Búsqueda de licitaciones", "✅ 12 fuentes", "✅ SEACE + Perú Compras"],
                  ["Alertas", "✅ Telegram instantáneo", "✅ Email + WhatsApp"],
                  ["Análisis IA de bases", "✅ Claude API profundo", "✅ LIA (su IA)"],
                  ["Auto-fill de anexos", "✅ COMPLETO", "❌ No tiene"],
                  ["Bot que pregunta lo que falta", "✅ Aprende de ti", "❌ No tiene"],
                  ["Genera propuesta técnica", "✅ IA adaptada", "❌ No tiene"],
                  ["Calcula precio competitivo", "✅ Con histórico", "⚠️ Solo muestra datos"],
                  ["Expediente ZIP listo", "✅ Completo", "❌ No tiene"],
                  ["Tracking post-ganar", "✅ Plazos + pagos", "❌ No tiene"],
                  ["Notificación de buena pro", "✅ Telegram + email", "⚠️ Solo cambio de estado"],
                  ["Generador de facturas", "✅ Sí", "❌ No tiene"],
                  ["Multi-empresa", "✅ 4+ empresas", "⚠️ 1 por cuenta"],
                  ["Reportes competidores", "✅ Sí", "✅ Sí"],
                  ["Personalización total", "✅ Código tuyo", "❌ Cerrado"],
                  ["Escalable a SaaS", "✅ Fase 4", "Ya es SaaS"],
                ].map(([feat, pro, lab], i) => (
                  <tr key={i} style={{ background: i % 2 === 0 ? "var(--color-background-secondary)" : "transparent" }}>
                    <td style={{ padding: "7px 10px", fontWeight: "500" }}>{feat}</td>
                    <td style={{ padding: "7px 10px", textAlign: "center" }}>{pro}</td>
                    <td style={{ padding: "7px 10px", textAlign: "center" }}>{lab}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{
            marginTop: "14px", padding: "12px", borderRadius: "10px",
            background: "#1D9E7510", border: "0.5px solid #1D9E7530",
          }}>
            <div style={{ fontSize: "12px", color: "#0F6E56", lineHeight: "1.5" }}>
              <strong>Ventaja definitiva:</strong> LicitaLAB te ayuda a ENCONTRAR licitaciones. LicitaPro te ayuda a ENCONTRAR + PREPARAR + PRESENTAR + GANAR + COBRAR. 
              Es la diferencia entre un buscador y un asistente completo que hace el trabajo por ti.
            </div>
          </div>
        </div>
      )}

      {/* MEGA PROMPT */}
      {tab === "prompt" && (
        <div style={{
          background: "var(--color-background-primary)", borderRadius: "14px",
          border: "0.5px solid var(--color-border-tertiary)", padding: "18px",
        }}>
          <h3 style={{ fontSize: "15px", fontWeight: "600", margin: "0 0 4px" }}>Mega Prompt — Claude Code</h3>
          <p style={{ fontSize: "12px", color: "var(--color-text-secondary)", margin: "0 0 12px" }}>
            Este prompt genera los 3 bots + toda la infraestructura. Cópialo a Claude Code.
          </p>
          <div style={{
            background: "#1a1a2e", borderRadius: "10px", padding: "14px",
            fontFamily: "var(--font-mono)", fontSize: "10.5px", lineHeight: "1.5",
            color: "#e0e0e0", maxHeight: "600px", overflowY: "auto",
            whiteSpace: "pre-wrap", wordBreak: "break-word",
          }}>
{`# LicitaPro Perú — Sistema Autónomo de Licitaciones
# 3 Bots de Telegram + N8N + PostgreSQL + Claude API

## ARQUITECTURA: 3 BOTS INDEPENDIENTES

### BOT 1: @LicitaRadarBot (Detectar + Analizar)
- Escanea 12 fuentes cada 30-60 min
- Filtra por: regiones, entidades, keywords, montos
- Descarga bases PDF automáticamente
- Analiza con Claude API → score viabilidad 0-100
- Envía alertas priorizadas (🔴🟡🟢)
- Comando /licitar [id] → dispara Bot 2

### BOT 2: @LicitaPrepBot (Preparar + Preguntar)
- Recibe licitaciones a preparar desde Bot 1
- Auto-fill de TODOS los anexos con datos del usuario
- Genera propuesta técnica con Claude API
- Calcula precio económico con histórico
- SI FALTA ALGO → PREGUNTA AL USUARIO
- Respuestas se guardan en knowledge_base (NUNCA repregunta)
- Genera expediente ZIP completo
- Guía paso a paso para subir a SEACE

### BOT 3: @LicitaWinBot (Ganar + Cobrar)
- Monitorea SEACE para detectar buena pro
- Notifica por Telegram + envía email (SMTP)
- Calcula timeline de plazos automáticamente
- Alerta 3 días antes de cada fecha límite
- Trackea pagos: pendientes / recibidos
- Genera actas de conformidad y facturas
- Resumen mensual de contratos activos

## ESTRUCTURA DEL PROYECTO
licitapro-peru/
├── docker-compose.yml
├── shared/
│   ├── db.py              # PostgreSQL shared
│   ├── models.py          # SQLAlchemy models
│   ├── config.py          # Settings + .env
│   ├── knowledge_base.py  # Lo que el bot aprende
│   └── email_sender.py    # SMTP sender
├── radar_bot/
│   ├── main.py            # Bot 1 entry point
│   ├── handlers/          # /hoy /buscar /licitar etc
│   ├── scrapers/          # 12 scrapers modulares
│   │   ├── seace.py
│   │   ├── ocds_api.py
│   │   ├── contratos_menores.py
│   │   ├── peru_compras.py
│   │   ├── conosce.py
│   │   ├── datos_abiertos.py
│   │   ├── open_contracting.py
│   │   ├── transparencia.py
│   │   ├── poder_judicial.py
│   │   ├── gore_portals.py
│   │   ├── sbs.py
│   │   └── essalud.py
│   ├── analyzer.py        # Claude API analysis
│   └── scorer.py          # Viability scoring
├── prep_bot/
│   ├── main.py            # Bot 2 entry point
│   ├── handlers/          # /estado /responder /aprobar
│   ├── autofill/
│   │   ├── engine.py      # Auto-fill orchestrator
│   │   ├── annexes.py     # 14+ tipos de anexos
│   │   ├── proposal.py    # Propuesta técnica IA
│   │   └── pricing.py     # Precio económico
│   ├── questioner.py      # Preguntador inteligente
│   ├── document_gen.py    # DOCX/PDF generator
│   └── zip_builder.py     # Expediente ZIP
├── win_bot/
│   ├── main.py            # Bot 3 entry point
│   ├── handlers/          # /contratos /pagos /plazos
│   ├── monitor.py         # Detecta buena pro
│   ├── timeline.py        # Calcula plazos
│   ├── payment_tracker.py # Seguimiento pagos
│   ├── invoice_gen.py     # Genera facturas
│   └── conformity_gen.py  # Actas de conformidad
├── n8n-workflows/         # 12 workflows CRON
├── templates/             # DOCX/PDF templates
│   ├── carta_presentacion.docx
│   ├── declaracion_jurada.docx
│   ├── propuesta_tecnica.docx
│   ├── propuesta_economica.xlsx
│   ├── experiencia_postor.docx
│   ├── equipo_tecnico.docx
│   ├── acta_conformidad.docx
│   └── factura.docx
├── .env
└── requirements.txt

## BASE DE DATOS PostgreSQL

-- Empresas del usuario (multi-empresa)
CREATE TABLE empresas (
  id SERIAL PRIMARY KEY,
  razon_social TEXT NOT NULL,
  ruc TEXT UNIQUE,
  representante_legal TEXT,
  dni_representante TEXT,
  partida_registral TEXT,
  direccion TEXT,
  telefono TEXT,
  email TEXT,
  rnp_numero TEXT,
  rnp_vigencia DATE,
  rubros TEXT[],
  datos_extra JSONB DEFAULT '{}'
);

-- Base de conocimiento (lo que el bot aprende)
CREATE TABLE knowledge_base (
  id SERIAL PRIMARY KEY,
  empresa_id INT REFERENCES empresas(id),
  categoria TEXT, -- legal|experiencia|equipo|equipamiento|precios|general
  clave TEXT NOT NULL,
  valor TEXT NOT NULL,
  fuente TEXT, -- 'usuario_respuesta'|'seace'|'manual'
  usado_en TEXT[], -- IDs de licitaciones donde se usó
  created_at TIMESTAMP DEFAULT NOW(),
  UNIQUE(empresa_id, categoria, clave)
);

-- Equipo técnico
CREATE TABLE equipo_tecnico (
  id SERIAL PRIMARY KEY,
  empresa_id INT REFERENCES empresas(id),
  nombre TEXT NOT NULL,
  dni TEXT,
  titulo_profesional TEXT,
  colegiatura TEXT,
  especialidad TEXT,
  anos_experiencia INT,
  cv_url TEXT,
  disponible BOOLEAN DEFAULT TRUE
);

-- Experiencia (contratos previos)
CREATE TABLE experiencia (
  id SERIAL PRIMARY KEY,
  empresa_id INT REFERENCES empresas(id),
  entidad_contratante TEXT,
  objeto TEXT,
  monto REAL,
  fecha_inicio DATE,
  fecha_fin DATE,
  conformidad_url TEXT,
  contrato_url TEXT,
  keywords TEXT[]
);

-- Licitaciones detectadas
CREATE TABLE licitaciones (
  id TEXT PRIMARY KEY,
  fuente TEXT NOT NULL,
  tipo TEXT,
  entidad TEXT NOT NULL,
  entidad_tipo TEXT,
  objeto TEXT NOT NULL,
  monto_referencial REAL,
  fecha_publicacion TIMESTAMP,
  fecha_cierre TIMESTAMP,
  estado TEXT,
  departamento TEXT,
  provincia TEXT,
  url TEXT,
  bases_urls TEXT[],
  bases_descargadas BOOLEAN DEFAULT FALSE,
  score_viabilidad REAL,
  analisis_ia JSONB,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Propuestas en preparación
CREATE TABLE propuestas (
  id SERIAL PRIMARY KEY,
  licitacion_id TEXT REFERENCES licitaciones(id),
  empresa_id INT REFERENCES empresas(id),
  estado TEXT DEFAULT 'preparando',
  -- preparando|preguntas_pendientes|listo|enviado|adjudicado|perdido
  anexos_completados INT DEFAULT 0,
  anexos_totales INT,
  preguntas_pendientes INT DEFAULT 0,
  propuesta_tecnica_url TEXT,
  propuesta_economica_url TEXT,
  precio_ofertado REAL,
  expediente_zip_url TEXT,
  fecha_envio TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Preguntas del bot al usuario
CREATE TABLE preguntas (
  id SERIAL PRIMARY KEY,
  propuesta_id INT REFERENCES propuestas(id),
  pregunta TEXT NOT NULL,
  contexto TEXT, -- por qué necesita esta info
  respuesta TEXT,
  respondida BOOLEAN DEFAULT FALSE,
  guardada_en_kb BOOLEAN DEFAULT FALSE, -- se guardó en knowledge_base?
  created_at TIMESTAMP DEFAULT NOW()
);

-- Contratos ganados (post-adjudicación)
CREATE TABLE contratos (
  id SERIAL PRIMARY KEY,
  propuesta_id INT REFERENCES propuestas(id),
  licitacion_id TEXT REFERENCES licitaciones(id),
  empresa_id INT REFERENCES empresas(id),
  monto_adjudicado REAL,
  fecha_adjudicacion DATE,
  fecha_firma_contrato DATE,
  fecha_inicio_ejecucion DATE,
  plazo_ejecucion_dias INT,
  fecha_entrega_final DATE,
  estado TEXT DEFAULT 'adjudicado',
  -- adjudicado|contrato_firmado|en_ejecucion|entregado|conformidad|pagado
  carta_fianza_monto REAL,
  carta_fianza_vencimiento DATE,
  email_notificado BOOLEAN DEFAULT FALSE
);

-- Timeline de plazos
CREATE TABLE plazos (
  id SERIAL PRIMARY KEY,
  contrato_id INT REFERENCES contratos(id),
  tipo TEXT, -- firma|fianza|entregable|conformidad|pago|garantia
  descripcion TEXT,
  fecha_limite DATE,
  completado BOOLEAN DEFAULT FALSE,
  alerta_enviada BOOLEAN DEFAULT FALSE,
  dias_antes_alerta INT DEFAULT 3
);

-- Pagos
CREATE TABLE pagos (
  id SERIAL PRIMARY KEY,
  contrato_id INT REFERENCES contratos(id),
  concepto TEXT,
  monto REAL,
  fecha_factura DATE,
  fecha_pago_esperada DATE,
  fecha_pago_real DATE,
  estado TEXT DEFAULT 'pendiente',
  -- pendiente|facturado|pagado
  factura_numero TEXT,
  factura_url TEXT
);

-- Config del usuario
CREATE TABLE user_config (
  user_id BIGINT PRIMARY KEY,
  regiones TEXT[] DEFAULT '{}',
  entidad_tipos TEXT[] DEFAULT '{}',
  keywords TEXT[] DEFAULT '{}',
  monto_min REAL DEFAULT 0,
  monto_max REAL DEFAULT 999999999,
  email_notificaciones TEXT,
  horario_alertas TEXT DEFAULT '07:00-22:00',
  telegram_radar_chat_id BIGINT,
  telegram_prep_chat_id BIGINT,
  telegram_win_chat_id BIGINT
);

## FLUJO DEL QUESTIONER (Bot 2 - clave del sistema)

1. Bot 2 recibe licitación a preparar
2. Lee bases PDF con Claude API → extrae requisitos
3. Para cada campo de cada anexo:
   a. Busca en knowledge_base → si existe, usa ese valor
   b. Busca en tabla empresas/equipo/experiencia
   c. Si NO encuentra → genera pregunta inteligente
   d. Agrupa preguntas y las envía al usuario
4. Usuario responde por Telegram
5. Bot guarda respuesta en knowledge_base
6. Con cada licitación, la KB crece
7. Después de 10-15 licitaciones, el bot ya sabe TODO

## EMAIL SMTP CONFIGURATION
Usar Gmail App Password o SendGrid free tier.
Enviar email cuando: buena pro detectada, 
plazo próximo a vencer, pago recibido.
Template HTML profesional con logo de empresa.

## EMPRESAS PRE-CONFIGURADAS
1. K&A Sistemas y Telecomunicaciones S.A.C.
   RUC: 20490765507
   Rubros: tecnología, telecomunicaciones, sistemas
2. Soluciones Informáticas MDD S.A.C.
   WhatsApp: 982 683 041 | ventas@sisac.pe
   Rubros: software, SISAC, consultoría TI
3. COMERCIAL Y MULTISERVICIOS SAN JOSE S.A.C.
   RUC: 20610570420
4. CUBS FAM S.A.C.
   RUC: 20602208070

## KEYWORDS TECNOLOGÍA
sistemas, software, tecnología, telecomunicaciones,
redes, cableado estructurado, servidores, soporte,
equipos de cómputo, desarrollo web, base de datos,
seguridad informática, cámaras, videovigilancia,
fibra óptica, UPS, biométrico, control de acceso,
mantenimiento preventivo, sistema académico, ERP,
gestión documental, hosting, data center, cloud

## REGIONES DEFAULT
Madre de Dios, Junín, Cusco (expandir según config)

## CLAVE ARQUITECTURAL
- Los 3 bots son PROCESOS INDEPENDIENTES
- Se comunican via PostgreSQL (tabla propuestas)
- Bot 1 inserta → Bot 2 procesa → Bot 3 monitorea
- Si un bot falla, los otros siguen funcionando
- Knowledge_base es COMPARTIDA entre los 3 bots
- Cada scraper es modular y tiene retry + logging
- Docker Compose levanta todo con un comando`}
          </div>
        </div>
      )}
    </div>
  );
}
