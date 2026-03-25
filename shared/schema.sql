-- ============================================
-- LICITAPRO PERÚ — SCHEMA COMPLETO
-- ============================================

-- Empresas del usuario
CREATE TABLE IF NOT EXISTS empresas (
    id SERIAL PRIMARY KEY,
    razon_social TEXT NOT NULL,
    ruc TEXT UNIQUE,
    representante_legal TEXT,
    dni_representante TEXT,
    cargo_representante TEXT DEFAULT 'Gerente General',
    partida_registral TEXT,
    direccion TEXT,
    departamento TEXT,
    telefono TEXT,
    email TEXT,
    web TEXT,
    rnp_numero TEXT,
    rnp_categoria TEXT,
    rnp_vigencia DATE,
    rubros TEXT[] DEFAULT '{}',
    activa BOOLEAN DEFAULT TRUE,
    firma_representante_path TEXT,    -- Imagen de firma del representante legal
    sello_empresa_path TEXT,          -- Imagen del sello de la empresa
    logo_empresa_path TEXT,           -- Logo de la empresa
    datos_extra JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

-- Base de conocimiento (EL CEREBRO - lo que el bot aprende)
CREATE TABLE IF NOT EXISTS knowledge_base (
    id SERIAL PRIMARY KEY,
    empresa_id INT REFERENCES empresas(id) ON DELETE CASCADE,
    categoria TEXT NOT NULL,
    clave TEXT NOT NULL,
    valor TEXT NOT NULL,
    tipo_dato TEXT DEFAULT 'texto',
    fuente TEXT DEFAULT 'usuario_respuesta',
    usado_count INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(empresa_id, categoria, clave)
);

-- Equipo técnico
CREATE TABLE IF NOT EXISTS equipo_tecnico (
    id SERIAL PRIMARY KEY,
    empresa_id INT REFERENCES empresas(id) ON DELETE CASCADE,
    nombre_completo TEXT NOT NULL,
    dni TEXT,
    titulo_profesional TEXT,
    grado_academico TEXT,
    colegiatura TEXT,
    especialidad TEXT,
    anos_experiencia INT DEFAULT 0,
    cargo_habitual TEXT,
    cv_resumen TEXT,
    disponible BOOLEAN DEFAULT TRUE,
    datos_extra JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

-- Experiencia (contratos previos)
CREATE TABLE IF NOT EXISTS experiencia (
    id SERIAL PRIMARY KEY,
    empresa_id INT REFERENCES empresas(id) ON DELETE CASCADE,
    entidad_contratante TEXT NOT NULL,
    objeto_contrato TEXT NOT NULL,
    numero_contrato TEXT,
    monto REAL,
    moneda TEXT DEFAULT 'PEN',
    fecha_inicio DATE,
    fecha_fin DATE,
    conformidad BOOLEAN DEFAULT FALSE,
    conformidad_detalle TEXT,
    keywords TEXT[] DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW()
);

-- Licitaciones detectadas
CREATE TABLE IF NOT EXISTS licitaciones (
    id TEXT PRIMARY KEY,
    fuente TEXT NOT NULL,
    tipo TEXT,
    nomenclatura TEXT,
    entidad TEXT NOT NULL,
    entidad_tipo TEXT,
    entidad_ruc TEXT,
    objeto TEXT NOT NULL,
    descripcion TEXT,
    monto_referencial REAL,
    moneda TEXT DEFAULT 'PEN',
    fecha_publicacion TIMESTAMP,
    fecha_cierre TIMESTAMP,
    fecha_buena_pro TIMESTAMP,
    estado TEXT DEFAULT 'convocado',
    departamento TEXT,
    provincia TEXT,
    distrito TEXT,
    url TEXT,
    bases_urls TEXT[] DEFAULT '{}',
    bases_descargadas BOOLEAN DEFAULT FALSE,
    bases_analisis JSONB,
    anexos_identificados JSONB,
    score_viabilidad REAL,
    score_detalle JSONB,
    notificado BOOLEAN DEFAULT FALSE,
    descartado BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Propuestas en preparación
CREATE TABLE IF NOT EXISTS propuestas (
    id SERIAL PRIMARY KEY,
    licitacion_id TEXT REFERENCES licitaciones(id),
    empresa_id INT REFERENCES empresas(id),
    estado TEXT DEFAULT 'iniciado',
    anexos_completados INT DEFAULT 0,
    anexos_totales INT DEFAULT 0,
    preguntas_pendientes INT DEFAULT 0,
    propuesta_tecnica_path TEXT,
    propuesta_economica_path TEXT,
    precio_ofertado REAL,
    precio_sugerido_min REAL,
    precio_sugerido_max REAL,
    expediente_zip_path TEXT,
    validacion JSONB,
    notas TEXT,
    fecha_envio TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Preguntas del bot al usuario
CREATE TABLE IF NOT EXISTS preguntas (
    id SERIAL PRIMARY KEY,
    propuesta_id INT REFERENCES propuestas(id) ON DELETE CASCADE,
    empresa_id INT REFERENCES empresas(id),
    campo_requerido TEXT,
    pregunta TEXT NOT NULL,
    contexto TEXT,
    opciones TEXT[],
    respuesta TEXT,
    respondida BOOLEAN DEFAULT FALSE,
    guardada_en_kb BOOLEAN DEFAULT FALSE,
    kb_categoria TEXT,
    kb_clave TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    responded_at TIMESTAMP
);

-- Contratos ganados
CREATE TABLE IF NOT EXISTS contratos (
    id SERIAL PRIMARY KEY,
    propuesta_id INT REFERENCES propuestas(id),
    licitacion_id TEXT REFERENCES licitaciones(id),
    empresa_id INT REFERENCES empresas(id),
    numero_contrato TEXT,
    monto_adjudicado REAL,
    moneda TEXT DEFAULT 'PEN',
    fecha_adjudicacion DATE,
    fecha_consentimiento DATE,
    fecha_firma_contrato DATE,
    fecha_inicio_ejecucion DATE,
    plazo_ejecucion_dias INT,
    fecha_entrega_final DATE,
    estado TEXT DEFAULT 'adjudicado',
    carta_fianza_monto REAL,
    carta_fianza_numero TEXT,
    carta_fianza_vencimiento DATE,
    email_notificado BOOLEAN DEFAULT FALSE,
    notas TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Timeline de plazos
CREATE TABLE IF NOT EXISTS plazos (
    id SERIAL PRIMARY KEY,
    contrato_id INT REFERENCES contratos(id) ON DELETE CASCADE,
    tipo TEXT NOT NULL,
    descripcion TEXT NOT NULL,
    fecha_limite DATE NOT NULL,
    completado BOOLEAN DEFAULT FALSE,
    fecha_completado DATE,
    alerta_enviada BOOLEAN DEFAULT FALSE,
    dias_antes_alerta INT DEFAULT 3,
    notas TEXT
);

-- Pagos
CREATE TABLE IF NOT EXISTS pagos (
    id SERIAL PRIMARY KEY,
    contrato_id INT REFERENCES contratos(id) ON DELETE CASCADE,
    concepto TEXT NOT NULL,
    monto REAL NOT NULL,
    moneda TEXT DEFAULT 'PEN',
    numero_factura TEXT,
    fecha_factura DATE,
    fecha_pago_esperada DATE,
    fecha_pago_real DATE,
    estado TEXT DEFAULT 'pendiente',
    comprobante TEXT,
    notas TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Config del usuario
CREATE TABLE IF NOT EXISTS user_config (
    user_id BIGINT PRIMARY KEY,
    regiones TEXT[] DEFAULT '{}',
    entidad_tipos TEXT[] DEFAULT '{}',
    keywords TEXT[] DEFAULT '{}',
    keywords_excluir TEXT[] DEFAULT '{}',
    monto_min REAL DEFAULT 0,
    monto_max REAL DEFAULT 999999999,
    empresa_default_id INT REFERENCES empresas(id),
    email_notificaciones TEXT,
    horario_inicio TEXT DEFAULT '07:00',
    horario_fin TEXT DEFAULT '22:00',
    frecuencia_resumen TEXT DEFAULT 'diario',
    activo BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Histórico de precios (para estimar precios ganadores)
CREATE TABLE IF NOT EXISTS historico_precios (
    id SERIAL PRIMARY KEY,
    objeto_keywords TEXT[],
    entidad TEXT,
    departamento TEXT,
    tipo_procedimiento TEXT,
    monto_referencial REAL,
    monto_adjudicado REAL,
    ratio_adj_ref REAL,
    proveedor_ganador TEXT,
    proveedor_ruc TEXT,
    num_postores INT,
    fecha DATE,
    fuente TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Log de scraping (para debug y monitoreo)
CREATE TABLE IF NOT EXISTS scraping_log (
    id SERIAL PRIMARY KEY,
    fuente TEXT NOT NULL,
    inicio TIMESTAMP DEFAULT NOW(),
    fin TIMESTAMP,
    registros_encontrados INT DEFAULT 0,
    registros_nuevos INT DEFAULT 0,
    errores INT DEFAULT 0,
    error_detalle TEXT,
    status TEXT DEFAULT 'running'
);

-- Índices para performance
CREATE INDEX IF NOT EXISTS idx_licit_depto ON licitaciones(departamento);
CREATE INDEX IF NOT EXISTS idx_licit_estado ON licitaciones(estado);
CREATE INDEX IF NOT EXISTS idx_licit_cierre ON licitaciones(fecha_cierre);
CREATE INDEX IF NOT EXISTS idx_licit_fuente ON licitaciones(fuente);
CREATE INDEX IF NOT EXISTS idx_licit_notif ON licitaciones(notificado);
CREATE INDEX IF NOT EXISTS idx_kb_empresa ON knowledge_base(empresa_id, categoria);
CREATE INDEX IF NOT EXISTS idx_prop_estado ON propuestas(estado);
CREATE INDEX IF NOT EXISTS idx_preg_pend ON preguntas(respondida);
CREATE INDEX IF NOT EXISTS idx_contrato_estado ON contratos(estado);
CREATE INDEX IF NOT EXISTS idx_plazos_fecha ON plazos(fecha_limite, completado);
CREATE INDEX IF NOT EXISTS idx_pagos_estado ON pagos(estado);

-- ============================================
-- DATOS INICIALES — Empresas de Kevin
-- ============================================
INSERT INTO empresas (razon_social, ruc, rubros, email, departamento) VALUES
('K & A Sistemas y Telecomunicaciones S.A.C.', '20490765507', 
 ARRAY['tecnología','telecomunicaciones','sistemas','redes','cableado estructurado','videovigilancia','soporte técnico'],
 'ventas@sisac.pe', 'Madre de Dios'),
('Soluciones Informáticas MDD S.A.C.', NULL,
 ARRAY['software','consultoría TI','sistema académico','desarrollo web','SISAC'],
 'ventas@sisac.pe', 'Madre de Dios'),
('COMERCIAL Y MULTISERVICIOS SAN JOSE S.A.C.', '20610570420',
 ARRAY['multiservicios','suministros','equipos de cómputo','útiles de oficina'],
 NULL, 'Madre de Dios'),
('CUBS FAM S.A.C.', '20602208070',
 ARRAY['servicios generales','mantenimiento','limpieza'],
 NULL, 'Madre de Dios')
ON CONFLICT (ruc) DO NOTHING;

-- Config default
INSERT INTO user_config (user_id, regiones, keywords, monto_min, monto_max, empresa_default_id, email_notificaciones) VALUES
(0, -- Se actualiza con el Telegram ID real al primer /start
 ARRAY['Madre de Dios','Junín','Cusco'],
 ARRAY['sistemas','software','tecnología','telecomunicaciones','redes','cableado estructurado','servidores','soporte técnico','equipos de cómputo','desarrollo web','base de datos','seguridad informática','cámaras','videovigilancia','fibra óptica','UPS','biométrico','control de acceso','mantenimiento preventivo','sistema académico','ERP','gestión documental','aplicativo web','hosting'],
 1000, 500000,
 1, -- K&A como empresa default
 'ventas@sisac.pe'
) ON CONFLICT (user_id) DO NOTHING;

-- Experiencia inicial de K&A
INSERT INTO experiencia (empresa_id, entidad_contratante, objeto_contrato, keywords) VALUES
(1, 'IESPP Gustavo Allende Llavería - Tarma', 'Sistema de gestión académica integral', 
 ARRAY['sistema académico','gestión académica','software educativo','enrollment','kardex','notas']),
(1, 'IIE JEC José Gálvez Barrenechea - La Oroya', 'Sistema de control de asistencia SISAC',
 ARRAY['control de asistencia','biométrico','SISAC','sistema escolar'])
ON CONFLICT DO NOTHING;
