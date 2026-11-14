-- =============================================================================
-- ESQUEMA SQL PARA SUPABASE - UNIDAD DE INSUFICIENCIA CARDÍACA (ENFERMERÍA)
-- =============================================================================

-- 1. Tabla de Pacientes
CREATE TABLE IF NOT EXISTS public.pacientes (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    fecha_alta TIMESTAMPTZ DEFAULT NOW(),
    nhc TEXT UNIQUE NOT NULL,
    nombre TEXT,
    apellidos TEXT,
    iniciales_o_nombre TEXT NOT NULL,
    edad INTEGER,
    sexo TEXT,
    talla_cm NUMERIC(5,1)
);

ALTER TABLE public.pacientes ADD COLUMN IF NOT EXISTS nombre TEXT;
ALTER TABLE public.pacientes ADD COLUMN IF NOT EXISTS apellidos TEXT;
ALTER TABLE public.pacientes ADD COLUMN IF NOT EXISTS edad INTEGER;
ALTER TABLE public.pacientes ADD COLUMN IF NOT EXISTS sexo TEXT;

-- 2. Tabla de Visitas UIC
CREATE TABLE IF NOT EXISTS public.visitas_uic (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    paciente_id UUID NOT NULL REFERENCES public.pacientes(id) ON DELETE CASCADE,
    fecha_visita DATE NOT NULL DEFAULT CURRENT_DATE,

    -- Evolución e Incidencias (Cardiológicas y No Cardiológicas)
    incidencias_bool BOOLEAN DEFAULT FALSE,
    inc_cardio_muerte BOOLEAN DEFAULT FALSE,
    inc_cardio_ingreso_ic BOOLEAN DEFAULT FALSE,
    inc_cardio_ingreso_otras BOOLEAN DEFAULT FALSE,
    inc_cardio_urgencias_ic BOOLEAN DEFAULT FALSE,
    inc_cardio_diureticos_iv BOOLEAN DEFAULT FALSE,
    inc_cardio_aumento_diureticos_vo BOOLEAN DEFAULT FALSE,
    inc_nocardio_ingreso BOOLEAN DEFAULT FALSE,
    inc_nocardio_urgencias BOOLEAN DEFAULT FALSE,
    acudio_urgencias BOOLEAN DEFAULT FALSE,
    aumento_diureticos_ambulatorio BOOLEAN DEFAULT FALSE,
    detalle_incidencias TEXT,

    -- Situación Funcional
    clase_funcional_nyha TEXT,

    -- Control Domiciliario (TA, FC, Peso medio, Glucemia opcional)
    aporta_hoja_ta_domicilio BOOLEAN DEFAULT FALSE,
    tas_media_domicilio INTEGER,
    tad_media_domicilio INTEGER,
    fc_media_domicilio INTEGER,
    peso_medio_domicilio NUMERIC(5,2),
    tiene_glucemia_domicilio BOOLEAN DEFAULT FALSE,
    glucemia_media_domicilio INTEGER,

    -- Antropometría y Constantes en Consulta
    peso_kg NUMERIC(5,2),
    diferencia_peso_kg NUMERIC(5,2),
    imc NUMERIC(5,2),
    tas_consulta INTEGER,
    tad_consulta INTEGER,
    fc_consulta INTEGER,
    ecg_realizado BOOLEAN DEFAULT FALSE,

    -- Tratamiento y Adherencia
    medicacion_habitual TEXT,
    tratamiento_cardiologico TEXT,
    tratamiento_no_cardiologico TEXT,
    texto_gaia_bruto TEXT,
    buen_cumplimiento BOOLEAN DEFAULT TRUE,
    observaciones_cumplimiento TEXT,

    -- Técnicas y Actuaciones de Enfermería / Hospital de Día
    actuacion_post_completada BOOLEAN DEFAULT FALSE,
    furosemida_iv_bolo BOOLEAN DEFAULT FALSE,
    dosis_furosemida_iv_mg INTEGER,
    perfusor_furosemida BOOLEAN DEFAULT FALSE,
    ferinject_iv BOOLEAN DEFAULT FALSE,
    dosis_ferinject_mg INTEGER,
    derivacion_hd_diureticos_iv BOOLEAN DEFAULT FALSE,
    derivacion_hd_levosimendan BOOLEAN DEFAULT FALSE,
    analitica_realizada BOOLEAN DEFAULT FALSE,
    notas_post_consulta TEXT
);

-- Migraciones de columnas adicionales en visitas_uic
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS inc_cardio_muerte BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS inc_cardio_ingreso_ic BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS inc_cardio_ingreso_otras BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS inc_cardio_urgencias_ic BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS inc_cardio_diureticos_iv BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS inc_cardio_aumento_diureticos_vo BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS inc_nocardio_ingreso BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS inc_nocardio_urgencias BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS fc_media_domicilio INTEGER;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS peso_medio_domicilio NUMERIC(5,2);
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS tiene_glucemia_domicilio BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS glucemia_media_domicilio INTEGER;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS derivacion_hd_diureticos_iv BOOLEAN DEFAULT FALSE;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS tratamiento_cardiologico TEXT;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS tratamiento_no_cardiologico TEXT;
ALTER TABLE public.visitas_uic ADD COLUMN IF NOT EXISTS texto_gaia_bruto TEXT;

-- 3. Tabla de Analíticas UIC
CREATE TABLE IF NOT EXISTS public.analiticas_uic (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    paciente_id UUID NOT NULL REFERENCES public.pacientes(id) ON DELETE CASCADE,
    fecha_analitica DATE NOT NULL DEFAULT CURRENT_DATE,
    hb NUMERIC(5,2),
    hto NUMERIC(5,2),
    plaquetas NUMERIC(7,2),
    probnp NUMERIC(10,2),
    ca125 NUMERIC(8,2),
    cr NUMERIC(5,2),
    fge NUMERIC(6,2),
    fge_texto TEXT,
    urea NUMERIC(6,2),
    k NUMERIC(5,2),
    na NUMERIC(5,2),
    gpt NUMERIC(6,2),
    inr NUMERIC(5,2),
    pcr NUMERIC(6,2),
    tpi NUMERIC(10,2),
    texto_bruto TEXT
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_pacientes_nhc ON public.pacientes(nhc);
CREATE INDEX IF NOT EXISTS idx_visitas_paciente_fecha ON public.visitas_uic(paciente_id, fecha_visita DESC);
CREATE INDEX IF NOT EXISTS idx_analiticas_paciente_fecha ON public.analiticas_uic(paciente_id, fecha_analitica DESC, created_at DESC);
