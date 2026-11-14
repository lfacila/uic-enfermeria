from datetime import date, datetime
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st

try:
    from supabase import create_client
except ImportError:
    create_client = None  # type: ignore

# -----------------------------------------------------------------------------
# CONFIGURACIÓN DE LA PÁGINA
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Consulta de Enfermería - Unidad de IC HGUV",
    page_icon="🫀",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# -----------------------------------------------------------------------------
# CLIENTE REST COMPATIBLE CON SUPABASE (SOPORTA CLAVES JWT Y sb_secret_ / sb_publishable_)
# -----------------------------------------------------------------------------
class _RESTResponse:
    def __init__(self, data: Any):
        self.data = data


class _RESTQueryBuilder:
    def __init__(self, base_url: str, headers: Dict[str, str], table_name: str):
        self._url = f"{base_url}/rest/v1/{table_name}"
        self._headers = dict(headers)
        self._params: List[tuple] = []
        self._orders: List[str] = []
        self._method = "GET"
        self._json_body: Any = None

    def select(self, columns: str = "*") -> "_RESTQueryBuilder":
        self._method = "GET"
        self._params.append(("select", columns))
        return self

    def eq(self, column: str, value: Any) -> "_RESTQueryBuilder":
        self._params.append((column, f"eq.{value}"))
        return self

    def order(self, column: str, desc: bool = False) -> "_RESTQueryBuilder":
        direction = "desc" if desc else "asc"
        self._orders.append(f"{column}.{direction}")
        return self

    def limit(self, count: int) -> "_RESTQueryBuilder":
        self._params.append(("limit", str(count)))
        return self

    def insert(self, payload: Any) -> "_RESTQueryBuilder":
        self._method = "POST"
        self._json_body = payload
        self._headers["Prefer"] = "return=representation"
        return self

    def update(self, payload: Any) -> "_RESTQueryBuilder":
        self._method = "PATCH"
        self._json_body = payload
        self._headers["Prefer"] = "return=representation"
        return self

    def execute(self) -> _RESTResponse:
        params = list(self._params)
        if self._orders:
            params.append(("order", ",".join(self._orders)))

        resp = requests.request(
            method=self._method,
            url=self._url,
            headers=self._headers,
            params=params,
            json=self._json_body,
            timeout=20,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Error Supabase ({resp.status_code}): {resp.text}")
        if resp.text.strip():
            return _RESTResponse(resp.json())
        return _RESTResponse([])


class SupabaseRESTClient:
    """Cliente ligero PostgREST compatible con la interfaz de supabase-py."""

    def __init__(self, url: str, key: str):
        clean_url = re.sub(r"/rest/v1/?$", "", url.strip()).rstrip("/")
        self.base_url = clean_url
        self.headers = {
            "apikey": key.strip(),
            "Authorization": f"Bearer {key.strip()}",
            "Content-Type": "application/json",
        }

    def table(self, table_name: str) -> _RESTQueryBuilder:
        return _RESTQueryBuilder(self.base_url, self.headers, table_name)


@st.cache_resource
def get_supabase_client() -> Any:
    """Inicializa y devuelve el cliente de Supabase usando st.secrets."""
    raw_url = st.secrets["SUPABASE_URL"].strip()
    clean_url = re.sub(r"/rest/v1/?$", "", raw_url).rstrip("/")
    key = st.secrets["SUPABASE_KEY"].strip()

    if key.startswith("sb_"):
        return SupabaseRESTClient(clean_url, key)

    try:
        return create_client(clean_url, key)
    except Exception:
        return SupabaseRESTClient(clean_url, key)


# -----------------------------------------------------------------------------
# AUTENTICACIÓN SENCILLA
# -----------------------------------------------------------------------------
def check_password() -> bool:
    """Solicita la contraseña definida en st.secrets['APP_PASSWORD'] para acceder."""
    if st.session_state.get("authenticated", False):
        return True

    st.markdown("## 🫀 Unidad de Insuficiencia Cardíaca HGUV — Consulta de Enfermería")
    st.caption("Acceso restringido al personal sanitario autorizado.")

    with st.form("login_form"):
        password_input = st.text_input("Contraseña de acceso", type="password")
        submit_login = st.form_submit_button(
            "Entrar", type="primary", use_container_width=True
        )

        if submit_login:
            expected_password = st.secrets.get("APP_PASSWORD", "")
            if password_input == expected_password and expected_password != "":
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta. Inténtelo de nuevo.")

    return False


# -----------------------------------------------------------------------------
# PARSER DE ANALÍTICAS (FORMATO LABORATORIO HGUV / ORION / GESTLAB)
# -----------------------------------------------------------------------------
PARAMETROS_ANALITICA_META = [
    ("hb", "Hb", "g/dL"),
    ("hto", "Hto", "%"),
    ("plaquetas", "Plaquetas", "10*9/L"),
    ("probnp", "NT-proBNP / ProBNP", "pg/mL"),
    ("ca125", "CA 125", "U/mL"),
    ("cr", "Creatinina", "mg/dL"),
    ("fge", "FGE (CKD-EPI)", "mL/min/1,73m²"),
    ("urea", "Urea", "mg/dL"),
    ("k", "Potasio (K)", "mEq/L"),
    ("na", "Sodio (Na)", "mEq/L"),
    ("gpt", "GPT (ALT)", "U/L"),
    ("inr", "INR", ""),
    ("pcr", "PCR", "mg/dL"),
    ("tpi", "Troponina I (TpI)", "ng/L"),
]


def _to_float(val_str: Optional[str]) -> Optional[float]:
    if not val_str:
        return None
    limpio = re.sub(r"[<>=\s*]", "", val_str).replace(",", ".")
    try:
        return round(float(limpio), 2)
    except ValueError:
        return None


def parsear_analitica_hguv(texto: str) -> Dict[str, Any]:
    """
    Extrae automáticamente Hb, Hto, Plaquetas, ProBNP, CA 125, Cr, FGE, Urea,
    K, Na, GPT, INR, PCR y TpI (si existen) a partir del texto copiado del
    informe de laboratorio del HGUV.
    """
    resultado: Dict[str, Any] = {
        "fecha_analitica": date.today(),
        "hb": None,
        "hto": None,
        "plaquetas": None,
        "probnp": None,
        "ca125": None,
        "cr": None,
        "fge": None,
        "fge_texto": None,
        "urea": None,
        "k": None,
        "na": None,
        "gpt": None,
        "inr": None,
        "pcr": None,
        "tpi": None,
    }
    if not texto or not texto.strip():
        return resultado

    # 1. Fecha de extracción o validación (DD/MM/YYYY o DD-MM-YYYY)
    m_fecha = re.search(
        r"(?:Fec\.\s*Extrac|Fecha\s+de\s+validaci[oó]n:?|Fec\.\s*Solicitud)\s*(\d{2})[/-](\d{2})[/-](\d{4})",
        texto,
        re.IGNORECASE,
    )
    if m_fecha:
        try:
            d, m, y = int(m_fecha.group(1)), int(m_fecha.group(2)), int(m_fecha.group(3))
            resultado["fecha_analitica"] = date(y, m, d)
        except ValueError:
            pass

    # Patrones específicos adaptados al formato HGUV (con o sin asteriscos * / **)
    patrones = {
        "hb": r"(?:Hemoglobina|(?<!\w)Hb(?!\s*A1c)(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "hto": r"(?:Hematocrito|(?<!\w)Hto(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "plaquetas": r"(?:Plaquetas)\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "probnp": r"(?:NT-?proBNP|ProBNP|P[eé]ptido\s+natriur[eé]tico(?:\s+tipo\s+B)?)\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?(?:[<>]=?\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "ca125": r"(?:Ant[ií]geno\s+carbohidrato\s+125|CA[\s-]*125)\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?(?:[<>]=?\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "cr": r"(?:Ser\s*-\s*)?(?:Creatinina|(?<!\w)Cr(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "urea": r"(?:Ser\s*-\s*)?(?:Urea)\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "k": r"(?:Ser\s*-\s*)?(?:Potasio|(?<!\w)K(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "na": r"(?:Ser\s*-\s*)?(?:Sodio|(?<!\w)Na(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "gpt": r"(?:Alanina\s+aminotransferasa(?:\s*\(?(?:ALT|GPT)\)?)?|Transaminasa\s+GPT|(?<!\w)GPT(?:\s*/\s*ALT)?|(?<!\w)ALT(?:\s*/\s*GPT)?)\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "inr": r"(?:[IÍ]ndice\s+Internacional\s+Normalizado|(?<!\w)INR(?!\w))\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "pcr": r"(?:Prote[ií]na\s+C\s+Reactiva|(?<!\w)PCR(?!\w))\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?(?:[<>]=?\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "tpi": r"(?:Troponina\s*[IT](?:\s*\([^)]*\))?|hsTn[IT]|(?<!\w)TpI(?!\w))\s*(?:\*+\s*)?(?:[<>]=?\s*)?([0-9]+(?:[.,][0-9]+)?)",
    }

    for clave, regex in patrones.items():
        match = re.search(regex, texto, re.IGNORECASE)
        if match:
            resultado[clave] = _to_float(match.group(1))

    # FGE (puede venir como ">=90 mL/min/1,73m2" o como número "58 mL/min/1,73m2")
    m_fge = re.search(
        r"(?:FG\s*Estimado(?:\s*CKD-?EPI)?|CKD-?EPI|Filtrado\s+Glomerular(?:\s+Estimado)?)\s*(?:\*+\s*)?([<>]=?\s*[0-9]+(?:[.,][0-9]+)?|[0-9]+(?:[.,][0-9]+)?)",
        texto,
        re.IGNORECASE,
    )
    if m_fge:
        bruto_fge = m_fge.group(1).strip().replace(" ", "")
        resultado["fge_texto"] = bruto_fge
        resultado["fge"] = _to_float(bruto_fge)

    return resultado


def generar_texto_comparativo_analitica(
    paciente: Dict[str, Any],
    analitica_actual: Dict[str, Any],
    analitica_previa: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Genera un bloque de texto plano con los parámetros presentes en la analítica
    (omitiendo los que no existan, como TpI si no se solicitó) y comparándolos
    con la última analítica registrada si existe.
    """
    fecha_act = str(analitica_actual.get("fecha_analitica", date.today().isoformat()))[:10]
    fecha_prev = (
        str(analitica_previa.get("fecha_analitica", ""))[:10]
        if analitica_previa
        else None
    )

    partes_resumen: List[str] = []
    lineas_detalle: List[str] = []

    for clave, etiqueta, unidad in PARAMETROS_ANALITICA_META:
        val_act = analitica_actual.get(clave)
        if val_act is None:
            continue  # Si no está en la analítica (ej. TpI, CA125, INR...), no aparece

        # Mostrar ">=90" si aplica para FGE
        if clave == "fge" and analitica_actual.get("fge_texto"):
            val_str = f"{analitica_actual['fge_texto']}"
        else:
            val_num = float(val_act)
            val_str = f"{int(val_num)}" if val_num.is_integer() else f"{val_num:g}"

        unid_str = f" {unidad}" if unidad else ""

        # Comparación con la previa si existe
        comp_str = ""
        if analitica_previa and analitica_previa.get(clave) is not None:
            try:
                prev_num = float(analitica_previa[clave])
                act_num = float(val_act)
                diff = round(act_num - prev_num, 2)
                prev_fmt = f"{int(prev_num)}" if prev_num.is_integer() else f"{prev_num:g}"
                if diff > 0:
                    comp_str = f" (previo {prev_fmt}; +{diff:g})"
                elif diff < 0:
                    comp_str = f" (previo {prev_fmt}; {diff:g})"
                else:
                    comp_str = f" (previo {prev_fmt}; =)"
            except (TypeError, ValueError):
                pass

        partes_resumen.append(f"{etiqueta}: {val_str}{unid_str}{comp_str}")
        lineas_detalle.append(f"- {etiqueta}: {val_str}{unid_str}{comp_str}")

    encabezado = f"ANALÍTICA SANGUÍNEA ({fecha_act})"
    if fecha_prev:
        encabezado += f" [Comparada con previa del {fecha_prev}]"

    if not partes_resumen:
        return f"{encabezado}: Sin parámetros registrados."

    return (
        f"{encabezado}\n"
        f"Resumen en línea: {' | '.join(partes_resumen)}\n\n"
        f"Desglose por parámetro:\n" + "\n".join(lineas_detalle)
    )


# -----------------------------------------------------------------------------
# PARSER DE TRATAMIENTO DE ABUCASIS (GAIA) — CARDIOLÓGICO VS NO CARDIOLÓGICO
# -----------------------------------------------------------------------------
# Diccionario de fármacos cardiológicos (principios activos y marcas comerciales en España)
FARMACOS_CARDIO_CATALOGO: List[Tuple[str, str, List[str]]] = [
    (
        "ARNI (Sacubitrilo/Valsartán)",
        "PILAR_ARNI_IECA_ARA2",
        ["sacubitrilo", "entresto", "neparvis"],
    ),
    (
        "IECA",
        "PILAR_ARNI_IECA_ARA2",
        [
            "enalapril",
            "ramipril",
            "lisinopril",
            "perindopril",
            "captopril",
            "quinapril",
            "benazepril",
            "fosinopril",
            "trandolapril",
            "zofenopril",
            "tritace",
            "acovil",
            "renitec",
            "coversyl",
            "bipreterax",
        ],
    ),
    (
        "ARA-II",
        "PILAR_ARNI_IECA_ARA2",
        [
            "valsartan",
            "valsartán",
            "losartan",
            "losartán",
            "candesartan",
            "candesartán",
            "irbesartan",
            "irbesartán",
            "olmesartan",
            "olmesartán",
            "telmisartan",
            "telmisartán",
            "azilsartan",
            "eprosartan",
            "atacand",
            "cozaar",
            "diovan",
            "karvea",
            "aprovel",
            "olmetec",
            "openvas",
            "micardis",
        ],
    ),
    (
        "Betabloqueante",
        "PILAR_BB",
        [
            "bisoprolol",
            "carvedilol",
            "nebivolol",
            "metoprolol",
            "atenolol",
            "propranolol",
            "labetalol",
            "celiprolol",
            "esmolol",
            "emconcor",
            "euradal",
            "coropres",
            "lobivon",
            "silostar",
            "beloken",
            "seloken",
            "sumial",
            "tenormin",
            "trandate",
        ],
    ),
    (
        "ARM (Antagonista Mineralocorticoide)",
        "PILAR_ARM",
        [
            "espironolactona",
            "eplerenona",
            "finerenona",
            "aldactone",
            "inspra",
            "kerendia",
        ],
    ),
    (
        "iSGLT2",
        "PILAR_ISGLT2",
        [
            "dapagliflozina",
            "empagliflozina",
            "canagliflozina",
            "ertugliflozina",
            "sotagliflozina",
            "forxiga",
            "edistride",
            "jardiance",
            "invokana",
            "xigduo",
            "synjardy",
        ],
    ),
    (
        "Diurético",
        "DIURETICO",
        [
            "furosemida",
            "torasemida",
            "clortalidona",
            "hidroclorotiazida",
            "indapamida",
            "acetazolamida",
            "bumetanida",
            "amilorida",
            "seguril",
            "sutril",
            "dilutol",
            "isodiur",
            "higrotona",
            "hidrosaluretil",
            "edemox",
            "natrilix",
            "tertensif",
            "ameride",
        ],
    ),
    (
        "Modulador IC / Inotropo / Cronotropo",
        "OTRO_IC",
        [
            "ivabradina",
            "vericiguat",
            "digoxina",
            "procoralan",
            "verquvo",
            "lanacordin",
        ],
    ),
    (
        "Anticoagulante",
        "ANTICOAGULANTE",
        [
            "apixaban",
            "apixabán",
            "rivaroxaban",
            "rivaroxabán",
            "dabigatran",
            "dabigatrán",
            "edoxaban",
            "edoxabán",
            "acenocumarol",
            "warfarina",
            "enoxaparina",
            "bemiparina",
            "tinzaparina",
            "fondaparinux",
            "eliquis",
            "xarelto",
            "pradaxa",
            "lixiana",
            "sintrom",
            "aldocumar",
            "clexane",
            "hibor",
            "innohep",
        ],
    ),
    (
        "Antiagregante",
        "ANTIAGREGANTE",
        [
            "acido acetilsalicilico",
            "ácido acetilsalicílico",
            "acetilsalicilico",
            "clopidogrel",
            "ticagrelor",
            "prasugrel",
            "triflusal",
            "adiro",
            "tromalyt",
            "aas ",
            "plavix",
            "iscover",
            "vatoud",
            "brilique",
            "efient",
            "duoplavin",
            "disgren",
        ],
    ),
    (
        "Hipolipemiante",
        "HIPOLIPEMIANTE",
        [
            "atorvastatina",
            "rosuvastatina",
            "simvastatina",
            "pitavastatina",
            "pravastatina",
            "fluvastatina",
            "lovastatina",
            "ezetimiba",
            "bempedoico",
            "inclisiran",
            "inclisirán",
            "evolocumab",
            "alirocumab",
            "icosapento",
            "fenofibrato",
            "gemfibrozilo",
            "omega-3",
            "zarator",
            "cardyl",
            "prevencor",
            "crestor",
            "provisacor",
            "ezetrol",
            "zient",
            "vytorin",
            "inegy",
            "rosuzet",
            "atomerol",
            "rosumibe",
            "zenon",
            "lipocomb",
            "orvatez",
            "nustendi",
            "nilemdo",
            "repatha",
            "praluent",
            "leqvio",
            "vazkepa",
            "omacor",
            "livazo",
            "alipza",
            "secambi",
        ],
    ),
    (
        "Antiarrítmico / Antianginoso / Vasodilatador / Ca-antagonista",
        "OTRO_CARDIO",
        [
            "amiodarona",
            "dronedarona",
            "flecainida",
            "propafenona",
            "sotalol",
            "amlodipino",
            "lercanidipino",
            "nifedipino",
            "felodipino",
            "manidipino",
            "barnidipino",
            "diltiazem",
            "verapamilo",
            "nitroglicerina",
            "isosorbida",
            "mononitrato",
            "dinitrato",
            "ranolazina",
            "molsidomina",
            "hidralazina",
            "minoxidil",
            "doxazosina",
            "trangorex",
            "multaq",
            "apocard",
            "rytmonorm",
            "norvas",
            "astucor",
            "zanidip",
            "adalat",
            "masdil",
            "manidon",
            "ranexa",
            "cafenitrina",
            "trinispray",
            "minitran",
            "cordiplast",
            "uniket",
            "coronur",
            "carduran",
        ],
    ),
    (
        "Quelante / Suplemento de Potasio o Hierro Cardio",
        "ELECTROLITOS_CARDIO",
        [
            "patiromer",
            "ciclosilicato",
            "poliestireno sulfonato",
            "cloruro de potasio",
            "ascorbato potasico",
            "bicarbonato potasico",
            "veltassa",
            "lokelma",
            "resincalcio",
            "sorbisterit",
            "potasion",
            "boi-k",
            "ferinject",
            "carboximaltosa",
        ],
    ),
]

# Palabras ruido típicas de cabeceras de Abucasis / GAIA que no son medicamentos
GAIA_NOISE_PATTERNS = [
    r"^tratamiento\s+farmacol[oó]gico",
    r"^hoja\s+de\s+tratamiento",
    r"^paciente\s*:",
    r"^n[ºo°]\s*sip",
    r"^n[ºo°]\s*historia",
    r"^fecha\s+de\s+impresi[oó]n",
    r"^m[eé]dico\s*:",
    r"^centro\s*:",
    r"^prescripci[oó]n",
    r"^posolog[ií]a\s+vigente",
    r"^cr[oó]nico\s*$",
    r"^agudo\s*$",
    r"^a\s+demanda\s*$",
    r"^activo\s*$",
    r"^página\s+\d+",
]


def parsear_tratamiento_gaia(texto_gaia: str) -> Dict[str, Any]:
    """
    Procesa un texto copiado y pegado de la hoja de tratamiento de Abucasis (GAIA),
    identificando cada fármaco (por nombre comercial o principio activo genérico)
    con su posología, y separándolos en Tratamientos Cardiológicos y No Cardiológicos.
    """
    resultado = {
        "cardiologicos": [],
        "no_cardiologicos": [],
        "pilares": {
            "ARNI / IECA / ARA-II": False,
            "Betabloqueante": False,
            "ARM": False,
            "iSGLT2": False,
        },
        "texto_cardiologico": "",
        "texto_no_cardiologico": "",
    }
    if not texto_gaia or not texto_gaia.strip():
        return resultado

    lineas_brutas = [ln.strip() for ln in texto_gaia.splitlines() if ln.strip()]

    # Si una línea es solo una pauta posológica (ej. "1 cada 24 horas", "0-1-0", "1 comprimido / 12h"),
    # la unimos a la línea del fármaco anterior (muy habitual al copiar tablas de GAIA).
    bloques_medicacion: List[str] = []
    patron_solo_posologia = re.compile(
        r"^(?:\d+(?:[.,]\d+)?\s*(?:comp(?:rimidos?)?|c[aá]ps(?:ulas?)?|sobres?|pulsaciones|UI|mg|mcg|gotas|ml)?\s*(?:cada|/)\s*\d+\s*(?:h|horas?|d[ií]as?)|[012½]+-[012½]+-[012½]+(?:-[012½]+)?|(?:desayuno|comida|almuerzo|cena|noche)\b.*|\d+\s+al\s+d[ií]a.*)$",
        re.IGNORECASE,
    )

    for ln in lineas_brutas:
        # Ignorar cabeceras administrativas puras de GAIA
        if any(re.search(pat, ln, re.IGNORECASE) for pat in GAIA_NOISE_PATTERNS):
            continue
        # Ignorar líneas que son solo fechas sueltas (ej. "12/04/2025 - 12/10/2025")
        if re.match(r"^\d{2}[/-]\d{2}[/-]\d{4}(?:\s*-\s*\d{2}[/-]\d{2}[/-]\d{4})?$", ln):
            continue

        if bloques_medicacion and patron_solo_posologia.match(ln):
            bloques_medicacion[-1] = f"{bloques_medicacion[-1]} — Posología: {ln}"
        else:
            # Limpiar exceso de espacios o tabuladores de tabla GAIA
            ln_limpia = re.sub(r"\s{2,}|\t+", " | ", ln)
            bloques_medicacion.append(ln_limpia)

    cardio_list: List[str] = []
    nocardio_list: List[str] = []

    for item in bloques_medicacion:
        item_lower = item.lower()
        es_cardio = False
        clases_detectadas: List[str] = []

        for nombre_clase, codigo_pilar, keywords in FARMACOS_CARDIO_CATALOGO:
            if any(kw in item_lower for kw in keywords):
                es_cardio = True
                if nombre_clase not in clases_detectadas:
                    clases_detectadas.append(nombre_clase)
                if codigo_pilar == "PILAR_ARNI_IECA_ARA2":
                    resultado["pilares"]["ARNI / IECA / ARA-II"] = True
                elif codigo_pilar == "PILAR_BB":
                    resultado["pilares"]["Betabloqueante"] = True
                elif codigo_pilar == "PILAR_ARM":
                    resultado["pilares"]["ARM"] = True
                elif codigo_pilar == "PILAR_ISGLT2":
                    resultado["pilares"]["iSGLT2"] = True

        if es_cardio:
            etiqueta_clase = " + ".join(clases_detectadas)
            cardio_list.append(f"• [{etiqueta_clase}] {item}")
        else:
            # Comprobar si parece una línea de medicación válida (contiene dosis mg/mcg/UI/g/ml/comp o posología)
            nocardio_list.append(f"• {item}")

    resultado["cardiologicos"] = cardio_list
    resultado["no_cardiologicos"] = nocardio_list
    resultado["texto_cardiologico"] = "\n".join(cardio_list)
    resultado["texto_no_cardiologico"] = "\n".join(nocardio_list)
    return resultado


# -----------------------------------------------------------------------------
# FUNCIONES DE ACCESO A DATOS (SUPABASE)
# -----------------------------------------------------------------------------
def obtener_nombre_completo_paciente(paciente: Dict[str, Any]) -> str:
    """Devuelve 'Apellidos, Nombre' o 'iniciales_o_nombre' como fallback."""
    nombre = (paciente.get("nombre") or "").strip()
    apellidos = (paciente.get("apellidos") or "").strip()
    if apellidos and nombre:
        return f"{apellidos}, {nombre}"
    if nombre or apellidos:
        return f"{nombre} {apellidos}".strip()
    return str(paciente.get("iniciales_o_nombre") or "-")


def buscar_paciente_por_nhc(supabase: Any, nhc: str) -> Optional[Dict[str, Any]]:
    """Busca un paciente por NHC en la tabla 'pacientes'."""
    res = supabase.table("pacientes").select("*").eq("nhc", nhc).limit(1).execute()
    if res.data:
        return res.data[0]
    return None


def crear_paciente(
    supabase: Any,
    nhc: str,
    nombre: str,
    apellidos: str,
    edad: int,
    sexo: str,
    talla_cm: float,
) -> Dict[str, Any]:
    """Crea un nuevo paciente en la tabla 'pacientes' con Nombre, Apellidos, Edad, Sexo y Talla."""
    talla_val = (
        int(round(talla_cm))
        if float(talla_cm).is_integer()
        else round(float(talla_cm), 1)
    )
    nombre_limpio = nombre.strip()
    apellidos_limpios = apellidos.strip()
    nombre_completo = (
        f"{apellidos_limpios}, {nombre_limpio}"
        if (apellidos_limpios and nombre_limpio)
        else f"{nombre_limpio} {apellidos_limpios}".strip()
    )
    payload = {
        "nhc": nhc.strip(),
        "nombre": nombre_limpio,
        "apellidos": apellidos_limpios,
        "iniciales_o_nombre": nombre_completo,
        "edad": int(edad),
        "sexo": sexo,
        "talla_cm": talla_val,
    }
    res = supabase.table("pacientes").insert(payload).execute()
    return res.data[0]


def actualizar_datos_paciente(
    supabase: Any,
    paciente_id: Any,
    nombre: str,
    apellidos: str,
    edad: int,
    sexo: str,
    nueva_talla_cm: float,
) -> Dict[str, Any]:
    """Actualiza los datos antropométricos y demográficos del paciente."""
    talla_val = (
        int(round(nueva_talla_cm))
        if float(nueva_talla_cm).is_integer()
        else round(float(nueva_talla_cm), 1)
    )
    nombre_limpio = nombre.strip()
    apellidos_limpios = apellidos.strip()
    nombre_completo = (
        f"{apellidos_limpios}, {nombre_limpio}"
        if (apellidos_limpios and nombre_limpio)
        else f"{nombre_limpio} {apellidos_limpios}".strip()
    )
    payload = {
        "nombre": nombre_limpio,
        "apellidos": apellidos_limpios,
        "iniciales_o_nombre": nombre_completo,
        "edad": int(edad),
        "sexo": sexo,
        "talla_cm": talla_val,
    }
    res = (
        supabase.table("pacientes")
        .update(payload)
        .eq("id", paciente_id)
        .execute()
    )
    return res.data[0] if res.data else payload


def obtener_visitas_paciente(supabase: Any, paciente_id: Any) -> List[Dict[str, Any]]:
    """Obtiene todas las visitas del paciente ordenadas desde la más reciente."""
    try:
        res = (
            supabase.table("visitas_uic")
            .select("*")
            .eq("paciente_id", paciente_id)
            .order("fecha_visita", desc=True)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception:
        res = (
            supabase.table("visitas_uic")
            .select("*")
            .eq("paciente_id", paciente_id)
            .order("fecha_visita", desc=True)
            .execute()
        )
    return res.data or []


def obtener_analiticas_paciente(
    supabase: Any, paciente_id: Any
) -> List[Dict[str, Any]]:
    """Obtiene todas las analíticas del paciente ordenadas desde la más reciente."""
    try:
        res = (
            supabase.table("analiticas_uic")
            .select("*")
            .eq("paciente_id", paciente_id)
            .order("fecha_analitica", desc=True)
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def cargar_estado_paciente(
    paciente: Dict[str, Any],
    visitas: List[Dict[str, Any]],
    analiticas: List[Dict[str, Any]],
) -> None:
    """Actualiza st.session_state con los datos del paciente activo."""
    st.session_state["paciente_actual"] = paciente
    st.session_state["visitas_paciente"] = visitas
    st.session_state["analiticas_paciente"] = analiticas
    st.session_state["paciente_no_encontrado"] = False

    ultima_visita = visitas[0] if visitas else None
    med_previa = (
        (ultima_visita.get("medicacion_habitual") or "") if ultima_visita else ""
    )
    trat_cardio_prev = (
        (ultima_visita.get("tratamiento_cardiologico") or "") if ultima_visita else ""
    )
    trat_nocardio_prev = (
        (ultima_visita.get("tratamiento_no_cardiologico") or "")
        if ultima_visita
        else ""
    )

    st.session_state["pre_medicacion_habitual"] = med_previa
    st.session_state["trat_cardio_edit"] = trat_cardio_prev
    st.session_state["trat_nocardio_edit"] = trat_nocardio_prev
    st.session_state["talla_editable_cm"] = float(paciente.get("talla_cm") or 165.0)
    st.session_state["borrador_analitica"] = None
    st.session_state["texto_pre_consulta"] = None
    st.session_state["texto_post_consulta"] = None
    st.session_state["texto_analitica_hc"] = None
    st.session_state["texto_tratamiento_hc"] = None


def limpiar_paciente_actual() -> None:
    """Limpia el paciente activo en pantalla para poder buscar o dar de alta otro paciente."""
    claves_conservar = {"authenticated"}
    for k in list(st.session_state.keys()):
        if k not in claves_conservar:
            del st.session_state[k]


# -----------------------------------------------------------------------------
# GENERACIÓN DE TEXTOS PLANOS PARA HISTORIA CLÍNICA (HC)
# -----------------------------------------------------------------------------
def construir_resumen_incidencias(
    incidencias_bool: bool,
    cardio_seleccionadas: List[str],
    nocardio_seleccionadas: List[str],
    detalle_libre: str,
) -> str:
    """Construye el texto estructurado de incidencias cardiológicas y no cardiológicas."""
    if not incidencias_bool:
        return "Sin incidencias clínicas (cardiológicas ni no cardiológicas) desde la última visita."

    bloques = []
    if cardio_seleccionadas:
        bloques.append(f"Cardiológicas: {', '.join(cardio_seleccionadas)}")
    else:
        bloques.append("Cardiológicas: Ninguna")

    if nocardio_seleccionadas:
        bloques.append(f"No cardiológicas: {', '.join(nocardio_seleccionadas)}")
    else:
        bloques.append("No cardiológicas: Ninguna")

    detalle = detalle_libre.strip()
    if detalle:
        bloques.append(f"Observaciones: {detalle}")

    return " | ".join(bloques)


def construir_lineas_actuaciones_enfermeria(datos: Dict[str, Any]) -> List[str]:
    """Devuelve la lista de técnicas y actuaciones realizadas por enfermería."""
    actuaciones: List[str] = []

    if datos.get("ecg_realizado"):
        actuaciones.append("ECG de 12 derivaciones realizado")

    if datos.get("analitica_realizada"):
        actuaciones.append("Extracción de analítica sanguínea realizada")

    if datos.get("ferinject_iv"):
        dosis_fe = datos.get("dosis_ferinject_mg")
        actuaciones.append(
            f"Administración de Hierro carboximaltosa (Ferinject) IV: {dosis_fe} mg"
        )

    if datos.get("furosemida_iv_bolo"):
        dosis_furo = datos.get("dosis_furosemida_iv_mg")
        dosis_txt = f"{dosis_furo} mg" if dosis_furo is not None else "según pauta"
        actuaciones.append(
            f"Administración de Diurético IV en bolo (Furosemida): {dosis_txt}"
        )

    if datos.get("perfusor_furosemida"):
        actuaciones.append("Colocación de infusor / perfusor de Furosemida")

    if datos.get("derivacion_hd_diureticos_iv"):
        actuaciones.append("Enviado / derivado a Hospital de Día para Diuréticos IV")

    if datos.get("derivacion_hd_levosimendan"):
        actuaciones.append("Enviado / derivado a Hospital de Día para Levosimendán")

    return actuaciones


def construir_linea_analitica_para_consulta(
    analiticas_paciente: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """
    Si el paciente tiene analíticas subidas, construye la línea de resultados
    analíticos (con comparativa respecto a la previa si existe) para incluirla
    en el texto plano general de la Historia Clínica. Si no hay analíticas, devuelve None.
    """
    if not analiticas_paciente:
        return None

    actual = analiticas_paciente[0]
    previa = analiticas_paciente[1] if len(analiticas_paciente) >= 2 else None
    fecha_act = str(actual.get("fecha_analitica", ""))[:10]
    fecha_prev = str(previa.get("fecha_analitica", ""))[:10] if previa else None

    items: List[str] = []
    for clave, etiqueta, unidad in PARAMETROS_ANALITICA_META:
        val_act = actual.get(clave)
        if val_act is None:
            continue

        if clave == "fge" and actual.get("fge_texto"):
            val_str = f"{actual['fge_texto']}"
        else:
            val_num = float(val_act)
            val_str = f"{int(val_num)}" if val_num.is_integer() else f"{val_num:g}"

        unid_str = f" {unidad}" if unidad else ""
        comp_str = ""
        if previa and previa.get(clave) is not None:
            try:
                prev_num = float(previa[clave])
                act_num = float(val_act)
                diff = round(act_num - prev_num, 2)
                prev_fmt = (
                    f"{int(prev_num)}" if prev_num.is_integer() else f"{prev_num:g}"
                )
                if diff > 0:
                    comp_str = f" (previo {prev_fmt}; +{diff:g})"
                elif diff < 0:
                    comp_str = f" (previo {prev_fmt}; {diff:g})"
                else:
                    comp_str = f" (previo {prev_fmt}; =)"
            except (TypeError, ValueError):
                pass

        items.append(f"{etiqueta}: {val_str}{unid_str}{comp_str}")

    if not items:
        return None

    cabecera = f"Analítica ({fecha_act})"
    if fecha_prev:
        cabecera += f" [comparada con {fecha_prev}]"
    return f"- {cabecera}: " + " | ".join(items)


def generar_texto_consulta_enfermeria(
    paciente: Dict[str, Any],
    datos_visita: Dict[str, Any],
    talla_cm: float,
    resumen_incidencias_str: str,
    analiticas_paciente: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Genera el texto plano completo de la consulta de enfermería para copiar y pegar
    en la Historia Clínica (HC), incluyendo los resultados analíticos subidos (si los hay).
    Si no hay glucemia domiciliaria ni analítica, los omite sin dejar líneas vacías.
    """
    fecha_str = str(datos_visita.get("fecha_visita", date.today().isoformat()))[:10]
    nhc = paciente.get("nhc", "-")
    nombre_completo = obtener_nombre_completo_paciente(paciente)
    edad = paciente.get("edad")
    sexo = paciente.get("sexo")
    info_demografica = []
    if edad:
        info_demografica.append(f"{edad} años")
    if sexo:
        info_demografica.append(str(sexo))
    demog_str = f" ({', '.join(info_demografica)})" if info_demografica else ""

    # 1. Control domiciliario (TA, FC, Peso medio, y Glucemia SOLO si está presente)
    if datos_visita.get("aporta_hoja_ta_domicilio"):
        partes_dom = []
        tas_dom = datos_visita.get("tas_media_domicilio")
        tad_dom = datos_visita.get("tad_media_domicilio")
        if tas_dom is not None and tad_dom is not None:
            partes_dom.append(f"TA media {tas_dom}/{tad_dom} mmHg")

        fc_dom = datos_visita.get("fc_media_domicilio")
        if fc_dom is not None:
            partes_dom.append(f"FC media {fc_dom} lpm")

        peso_dom = datos_visita.get("peso_medio_domicilio")
        if peso_dom is not None and float(peso_dom) > 0:
            partes_dom.append(f"Peso medio {float(peso_dom):.1f} kg")

        # Glucemia domiciliaria: solo aparece si está marcada y tiene valor
        if (
            datos_visita.get("tiene_glucemia_domicilio")
            and datos_visita.get("glucemia_media_domicilio") is not None
        ):
            partes_dom.append(
                f"Glucemia capilar media {datos_visita.get('glucemia_media_domicilio')} mg/dL"
            )

        texto_dom = "Aporta registro domiciliario: " + " | ".join(partes_dom) + "."
    else:
        texto_dom = "No aporta hoja de registro domiciliario."
        # Incluso si no trae hoja de TA pero se anotó glucemia domiciliaria explícitamente:
        if (
            datos_visita.get("tiene_glucemia_domicilio")
            and datos_visita.get("glucemia_media_domicilio") is not None
        ):
            texto_dom += f" Glucemia capilar media en domicilio: {datos_visita.get('glucemia_media_domicilio')} mg/dL."

    # 2. Antropometría y constantes en consulta
    peso = datos_visita.get("peso_kg")
    dif_peso = datos_visita.get("diferencia_peso_kg")
    imc = datos_visita.get("imc")

    if dif_peso is not None:
        signo = "+" if dif_peso > 0 else ""
        dif_str = f"{signo}{dif_peso:.2f} kg respecto a visita previa"
        if dif_peso >= 2.0:
            dif_str += " [ALERTA CLÍNICA: Ganancia ponderal >= 2 kg]"
    else:
        dif_str = "Sin registro de peso previo para comparar"

    tas_con = datos_visita.get("tas_consulta")
    tad_con = datos_visita.get("tad_consulta")
    fc_con = datos_visita.get("fc_consulta")

    # 3. Actuaciones de enfermería realizadas
    actuaciones_enf = construir_lineas_actuaciones_enfermeria(datos_visita)
    texto_actuaciones = (
        "; ".join(actuaciones_enf) + "."
        if actuaciones_enf
        else "Valoración clínica y educación sanitaria."
    )

    # 4. Resultados analíticos (si se han subido para este paciente)
    linea_analitica = construir_linea_analitica_para_consulta(analiticas_paciente)

    # 5. Adherencia y tratamiento
    cumplimiento_str = (
        "Buen cumplimiento terapéutico"
        if datos_visita.get("buen_cumplimiento")
        else "Mal / irregular cumplimiento terapéutico"
    )
    obs_cump = (datos_visita.get("observaciones_cumplimiento") or "").strip()
    if obs_cump:
        cumplimiento_str += f" ({obs_cump})"

    med_habitual = (
        datos_visita.get("medicacion_habitual") or ""
    ).strip() or "No especificada"

    notas_post = (datos_visita.get("notas_post_consulta") or "").strip()

    lineas = [
        f"CONSULTA DE ENFERMERÍA - UNIDAD DE INSUFICIENCIA CARDÍACA ({fecha_str})",
        f"Paciente: {nombre_completo}{demog_str} | NHC: {nhc}",
        "-" * 72,
        f"- Incidencias desde última visita: {resumen_incidencias_str}",
        f"- Situación Funcional: Clase funcional NYHA {datos_visita.get('clase_funcional_nyha', '-')}",
        f"- Control Domiciliario: {texto_dom}",
        f"- Antropometría en Consulta: Peso actual {peso:.1f} kg ({dif_str}) | Talla: {talla_cm:g} cm | IMC: {imc:.2f} kg/m2",
        f"- Constantes en Consulta: TA {tas_con}/{tad_con} mmHg | FC {fc_con} lpm",
        f"- Actuaciones / Técnicas de Enfermería: {texto_actuaciones}",
    ]
    if linea_analitica:
        lineas.append(linea_analitica)

    lineas.extend(
        [
            f"- Adherencia Terapéutica: {cumplimiento_str}.",
            "- Tratamiento Actual:",
            f"{med_habitual}",
        ]
    )
    if notas_post:
        lineas.extend(["", f"- Observaciones / Notas de Enfermería:\n{notas_post}"])

    return "\n".join(lineas)


def mostrar_cuadro_copia_hc(
    titulo: str, texto_plano: str, key_prefix: str, height: int = 260
) -> None:
    """
    Muestra tanto un cuadro de texto plano editable (st.text_area) como un bloque
    con botón de copia rápida en 1 clic (st.code) para pegar en la Historia Clínica.
    """
    st.markdown(f"#### 📋 {titulo}")
    col_txt, col_code = st.columns(2)
    with col_txt:
        st.caption(
            "Cuadro de texto plano editable (puede seleccionarlo todo con Ctrl+A / Cmd+A y copiar):"
        )
        texto_editado = st.text_area(
            "Texto plano para Historia Clínica (HC)",
            value=texto_plano,
            height=height,
            key=f"{key_prefix}_textarea",
            label_visibility="collapsed",
        )
    with col_code:
        st.caption(
            "Copia rápida en 1 clic (pulse el icono de copiar en la esquina superior derecha):"
        )
        st.code(texto_editado, language="text")


# -----------------------------------------------------------------------------
# INTERFAZ PRINCIPAL
# -----------------------------------------------------------------------------
def main() -> None:
    if not check_password():
        return

    # Cabecera superior con botón de cierre de sesión
    col_title, col_logout = st.columns([6, 1])
    with col_title:
        st.title("🫀 Consulta de Enfermería — Unidad de Insuficiencia Cardíaca (HGUV)")
    with col_logout:
        st.write("")
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    # Inicializar cliente Supabase
    try:
        supabase = get_supabase_client()
    except Exception as exc:
        st.error(
            f"Error al inicializar el cliente de Supabase. Verifique `.streamlit/secrets.toml`: {exc}"
        )
        return

    st.divider()

    # =========================================================================
    # SECCIÓN SUPERIOR: IDENTIFICACIÓN DEL PACIENTE
    # =========================================================================
    st.subheader("Identificación del Paciente")

    col_nhc, col_btn, col_nuevo_pac = st.columns([3, 1.2, 1.5])
    with col_nhc:
        nhc_input = st.text_input(
            "Número de Historia Clínica (NHC)",
            value=st.session_state.get("nhc_buscado", ""),
            placeholder="Introduzca el NHC del paciente (ej. H1484620)...",
        )
    with col_btn:
        st.write("")
        st.write("")
        buscar_clicked = st.button(
            "🔍 Buscar / Iniciar", type="primary", use_container_width=True
        )
    with col_nuevo_pac:
        st.write("")
        st.write("")
        if st.button(
            "🔄 Cambiar / Otro Paciente",
            use_container_width=True,
            help="Limpia la ficha actual en pantalla para introducir el NHC de otro paciente",
        ):
            limpiar_paciente_actual()
            st.rerun()

    if buscar_clicked:
        nhc_limpio = nhc_input.strip()
        if not nhc_limpio:
            st.warning("Por favor, introduzca un NHC válido.")
        else:
            st.session_state["nhc_buscado"] = nhc_limpio
            try:
                paciente = buscar_paciente_por_nhc(supabase, nhc_limpio)
                if paciente:
                    visitas = obtener_visitas_paciente(supabase, paciente["id"])
                    analiticas = obtener_analiticas_paciente(supabase, paciente["id"])
                    cargar_estado_paciente(paciente, visitas, analiticas)
                else:
                    st.session_state["paciente_actual"] = None
                    st.session_state["visitas_paciente"] = []
                    st.session_state["analiticas_paciente"] = []
                    st.session_state["paciente_no_encontrado"] = True
            except Exception as exc:
                st.error(f"Error al consultar Supabase: {exc}")

    # -------------------------------------------------------------------------
    # CASO A: PACIENTE NO EXISTE -> FORMULARIO DE ALTA (NOMBRE, APELLIDOS, EDAD, SEXO, TALLA)
    # -------------------------------------------------------------------------
    if st.session_state.get("paciente_no_encontrado"):
        nhc_nuevo = st.session_state.get("nhc_buscado", nhc_input.strip())
        st.warning(
            f"No existe ningún paciente registrado con el NHC **{nhc_nuevo}**. "
            "Complete sus datos para darlo de alta:"
        )
        with st.form("form_nuevo_paciente"):
            c_nom, c_ape = st.columns(2)
            with c_nom:
                nombre_nuevo = st.text_input(
                    "Nombre *", placeholder="Ej. EDUARDO"
                )
            with c_ape:
                apellidos_nuevo = st.text_input(
                    "Apellidos *", placeholder="Ej. NAVARRO MORENO"
                )

            c_edad, c_sexo, c_talla = st.columns(3)
            with c_edad:
                edad_nueva = st.number_input(
                    "Edad (años) *", min_value=15, max_value=115, value=72, step=1
                )
            with c_sexo:
                sexo_nuevo = st.selectbox(
                    "Sexo *", options=["Hombre", "Mujer", "Otro"]
                )
            with c_talla:
                talla_cm_nuevo = st.number_input(
                    "Talla (cm) *",
                    min_value=100.0,
                    max_value=230.0,
                    value=165.0,
                    step=1.0,
                )

            crear_btn = st.form_submit_button(
                "➕ Crear Paciente e Iniciar Consulta",
                type="primary",
                use_container_width=True,
            )

            if crear_btn:
                if not nombre_nuevo.strip() or not apellidos_nuevo.strip():
                    st.error("Por favor, indique el Nombre y los Apellidos del paciente.")
                else:
                    try:
                        nuevo_pac = crear_paciente(
                            supabase,
                            nhc=nhc_nuevo,
                            nombre=nombre_nuevo,
                            apellidos=apellidos_nuevo,
                            edad=int(edad_nueva),
                            sexo=sexo_nuevo,
                            talla_cm=talla_cm_nuevo,
                        )
                        cargar_estado_paciente(nuevo_pac, [], [])
                        st.success(
                            f"Paciente {apellidos_nuevo}, {nombre_nuevo} (NHC: {nhc_nuevo}) creado correctamente."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"No se pudo crear el paciente en Supabase: {exc}")

    # -------------------------------------------------------------------------
    # RESUMEN DEL PACIENTE ACTIVO (SI SE HA SELECCIONADO UN PACIENTE)
    # -------------------------------------------------------------------------
    paciente_actual = st.session_state.get("paciente_actual")
    if paciente_actual:
        visitas_paciente: List[Dict[str, Any]] = st.session_state.get(
            "visitas_paciente", []
        )
        ultima_visita: Optional[Dict[str, Any]] = (
            visitas_paciente[0] if visitas_paciente else None
        )

        if ultima_visita:
            ult_fecha = str(ultima_visita.get("fecha_visita", "-"))[:10]
            ult_peso = ultima_visita.get("peso_kg")
            ult_peso_str = (
                f"{float(ult_peso):.1f} kg" if ult_peso is not None else "No registrado"
            )
            ult_tas = ultima_visita.get("tas_consulta")
            ult_tad = ultima_visita.get("tad_consulta")
            ult_ta_str = (
                f"{ult_tas}/{ult_tad} mmHg"
                if (ult_tas is not None and ult_tad is not None)
                else "No registrada"
            )
            ult_med = (
                ultima_visita.get("medicacion_habitual") or ""
            ).strip() or "Sin medicación previa registrada"
        else:
            ult_fecha = "Sin visitas previas"
            ult_peso_str = "Sin registro previo"
            ult_ta_str = "Sin registro previo"
            ult_med = "Sin medicación previa registrada"

        nombre_mostrar = obtener_nombre_completo_paciente(paciente_actual)
        edad_guardada = paciente_actual.get("edad") or "-"
        sexo_guardado = paciente_actual.get("sexo") or "-"
        talla_guardada = paciente_actual.get("talla_cm")
        talla_guardada_str = f"{talla_guardada} cm" if talla_guardada else "No registrada"

        st.info(
            f"👤 **Paciente:** {nombre_mostrar} | **NHC:** `{paciente_actual.get('nhc')}` | "
            f"**Edad:** {edad_guardada} años | **Sexo:** {sexo_guardado} | "
            f"📏 **Talla:** {talla_guardada_str}  \n"
            f"📅 **Última visita:** {ult_fecha} | "
            f"⚖️ **Último peso:** {ult_peso_str} | "
            f"🩺 **Última TA:** {ult_ta_str}  \n"
            f"💊 **Medicación habitual previa:** {ult_med}"
        )

        with st.expander("✏️ Editar datos demográficos o talla del paciente"):
            ec1, ec2, ec3, ec4, ec5 = st.columns([2, 2, 1, 1, 1])
            with ec1:
                edit_nombre = st.text_input(
                    "Nombre",
                    value=paciente_actual.get("nombre")
                    or paciente_actual.get("iniciales_o_nombre")
                    or "",
                    key="edit_pac_nombre",
                )
            with ec2:
                edit_apellidos = st.text_input(
                    "Apellidos",
                    value=paciente_actual.get("apellidos") or "",
                    key="edit_pac_apellidos",
                )
            with ec3:
                edit_edad = st.number_input(
                    "Edad",
                    min_value=15,
                    max_value=115,
                    value=int(paciente_actual.get("edad") or 70),
                    step=1,
                    key="edit_pac_edad",
                )
            with ec4:
                opciones_sexo = ["Hombre", "Mujer", "Otro"]
                idx_sexo = (
                    opciones_sexo.index(paciente_actual.get("sexo"))
                    if paciente_actual.get("sexo") in opciones_sexo
                    else 0
                )
                edit_sexo = st.selectbox(
                    "Sexo", options=opciones_sexo, index=idx_sexo, key="edit_pac_sexo"
                )
            with ec5:
                talla_actual_cm = st.number_input(
                    "Talla (cm)",
                    min_value=100.0,
                    max_value=230.0,
                    value=float(
                        st.session_state.get(
                            "talla_editable_cm", talla_guardada or 165.0
                        )
                    ),
                    step=1.0,
                    key="input_talla_paciente",
                )
                st.session_state["talla_editable_cm"] = talla_actual_cm

            if st.button("Guardar cambios del paciente"):
                try:
                    pac_act = actualizar_datos_paciente(
                        supabase,
                        paciente_id=paciente_actual["id"],
                        nombre=edit_nombre,
                        apellidos=edit_apellidos,
                        edad=int(edit_edad),
                        sexo=edit_sexo,
                        nueva_talla_cm=talla_actual_cm,
                    )
                    st.session_state["paciente_actual"] = pac_act
                    st.success("Datos del paciente actualizados correctamente.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Error al actualizar paciente: {exc}")
        talla_actual_cm = float(
            st.session_state.get("talla_editable_cm", talla_guardada or 165.0)
        )

    st.divider()

    # =========================================================================
    # PESTAÑAS PRINCIPALES (EL CUADRO DE MANDOS SIEMPRE ESTÁ DISPONIBLE)
    # =========================================================================
    tab_consulta, tab_analitica, tab_tratamiento, tab_historico, tab_dashboard = (
        st.tabs(
            [
                "🩺 1. Consulta y Actuaciones Enfermería",
                "🧪 2. Analítica (Extractor HGUV)",
                "💊 3. Tratamiento (Abucasis / GAIA)",
                "📈 4. Evolución e Histórico",
                "📊 5. Cuadro de Mandos (Dashboard UIC)",
            ]
        )
    )

    # =========================================================================
    # PESTAÑA 1: CONSULTA Y ACTUACIONES DE ENFERMERÍA
    # =========================================================================
    with tab_consulta:
        if not paciente_actual:
            st.info(
                "👆 Busque o cree un paciente por su **NHC** en la parte superior para iniciar la consulta."
            )
        else:
            st.subheader("Valoración Clínica y Actuaciones de Enfermería en Consulta")

            col_fecha, col_nyha = st.columns(2)
            with col_fecha:
                fecha_visita_input = st.date_input(
                    "Fecha de la visita", value=date.today()
                )
            with col_nyha:
                clase_nyha = st.selectbox(
                    "Clase Funcional NYHA",
                    options=["I", "II", "III", "IV"],
                    index=1,
                )

            st.markdown("---")
            # -----------------------------------------------------------------
            # BLOQUE A: EVOLUCIÓN E INCIDENCIAS (CARDIOLÓGICAS Y NO CARDIOLÓGICAS)
            # -----------------------------------------------------------------
            st.markdown("#### 1. Evolución e Incidencias desde la última visita")
            incidencias_bool = st.checkbox(
                "¿Ha tenido incidencias desde la última visita?"
            )

            inc_cardio_muerte = False
            inc_cardio_ingreso_ic = False
            inc_cardio_ingreso_otras = False
            inc_cardio_urgencias_ic = False
            inc_cardio_diureticos_iv = False
            inc_cardio_aumento_diureticos_vo = False
            inc_nocardio_ingreso = False
            inc_nocardio_urgencias = False
            detalle_incidencias_libre = ""

            if incidencias_bool:
                col_inc_cardio, col_inc_nocardio = st.columns(2)
                with col_inc_cardio:
                    st.markdown(
                        "##### 🫀 Incidencias Cardiológicas *(puede marcar varias)*"
                    )
                    inc_cardio_muerte = st.checkbox("Muerte")
                    inc_cardio_ingreso_ic = st.checkbox("Ingreso por IC")
                    inc_cardio_ingreso_otras = st.checkbox(
                        "Ingreso por otras patologías cardiológicas"
                    )
                    inc_cardio_urgencias_ic = st.checkbox("Urgencias por IC")
                    inc_cardio_diureticos_iv = st.checkbox("Uso de diuréticos IV")
                    inc_cardio_aumento_diureticos_vo = st.checkbox(
                        "Aumento de diuréticos orales"
                    )

                with col_inc_nocardio:
                    st.markdown(
                        "##### 🏥 Incidencias No Cardiológicas *(puede marcar varias)*"
                    )
                    inc_nocardio_ingreso = st.checkbox("Ingreso no cardiológico")
                    inc_nocardio_urgencias = st.checkbox("Urgencias no cardiológicas")

                detalle_incidencias_libre = st.text_area(
                    "Detalle adicional de las incidencias (fechas, motivo, evolución...)",
                    placeholder="Describa fechas o detalles clínicos de los ingresos, urgencias o ajustes de diurético...",
                )

            cardio_seleccionadas: List[str] = []
            if inc_cardio_muerte:
                cardio_seleccionadas.append("Muerte")
            if inc_cardio_ingreso_ic:
                cardio_seleccionadas.append("Ingreso por IC")
            if inc_cardio_ingreso_otras:
                cardio_seleccionadas.append(
                    "Ingreso por otras patologías cardiológicas"
                )
            if inc_cardio_urgencias_ic:
                cardio_seleccionadas.append("Urgencias por IC")
            if inc_cardio_diureticos_iv:
                cardio_seleccionadas.append("Uso de diuréticos IV")
            if inc_cardio_aumento_diureticos_vo:
                cardio_seleccionadas.append("Aumento de diuréticos orales")

            nocardio_seleccionadas: List[str] = []
            if inc_nocardio_ingreso:
                nocardio_seleccionadas.append("Ingreso no cardiológico")
            if inc_nocardio_urgencias:
                nocardio_seleccionadas.append("Urgencias no cardiológicas")

            resumen_incidencias_str = construir_resumen_incidencias(
                incidencias_bool=incidencias_bool,
                cardio_seleccionadas=cardio_seleccionadas,
                nocardio_seleccionadas=nocardio_seleccionadas,
                detalle_libre=detalle_incidencias_libre,
            )

            st.markdown("---")
            # -----------------------------------------------------------------
            # BLOQUE B: CONTROL DOMICILIARIO (TA, FC, PESO MEDIO Y GLUCEMIA OPCIONAL)
            # -----------------------------------------------------------------
            st.markdown(
                "#### 2. Control Domiciliario (TA, FC, Peso medio y Glucemia si procede)"
            )
            aporta_hoja_ta_domicilio = st.checkbox(
                "Aporta hoja de registro domiciliario (TA / FC / Peso)"
            )

            tas_media_domicilio: Optional[int] = None
            tad_media_domicilio: Optional[int] = None
            fc_media_domicilio: Optional[int] = None
            peso_medio_domicilio: Optional[float] = None

            if aporta_hoja_ta_domicilio:
                c_dom1, c_dom2, c_dom3, c_dom4 = st.columns(4)
                with c_dom1:
                    tas_media_domicilio = int(
                        st.number_input(
                            "TAS media domicilio (mmHg)",
                            min_value=50,
                            max_value=260,
                            value=120,
                            step=1,
                        )
                    )
                with c_dom2:
                    tad_media_domicilio = int(
                        st.number_input(
                            "TAD media domicilio (mmHg)",
                            min_value=30,
                            max_value=160,
                            value=72,
                            step=1,
                        )
                    )
                with c_dom3:
                    fc_media_domicilio = int(
                        st.number_input(
                            "FC media domicilio (lpm)",
                            min_value=25,
                            max_value=220,
                            value=68,
                            step=1,
                        )
                    )
                with c_dom4:
                    peso_prev_dom = (
                        float(ultima_visita["peso_kg"])
                        if (ultima_visita and ultima_visita.get("peso_kg") is not None)
                        else 70.0
                    )
                    peso_medio_domicilio = float(
                        st.number_input(
                            "Peso medio domicilio (kg)",
                            min_value=25.0,
                            max_value=250.0,
                            value=peso_prev_dom,
                            step=0.1,
                            format="%.1f",
                        )
                    )
            else:
                st.caption("ℹ️ Registro: *No aporta hoja de registro domiciliario*.")

            # Glucemia domiciliaria opcional: si no se marca, no aparece en el informe
            col_gluc_chk, col_gluc_val = st.columns([2, 2])
            with col_gluc_chk:
                tiene_glucemia_domicilio = st.checkbox(
                    "¿Aporta control de glucemia domiciliaria? *(Si no se marca, no aparecerá en el texto)*"
                )
            with col_gluc_val:
                glucemia_media_domicilio: Optional[int] = None
                if tiene_glucemia_domicilio:
                    glucemia_media_domicilio = int(
                        st.number_input(
                            "Glucemia media domicilio (mg/dL)",
                            min_value=30,
                            max_value=600,
                            value=110,
                            step=1,
                        )
                    )

            st.markdown("---")
            # -----------------------------------------------------------------
            # BLOQUE C: ANTROPOMETRÍA Y CONSTANTES EN CONSULTA
            # -----------------------------------------------------------------
            st.markdown("#### 3. Antropometría y Constantes en Consulta")

            peso_previo_val: Optional[float] = None
            if ultima_visita and ultima_visita.get("peso_kg") is not None:
                try:
                    peso_previo_val = float(ultima_visita["peso_kg"])
                except (TypeError, ValueError):
                    peso_previo_val = None

            valor_peso_inicial = (
                peso_previo_val if peso_previo_val is not None else 70.0
            )

            col_peso, col_dif, col_imc = st.columns(3)
            with col_peso:
                peso_kg = st.number_input(
                    "Peso actual en consulta (kg)",
                    min_value=25.0,
                    max_value=250.0,
                    value=float(valor_peso_inicial),
                    step=0.1,
                    format="%.1f",
                )

            if peso_previo_val is not None:
                diferencia_peso_kg: Optional[float] = round(
                    peso_kg - peso_previo_val, 2
                )
            else:
                diferencia_peso_kg = None

            altura_m = talla_actual_cm / 100.0 if talla_actual_cm > 0 else 1.65
            imc_calculado = round(peso_kg / (altura_m**2), 2)

            with col_dif:
                if diferencia_peso_kg is not None:
                    signo_delta = "+" if diferencia_peso_kg > 0 else ""
                    st.metric(
                        label="Diferencia de peso vs. última visita",
                        value=f"{signo_delta}{diferencia_peso_kg:.2f} kg",
                        delta=f"Previo: {peso_previo_val:.1f} kg",
                        delta_color="inverse",
                    )
                else:
                    st.metric(
                        label="Diferencia de peso vs. última visita",
                        value="N/A",
                        delta="Sin peso previo",
                        delta_color="off",
                    )

            with col_imc:
                st.metric(
                    label="IMC calculado",
                    value=f"{imc_calculado:.2f} kg/m²",
                    delta=f"Talla: {talla_actual_cm:g} cm",
                    delta_color="off",
                )

            if diferencia_peso_kg is not None and diferencia_peso_kg >= 2.0:
                st.error("ALERTA CLÍNICA: Ganancia ponderal >= 2 kg")

            col_tas_c, col_tad_c, col_fc_c = st.columns(3)
            with col_tas_c:
                tas_consulta = int(
                    st.number_input(
                        "TAS consulta (mmHg)",
                        min_value=50,
                        max_value=260,
                        value=120,
                        step=1,
                    )
                )
            with col_tad_c:
                tad_consulta = int(
                    st.number_input(
                        "TAD consulta (mmHg)",
                        min_value=30,
                        max_value=160,
                        value=70,
                        step=1,
                    )
                )
            with col_fc_c:
                fc_consulta = int(
                    st.number_input(
                        "FC consulta (lpm)",
                        min_value=25,
                        max_value=220,
                        value=68,
                        step=1,
                    )
                )

            st.markdown("---")
            # -----------------------------------------------------------------
            # BLOQUE D: TODAS LAS ACTUACIONES QUE REALIZA ENFERMERÍA
            # -----------------------------------------------------------------
            st.markdown(
                "#### 4. Técnicas y Actuaciones realizadas por Enfermería (Consulta / Hospital de Día)"
            )
            col_act1, col_act2 = st.columns(2)

            with col_act1:
                ecg_realizado = st.checkbox("📈 ECG realizado")
                analitica_realizada = st.checkbox("🩸 Extracción de analítica realizada")

                ferinject_iv = st.checkbox(
                    "💉 Hierro IV — Ferinject (Hierro carboximaltosa)"
                )
                dosis_ferinject_mg: Optional[int] = None
                if ferinject_iv:
                    dosis_ferinject_mg = int(
                        st.selectbox(
                            "Dosis de Ferinject IV (mg)",
                            options=[500, 1000, 1500],
                            index=1,
                        )
                    )

            with col_act2:
                furosemida_iv_bolo = st.checkbox(
                    "💊 Diuréticos IV en bolo (Furosemida / Seguril)"
                )
                dosis_furosemida_iv_mg: Optional[int] = None
                if furosemida_iv_bolo:
                    dosis_furosemida_iv_mg = int(
                        st.number_input(
                            "Dosis de Diurético IV en bolo (mg)",
                            min_value=10,
                            max_value=500,
                            value=40,
                            step=10,
                        )
                    )

                perfusor_furosemida = st.checkbox(
                    "📟 Colocar infusor / perfusor de Furosemida"
                )
                derivacion_hd_diureticos_iv = st.checkbox(
                    "🏥 Enviar a Hospital de Día para Diuréticos IV"
                )
                derivacion_hd_levosimendan = st.checkbox(
                    "🏥 Enviar a Hospital de Día para Levosimendán"
                )

            st.markdown("---")
            # -----------------------------------------------------------------
            # BLOQUE E: ADHERENCIA, MEDICACIÓN Y NOTAS
            # -----------------------------------------------------------------
            st.markdown("#### 5. Tratamiento Habitual, Adherencia y Observaciones")
            medicacion_habitual = st.text_area(
                "Medicación habitual (precargada de la visita anterior o de la Pestaña 3 de Abucasis/GAIA)",
                key="pre_medicacion_habitual",
                height=120,
                placeholder="Detalle los fármacos y dosis actuales del paciente...",
            )
            c_adh1, c_adh2 = st.columns([1, 2])
            with c_adh1:
                buen_cumplimiento = st.checkbox(
                    "Buen cumplimiento terapéutico", value=True
                )
            with c_adh2:
                observaciones_cumplimiento = st.text_input(
                    "Observaciones de cumplimiento",
                    placeholder="Ej. Olvidos ocasionales, dudas con pauta diurética...",
                )

            notas_post_consulta = st.text_area(
                "Notas / Observaciones de Enfermería",
                placeholder="Educación sanitaria impartida, tolerancia al hierro/diurético IV, vía canalizada, próxima cita...",
                height=80,
            )

            st.write("")
            guardar_pre_btn = st.button(
                "💾 Guardar Consulta de Enfermería y Generar Texto Plano para la HC",
                type="primary",
                use_container_width=True,
            )

            if guardar_pre_btn:
                try:
                    acudio_urgencias_any = bool(
                        inc_cardio_urgencias_ic or inc_nocardio_urgencias
                    )
                    aumento_diureticos_any = bool(
                        inc_cardio_diureticos_iv or inc_cardio_aumento_diureticos_vo
                    )
                    hay_actuacion_post = any(
                        [
                            furosemida_iv_bolo,
                            perfusor_furosemida,
                            ferinject_iv,
                            derivacion_hd_diureticos_iv,
                            derivacion_hd_levosimendan,
                            analitica_realizada,
                            bool(notas_post_consulta.strip()),
                        ]
                    )

                    payload_visita: Dict[str, Any] = {
                        "paciente_id": paciente_actual["id"],
                        "fecha_visita": fecha_visita_input.isoformat(),
                        "incidencias_bool": bool(incidencias_bool),
                        "inc_cardio_muerte": bool(inc_cardio_muerte)
                        if incidencias_bool
                        else False,
                        "inc_cardio_ingreso_ic": bool(inc_cardio_ingreso_ic)
                        if incidencias_bool
                        else False,
                        "inc_cardio_ingreso_otras": bool(inc_cardio_ingreso_otras)
                        if incidencias_bool
                        else False,
                        "inc_cardio_urgencias_ic": bool(inc_cardio_urgencias_ic)
                        if incidencias_bool
                        else False,
                        "inc_cardio_diureticos_iv": bool(inc_cardio_diureticos_iv)
                        if incidencias_bool
                        else False,
                        "inc_cardio_aumento_diureticos_vo": bool(
                            inc_cardio_aumento_diureticos_vo
                        )
                        if incidencias_bool
                        else False,
                        "inc_nocardio_ingreso": bool(inc_nocardio_ingreso)
                        if incidencias_bool
                        else False,
                        "inc_nocardio_urgencias": bool(inc_nocardio_urgencias)
                        if incidencias_bool
                        else False,
                        "acudio_urgencias": acudio_urgencias_any
                        if incidencias_bool
                        else False,
                        "aumento_diureticos_ambulatorio": aumento_diureticos_any
                        if incidencias_bool
                        else False,
                        "detalle_incidencias": (
                            resumen_incidencias_str if incidencias_bool else None
                        ),
                        "clase_funcional_nyha": clase_nyha,
                        "aporta_hoja_ta_domicilio": bool(aporta_hoja_ta_domicilio),
                        "tas_media_domicilio": (
                            tas_media_domicilio if aporta_hoja_ta_domicilio else None
                        ),
                        "tad_media_domicilio": (
                            tad_media_domicilio if aporta_hoja_ta_domicilio else None
                        ),
                        "fc_media_domicilio": (
                            fc_media_domicilio if aporta_hoja_ta_domicilio else None
                        ),
                        "peso_medio_domicilio": (
                            peso_medio_domicilio if aporta_hoja_ta_domicilio else None
                        ),
                        "tiene_glucemia_domicilio": bool(tiene_glucemia_domicilio),
                        "glucemia_media_domicilio": (
                            glucemia_media_domicilio
                            if tiene_glucemia_domicilio
                            else None
                        ),
                        "peso_kg": float(peso_kg),
                        "diferencia_peso_kg": (
                            float(diferencia_peso_kg)
                            if diferencia_peso_kg is not None
                            else 0.0
                        ),
                        "imc": float(imc_calculado),
                        "tas_consulta": int(tas_consulta),
                        "tad_consulta": int(tad_consulta),
                        "fc_consulta": int(fc_consulta),
                        "ecg_realizado": bool(ecg_realizado),
                        "analitica_realizada": bool(analitica_realizada),
                        "ferinject_iv": bool(ferinject_iv),
                        "dosis_ferinject_mg": (
                            int(dosis_ferinject_mg) if ferinject_iv else None
                        ),
                        "furosemida_iv_bolo": bool(furosemida_iv_bolo),
                        "dosis_furosemida_iv_mg": (
                            int(dosis_furosemida_iv_mg)
                            if furosemida_iv_bolo
                            else None
                        ),
                        "perfusor_furosemida": bool(perfusor_furosemida),
                        "derivacion_hd_diureticos_iv": bool(
                            derivacion_hd_diureticos_iv
                        ),
                        "derivacion_hd_levosimendan": bool(
                            derivacion_hd_levosimendan
                        ),
                        "medicacion_habitual": medicacion_habitual.strip(),
                        "buen_cumplimiento": bool(buen_cumplimiento),
                        "observaciones_cumplimiento": observaciones_cumplimiento.strip()
                        or None,
                        "notas_post_consulta": notas_post_consulta.strip() or None,
                        "actuacion_post_completada": bool(hay_actuacion_post),
                    }

                    supabase.table("visitas_uic").insert(payload_visita).execute()

                    visitas_actualizadas = obtener_visitas_paciente(
                        supabase, paciente_actual["id"]
                    )
                    st.session_state["visitas_paciente"] = visitas_actualizadas

                    st.session_state["texto_pre_consulta"] = (
                        generar_texto_consulta_enfermeria(
                            paciente=st.session_state["paciente_actual"],
                            datos_visita=payload_visita,
                            talla_cm=talla_actual_cm,
                            resumen_incidencias_str=resumen_incidencias_str,
                            analiticas_paciente=st.session_state.get(
                                "analiticas_paciente"
                            ),
                        )
                    )
                    st.success("✅ Consulta guardada correctamente en Supabase.")
                except Exception as exc:
                    st.error(f"Error al guardar la consulta: {exc}")

            if st.session_state.get("texto_pre_consulta"):
                st.markdown("---")
                mostrar_cuadro_copia_hc(
                    titulo="Texto Plano para Copiar a la Historia Clínica (HC)",
                    texto_plano=st.session_state["texto_pre_consulta"],
                    key_prefix="consulta_hc",
                )
                if st.button(
                    "👤 Finalizar con este paciente y pasar al siguiente",
                    key="btn_fin_pac_tab1",
                    use_container_width=True,
                ):
                    limpiar_paciente_actual()
                    st.rerun()

            # Desplegable opcional para actualizar una visita previa tras consulta médica
            if visitas_paciente:
                st.markdown("---")
                with st.expander(
                    "🔄 Actualizar técnicas / tratamientos post-consulta de una visita ya guardada"
                ):
                    opciones_visita = {
                        v["id"]: (
                            f"Visita del {str(v.get('fecha_visita'))[:10]} "
                            f"(Peso: {v.get('peso_kg', '-')} kg | TA: {v.get('tas_consulta', '-')}/{v.get('tad_consulta', '-')} mmHg)"
                        )
                        for v in visitas_paciente
                    }
                    visita_id_sel = st.selectbox(
                        "Seleccione la visita a modificar",
                        options=list(opciones_visita.keys()),
                        format_func=lambda vid: opciones_visita[vid],
                    )
                    visita_sel = next(
                        (v for v in visitas_paciente if v["id"] == visita_id_sel),
                        visitas_paciente[0],
                    )

                    u1, u2 = st.columns(2)
                    with u1:
                        u_ecg = st.checkbox(
                            "ECG realizado",
                            value=bool(visita_sel.get("ecg_realizado") or False),
                            key=f"u_ecg_{visita_id_sel}",
                        )
                        u_analitica = st.checkbox(
                            "Extracción de analítica realizada",
                            value=bool(visita_sel.get("analitica_realizada") or False),
                            key=f"u_ana_{visita_id_sel}",
                        )
                        u_fe = st.checkbox(
                            "Hierro IV (Ferinject)",
                            value=bool(visita_sel.get("ferinject_iv") or False),
                            key=f"u_fe_{visita_id_sel}",
                        )
                        u_fe_dosis: Optional[int] = None
                        if u_fe:
                            ops_fe = [500, 1000, 1500]
                            d_prev = visita_sel.get("dosis_ferinject_mg")
                            idx_fe = (
                                ops_fe.index(int(d_prev))
                                if (d_prev and int(d_prev) in ops_fe)
                                else 1
                            )
                            u_fe_dosis = int(
                                st.selectbox(
                                    "Dosis Ferinject (mg)",
                                    options=ops_fe,
                                    index=idx_fe,
                                    key=f"u_fe_d_{visita_id_sel}",
                                )
                            )
                    with u2:
                        u_furo = st.checkbox(
                            "Diuréticos IV en bolo",
                            value=bool(visita_sel.get("furosemida_iv_bolo") or False),
                            key=f"u_furo_{visita_id_sel}",
                        )
                        u_furo_dosis: Optional[int] = None
                        if u_furo:
                            u_furo_dosis = int(
                                st.number_input(
                                    "Dosis Diurético IV (mg)",
                                    min_value=10,
                                    max_value=500,
                                    value=int(
                                        visita_sel.get("dosis_furosemida_iv_mg") or 40
                                    ),
                                    step=10,
                                    key=f"u_furo_d_{visita_id_sel}",
                                )
                            )
                        u_perf = st.checkbox(
                            "Colocar infusor / perfusor de Furosemida",
                            value=bool(visita_sel.get("perfusor_furosemida") or False),
                            key=f"u_perf_{visita_id_sel}",
                        )
                        u_hd_diu = st.checkbox(
                            "Enviar a Hospital de Día para Diuréticos IV",
                            value=bool(
                                visita_sel.get("derivacion_hd_diureticos_iv") or False
                            ),
                            key=f"u_hddiu_{visita_id_sel}",
                        )
                        u_hd_levo = st.checkbox(
                            "Enviar a Hospital de Día para Levosimendán",
                            value=bool(
                                visita_sel.get("derivacion_hd_levosimendan") or False
                            ),
                            key=f"u_hdlevo_{visita_id_sel}",
                        )

                    u_notas = st.text_area(
                        "Notas post-consulta",
                        value=visita_sel.get("notas_post_consulta") or "",
                        key=f"u_notas_{visita_id_sel}",
                    )

                    if st.button(
                        "💾 Actualizar Actuaciones de esta Visita",
                        key=f"btn_upd_{visita_id_sel}",
                    ):
                        payload_upd = {
                            "actuacion_post_completada": True,
                            "ecg_realizado": bool(u_ecg),
                            "analitica_realizada": bool(u_analitica),
                            "ferinject_iv": bool(u_fe),
                            "dosis_ferinject_mg": int(u_fe_dosis) if u_fe else None,
                            "furosemida_iv_bolo": bool(u_furo),
                            "dosis_furosemida_iv_mg": (
                                int(u_furo_dosis) if u_furo else None
                            ),
                            "perfusor_furosemida": bool(u_perf),
                            "derivacion_hd_diureticos_iv": bool(u_hd_diu),
                            "derivacion_hd_levosimendan": bool(u_hd_levo),
                            "notas_post_consulta": u_notas.strip() or None,
                        }
                        supabase.table("visitas_uic").update(payload_upd).eq(
                            "id", visita_id_sel
                        ).execute()
                        st.session_state["visitas_paciente"] = obtener_visitas_paciente(
                            supabase, paciente_actual["id"]
                        )
                        visita_merged = {**visita_sel, **payload_upd}
                        st.session_state["texto_pre_consulta"] = (
                            generar_texto_consulta_enfermeria(
                                paciente=paciente_actual,
                                datos_visita=visita_merged,
                                talla_cm=talla_actual_cm,
                                resumen_incidencias_str=visita_sel.get(
                                    "detalle_incidencias"
                                )
                                or "Sin incidencias registradas.",
                                analiticas_paciente=st.session_state.get(
                                    "analiticas_paciente"
                                ),
                            )
                        )
                        st.success("Visita actualizada y texto regenerado.")
                        st.rerun()

    # =========================================================================
    # PESTAÑA 2: ANALÍTICA (PASO 1: EXTRAER Y REVISAR -> PASO 2: GRABAR EN BD)
    # =========================================================================
    with tab_analitica:
        if not paciente_actual:
            st.info(
                "👆 Busque o cree un paciente por su **NHC** en la parte superior para gestionar sus analíticas."
            )
        else:
            st.subheader(
                "🧪 Extractor Automático de Analítica (Formato HGUV) en 2 Pasos"
            )
            st.caption(
                "**Paso 1:** Pegue el texto del informe de laboratorio del HGUV y pulse **Extraer Datos**. "
                "Podrá revisar y corregir cualquier cifra en pantalla antes de guardarla.  \n"
                "**Paso 2:** Una vez comprobado que todo es correcto, pulse **Confirmar y Grabar Analítica en Base de Datos**."
            )

            analiticas_previas: List[Dict[str, Any]] = st.session_state.get(
                "analiticas_paciente", []
            )
            ultima_analitica: Optional[Dict[str, Any]] = (
                analiticas_previas[0] if analiticas_previas else None
            )

            texto_analitica_input = st.text_area(
                "Pegue aquí el texto del informe de laboratorio (HGUV)",
                height=180,
                placeholder="Pegue aquí el texto completo del informe de laboratorio (Hematimetría, Bioquímica, Iones, Función Renal, Marcadores...)...",
                key="input_raw_analitica",
            )

            col_ext_btn, col_limp_btn = st.columns([3, 1])
            with col_ext_btn:
                extraer_clicked = st.button(
                    "🔍 Paso 1: Extraer Datos de la Analítica (Para Revisar antes de Grabar)",
                    type="primary",
                    use_container_width=True,
                )
            with col_limp_btn:
                if st.button(
                    "🧹 Limpiar borrador de analítica", use_container_width=True
                ):
                    st.session_state["borrador_analitica"] = None
                    st.session_state["texto_analitica_hc"] = None
                    st.rerun()

            if extraer_clicked:
                if not texto_analitica_input.strip():
                    st.warning(
                        "Pegue primero el texto de la analítica en el cuadro superior (o rellene manualmente tras pulsar)."
                    )
                datos_ext = parsear_analitica_hguv(texto_analitica_input)
                st.session_state["borrador_analitica"] = datos_ext
                st.session_state["borrador_analitica_id"] = (
                    st.session_state.get("borrador_analitica_id", 0) + 1
                )
                st.session_state["texto_analitica_hc"] = None

            borrador_actual = st.session_state.get("borrador_analitica")
            borrador_id = st.session_state.get("borrador_analitica_id", 1)

            if borrador_actual is not None:
                st.info(
                    "🔎 **Paso 1 completado (Borrador en revisión):** Revise los valores extraídos abajo. "
                    "**Todavía NO se ha grabado en la base de datos** hasta que pulse el botón del **Paso 2**."
                )

                fecha_ana_col, _ = st.columns([1, 3])
                with fecha_ana_col:
                    fecha_analitica_val = st.date_input(
                        "Fecha de la analítica",
                        value=borrador_actual.get("fecha_analitica", date.today()),
                        key=f"fecha_ana_input_{borrador_id}",
                    )

                valores_editados: Dict[str, Any] = {
                    "fecha_analitica": fecha_analitica_val.isoformat(),
                    "fge_texto": borrador_actual.get("fge_texto"),
                }

                cols_ana = st.columns(4)
                for idx, (clave, etiqueta, unidad) in enumerate(
                    PARAMETROS_ANALITICA_META
                ):
                    col = cols_ana[idx % 4]
                    val_detectado = borrador_actual.get(clave)
                    val_str_default = (
                        f"{val_detectado:g}" if val_detectado is not None else ""
                    )
                    with col:
                        label_input = (
                            f"{etiqueta} ({unidad})" if unidad else etiqueta
                        )
                        entrada_txt = st.text_input(
                            label_input,
                            value=val_str_default,
                            placeholder="No solicitado / vacío",
                            key=f"ana_field_{clave}_{borrador_id}",
                        )
                        if clave == "fge" and entrada_txt.strip().startswith(
                            (">", "<")
                        ):
                            valores_editados["fge_texto"] = entrada_txt.strip()
                        valores_editados[clave] = _to_float(entrada_txt)

                # Vista previa en vivo de la comparativa con la última analítica
                if ultima_analitica:
                    st.markdown(
                        f"##### 📊 Vista previa comparativa con la última analítica guardada ({str(ultima_analitica.get('fecha_analitica'))[:10]})"
                    )
                    filas_comp = []
                    for clave, etiqueta, unidad in PARAMETROS_ANALITICA_META:
                        v_act = valores_editados.get(clave)
                        v_prev = ultima_analitica.get(clave)
                        if v_act is None and v_prev is None:
                            continue
                        diff_str = "-"
                        if v_act is not None and v_prev is not None:
                            try:
                                d = round(float(v_act) - float(v_prev), 2)
                                flecha = "🔺" if d > 0 else ("🔻" if d < 0 else "＝")
                                signo = "+" if d > 0 else ""
                                diff_str = f"{flecha} {signo}{d:g}"
                            except (TypeError, ValueError):
                                diff_str = "-"
                        filas_comp.append(
                            {
                                "Parámetro": f"{etiqueta} ({unidad})"
                                if unidad
                                else etiqueta,
                                f"Actual ({fecha_analitica_val.isoformat()})": (
                                    valores_editados.get("fge_texto")
                                    if (
                                        clave == "fge"
                                        and valores_editados.get("fge_texto")
                                    )
                                    else (f"{v_act:g}" if v_act is not None else "—")
                                ),
                                f"Previa ({str(ultima_analitica.get('fecha_analitica'))[:10]})": (
                                    f"{float(v_prev):g}"
                                    if v_prev is not None
                                    else "—"
                                ),
                                "Diferencia (Δ)": diff_str,
                            }
                        )
                    if filas_comp:
                        st.dataframe(
                            pd.DataFrame(filas_comp),
                            use_container_width=True,
                            hide_index=True,
                        )

                st.write("")
                if st.button(
                    "💾 Paso 2: Confirmar y Grabar Analítica en Base de Datos y Generar Texto HC",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        payload_ana = {
                            "paciente_id": paciente_actual["id"],
                            "fecha_analitica": fecha_analitica_val.isoformat(),
                            "hb": valores_editados.get("hb"),
                            "hto": valores_editados.get("hto"),
                            "plaquetas": valores_editados.get("plaquetas"),
                            "probnp": valores_editados.get("probnp"),
                            "ca125": valores_editados.get("ca125"),
                            "cr": valores_editados.get("cr"),
                            "fge": valores_editados.get("fge"),
                            "fge_texto": valores_editados.get("fge_texto"),
                            "urea": valores_editados.get("urea"),
                            "k": valores_editados.get("k"),
                            "na": valores_editados.get("na"),
                            "gpt": valores_editados.get("gpt"),
                            "inr": valores_editados.get("inr"),
                            "pcr": valores_editados.get("pcr"),
                            "tpi": valores_editados.get("tpi"),
                            "texto_bruto": texto_analitica_input.strip() or None,
                        }

                        supabase.table("analiticas_uic").insert(payload_ana).execute()

                        st.session_state["texto_analitica_hc"] = (
                            generar_texto_comparativo_analitica(
                                paciente=paciente_actual,
                                analitica_actual=payload_ana,
                                analitica_previa=ultima_analitica,
                            )
                        )
                        st.session_state["analiticas_paciente"] = (
                            obtener_analiticas_paciente(
                                supabase, paciente_actual["id"]
                            )
                        )
                        # Si ya hay una visita guardada del paciente, actualizar también
                        # el texto general de la consulta para que incluya esta nueva analítica
                        visitas_act = st.session_state.get("visitas_paciente", [])
                        if visitas_act:
                            v0 = visitas_act[0]
                            st.session_state["texto_pre_consulta"] = (
                                generar_texto_consulta_enfermeria(
                                    paciente=paciente_actual,
                                    datos_visita=v0,
                                    talla_cm=talla_actual_cm,
                                    resumen_incidencias_str=v0.get(
                                        "detalle_incidencias"
                                    )
                                    or "Sin incidencias clínicas (cardiológicas ni no cardiológicas) desde la última visita.",
                                    analiticas_paciente=st.session_state[
                                        "analiticas_paciente"
                                    ],
                                )
                            )
                        st.session_state["borrador_analitica"] = None
                        st.success(
                            "✅ ¡Analítica confirmada, grabada en Supabase e incorporada al texto general de la consulta para la HC!"
                        )
                    except Exception as exc:
                        st.error(f"Error al guardar la analítica: {exc}")

            if st.session_state.get("texto_analitica_hc"):
                st.markdown("---")
                mostrar_cuadro_copia_hc(
                    titulo="Texto Plano de Analítica (con Comparativa) para Copiar a la HC",
                    texto_plano=st.session_state["texto_analitica_hc"],
                    key_prefix="analitica_hc",
                )

    # =========================================================================
    # PESTAÑA 3: TRATAMIENTO (HOJA DE ABUCASIS / GAIA)
    # =========================================================================
    with tab_tratamiento:
        if not paciente_actual:
            st.info(
                "👆 Busque o cree un paciente por su **NHC** en la parte superior para procesar su tratamiento."
            )
        else:
            st.subheader(
                "💊 Clasificador Automático de Tratamiento (Hoja de Abucasis / GAIA)"
            )
            st.caption(
                "Pegue el *copy-paste* de la hoja de tratamientos de **Abucasis (GAIA)**. "
                "El sistema reconoce tanto **nombres comerciales** (Entresto, Neparvis, Forxiga, Jardiance, Emconcor, Coropres, Aldactone, Inspra, Seguril, Sutril, Eliquis, Sintrom, Zarator, Crestor...) "
                "como **principios activos genéricos** y su posología, separándolos en **Tratamientos Cardiológicos** y **No Cardiológicos**."
            )

            texto_gaia_input = st.text_area(
                "Pegue aquí el texto copiado de la hoja de Abucasis (GAIA)",
                height=160,
                placeholder="Ejemplo:\nNEPARVIS 49 MG/51 MG COMPRIMIDOS RECUBIERTOS - 1 cada 12 horas\nEMCONCOR COR 2,5 MG - 1 cada 24 horas\nFORXIGA 10 MG - 1 cada 24 horas\nINSPRA 25 MG - 1 cada 24 horas\nSEGURIL 40 MG - 1 en desayuno\nOMEPRAZOL 20 MG - 1 cada 24 horas",
                key="input_raw_gaia",
            )

            if st.button(
                "🔍 Extraer y Clasificar Tratamientos de Abucasis (GAIA)",
                use_container_width=True,
            ):
                parsed_gaia = parsear_tratamiento_gaia(texto_gaia_input)
                st.session_state["trat_cardio_edit"] = parsed_gaia[
                    "texto_cardiologico"
                ]
                st.session_state["trat_nocardio_edit"] = parsed_gaia[
                    "texto_no_cardiologico"
                ]
                st.session_state["pilares_detectados"] = parsed_gaia["pilares"]

            # Semáforo de los 4 Pilares de IC (si hay texto o edición activa)
            texto_combinado_actual = (
                f"{st.session_state.get('trat_cardio_edit', '')}\n"
                f"{texto_gaia_input}"
            )
            pilares_actuales = parsear_tratamiento_gaia(texto_combinado_actual)[
                "pilares"
            ]

            st.markdown("##### 🛡️ Estado de los 4 Pilares Farmacológicos de Insuficiencia Cardíaca")
            cp1, cp2, cp3, cp4 = st.columns(4)
            with cp1:
                st.metric(
                    "1. ARNI / IECA / ARA-II",
                    "SÍ ✅" if pilares_actuales["ARNI / IECA / ARA-II"] else "NO ❌",
                )
            with cp2:
                st.metric(
                    "2. Betabloqueante",
                    "SÍ ✅" if pilares_actuales["Betabloqueante"] else "NO ❌",
                )
            with cp3:
                st.metric(
                    "3. ARM (Antag. Mineraloc.)",
                    "SÍ ✅" if pilares_actuales["ARM"] else "NO ❌",
                )
            with cp4:
                st.metric(
                    "4. iSGLT2",
                    "SÍ ✅" if pilares_actuales["iSGLT2"] else "NO ❌",
                )

            col_tcardio, col_tnocardio = st.columns(2)
            with col_tcardio:
                st.markdown("#### 🫀 Tratamientos Cardiológicos")
                trat_cardio_final = st.text_area(
                    "Tratamientos Cardiológicos",
                    key="trat_cardio_edit",
                    height=220,
                    label_visibility="collapsed",
                    placeholder="Fármacos cardiológicos y posología...",
                )
            with col_tnocardio:
                st.markdown("#### 💊 Tratamientos No Cardiológicos *(si los lleva)*")
                trat_nocardio_final = st.text_area(
                    "Tratamientos No Cardiológicos",
                    key="trat_nocardio_edit",
                    height=220,
                    label_visibility="collapsed",
                    placeholder="Si no lleva tratamientos no cardiológicos, déjelo vacío y no aparecerá en el informe...",
                )

            if st.button(
                "💾 Guardar Tratamiento y Generar Texto Plano para HC",
                type="primary",
                use_container_width=True,
            ):
                bloques_med_hc = []
                if trat_cardio_final.strip():
                    bloques_med_hc.append(
                        f"TRATAMIENTO CARDIOLÓGICO:\n{trat_cardio_final.strip()}"
                    )
                # Si no hay tratamiento no cardiológico, no aparece
                if trat_nocardio_final.strip():
                    bloques_med_hc.append(
                        f"TRATAMIENTO NO CARDIOLÓGICO:\n{trat_nocardio_final.strip()}"
                    )

                texto_med_unificado = "\n\n".join(bloques_med_hc)
                st.session_state["pre_medicacion_habitual"] = texto_med_unificado
                st.session_state["texto_tratamiento_hc"] = texto_med_unificado

                # Si el paciente ya tiene una última visita en Supabase, sincronizarla también
                if visitas_paciente:
                    try:
                        supabase.table("visitas_uic").update(
                            {
                                "medicacion_habitual": texto_med_unificado,
                                "tratamiento_cardiologico": trat_cardio_final.strip()
                                or None,
                                "tratamiento_no_cardiologico": trat_nocardio_final.strip()
                                or None,
                                "texto_gaia_bruto": texto_gaia_input.strip() or None,
                            }
                        ).eq("id", visitas_paciente[0]["id"]).execute()
                        st.session_state["visitas_paciente"] = obtener_visitas_paciente(
                            supabase, paciente_actual["id"]
                        )
                    except Exception:
                        pass

                st.success(
                    "✅ Tratamiento clasificado, sincronizado con la consulta actual y listo para copiar a la HC."
                )

            if st.session_state.get("texto_tratamiento_hc"):
                st.markdown("---")
                mostrar_cuadro_copia_hc(
                    titulo="Texto Plano del Tratamiento para Copiar a la Historia Clínica (HC)",
                    texto_plano=st.session_state["texto_tratamiento_hc"],
                    key_prefix="trat_hc",
                )

    # =========================================================================
    # PESTAÑA 4: EVOLUCIÓN E HISTÓRICO DEL PACIENTE
    # =========================================================================
    with tab_historico:
        if not paciente_actual:
            st.info(
                "👆 Busque o cree un paciente por su **NHC** en la parte superior para ver su evolución histórica."
            )
        else:
            st.subheader(
                f"📈 Evolución Clínica e Histórico — {obtener_nombre_completo_paciente(paciente_actual)}"
            )

            visitas_hist = st.session_state.get("visitas_paciente", [])
            analiticas_hist = st.session_state.get("analiticas_paciente", [])

            if not visitas_hist and not analiticas_hist:
                st.info("No hay visitas ni analíticas registradas todavía para este paciente.")
            else:
                if visitas_hist:
                    df_visitas = pd.DataFrame(visitas_hist)
                    df_visitas["fecha_dt"] = pd.to_datetime(
                        df_visitas["fecha_visita"], errors="coerce"
                    )
                    sort_cols = ["fecha_dt"] + (
                        ["created_at"] if "created_at" in df_visitas.columns else []
                    )
                    df_plot = df_visitas.sort_values(
                        by=sort_cols, ascending=True
                    ).copy()
                    df_plot["fecha_label"] = (
                        df_plot["fecha_dt"]
                        .dt.strftime("%Y-%m-%d")
                        .fillna(df_plot["fecha_visita"].astype(str))
                    )

                    fig = make_subplots(
                        rows=2,
                        cols=1,
                        shared_xaxes=True,
                        vertical_spacing=0.12,
                        subplot_titles=(
                            "Evolución del Peso Corporal (kg)",
                            "Evolución de la Tensión Arterial (TAS / TAD en mmHg) y FC (lpm)",
                        ),
                    )

                    fig.add_trace(
                        go.Scatter(
                            x=df_plot["fecha_label"],
                            y=df_plot["peso_kg"],
                            mode="lines+markers+text",
                            name="Peso Consulta (kg)",
                            text=[
                                f"{p:.1f}" if pd.notnull(p) else ""
                                for p in df_plot["peso_kg"]
                            ],
                            textposition="top center",
                            line=dict(color="#1f77b4", width=3),
                            marker=dict(size=9),
                        ),
                        row=1,
                        col=1,
                    )

                    fig.add_trace(
                        go.Scatter(
                            x=df_plot["fecha_label"],
                            y=df_plot["tas_consulta"],
                            mode="lines+markers",
                            name="TAS Consulta (mmHg)",
                            line=dict(color="#d62728", width=2.5),
                        ),
                        row=2,
                        col=1,
                    )
                    fig.add_trace(
                        go.Scatter(
                            x=df_plot["fecha_label"],
                            y=df_plot["tad_consulta"],
                            mode="lines+markers",
                            name="TAD Consulta (mmHg)",
                            line=dict(color="#ff7f0e", width=2.5, dash="dot"),
                        ),
                        row=2,
                        col=1,
                    )
                    if "fc_consulta" in df_plot.columns:
                        fig.add_trace(
                            go.Scatter(
                                x=df_plot["fecha_label"],
                                y=df_plot["fc_consulta"],
                                mode="lines+markers",
                                name="FC Consulta (lpm)",
                                line=dict(color="#2ca02c", width=2),
                            ),
                            row=2,
                            col=1,
                        )

                    fig.update_layout(
                        height=540,
                        hovermode="x unified",
                        legend=dict(
                            orientation="h",
                            yanchor="bottom",
                            y=1.04,
                            xanchor="right",
                            x=1,
                        ),
                    )
                    st.plotly_chart(fig, use_container_width=True)

                    st.markdown("#### 📋 Histórico de Visitas")
                    st.dataframe(df_visitas, use_container_width=True, hide_index=True)

                if analiticas_hist:
                    st.markdown("---")
                    st.markdown("#### 🧪 Histórico de Analíticas del Paciente")
                    df_ana = pd.DataFrame(analiticas_hist)
                    cols_ana_tabla = [
                        c
                        for c in [
                            "fecha_analitica",
                            "hb",
                            "hto",
                            "plaquetas",
                            "probnp",
                            "ca125",
                            "cr",
                            "fge_texto",
                            "fge",
                            "urea",
                            "k",
                            "na",
                            "gpt",
                            "inr",
                            "pcr",
                            "tpi",
                        ]
                        if c in df_ana.columns
                    ]
                    st.dataframe(
                        df_ana[cols_ana_tabla],
                        use_container_width=True,
                        hide_index=True,
                    )

    # =========================================================================
    # PESTAÑA 5: CUADRO DE MANDOS / DASHBOARD GLOBAL DE LA UNIDAD DE IC
    # =========================================================================
    with tab_dashboard:
        st.subheader("📊 Cuadro de Mandos Integral — Unidad de Insuficiencia Cardíaca")

        try:
            all_pac = supabase.table("pacientes").select("*").execute().data or []
            all_vis = (
                supabase.table("visitas_uic")
                .select("*")
                .order("fecha_visita", desc=True)
                .execute()
                .data
                or []
            )
            all_ana = (
                supabase.table("analiticas_uic")
                .select("*")
                .order("fecha_analitica", desc=True)
                .execute()
                .data
                or []
            )
        except Exception as exc:
            st.error(f"Error al cargar datos del Cuadro de Mandos: {exc}")
            all_pac, all_vis, all_ana = [], [], []

        if not all_pac and not all_vis:
            st.info(
                "Todavía no hay pacientes ni visitas registradas en la base de datos para mostrar estadísticas."
            )
        else:
            df_p = pd.DataFrame(all_pac) if all_pac else pd.DataFrame()
            df_v = pd.DataFrame(all_vis) if all_vis else pd.DataFrame()

            # 1. KPIs Superiores
            k1, k2, k3, k4, k5 = st.columns(5)
            total_pacientes = len(df_p)
            total_visitas = len(df_v)
            edad_media = (
                f"{df_p['edad'].dropna().mean():.1f} años"
                if (not df_p.empty and "edad" in df_p.columns and not df_p["edad"].dropna().empty)
                else "—"
            )
            total_ferinject = (
                int(df_v["ferinject_iv"].fillna(False).astype(bool).sum())
                if (not df_v.empty and "ferinject_iv" in df_v.columns)
                else 0
            )
            total_diuretico_iv = (
                int(
                    (
                        df_v.get("furosemida_iv_bolo", pd.Series(dtype=bool)).fillna(False).astype(bool)
                        | df_v.get("perfusor_furosemida", pd.Series(dtype=bool)).fillna(False).astype(bool)
                        | df_v.get("derivacion_hd_diureticos_iv", pd.Series(dtype=bool)).fillna(False).astype(bool)
                    ).sum()
                )
                if not df_v.empty
                else 0
            )

            k1.metric("👥 Pacientes en Seguimiento", total_pacientes)
            k2.metric("🩺 Consultas Realizadas", total_visitas)
            k3.metric("🎂 Edad Media Cohorte", edad_media)
            k4.metric("💉 Tratamientos Ferinject IV", total_ferinject)
            k5.metric("💧 Actuaciones Diurético IV / Infusor", total_diuretico_iv)

            st.markdown("---")

            if not df_v.empty:
                col_g1, col_g2 = st.columns(2)

                # Gráfico 1: Actividad y Técnicas de Enfermería / Hospital de Día
                with col_g1:
                    conteo_tecnicas = {
                        "ECG realizado": int(
                            df_v.get("ecg_realizado", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Analítica extraída": int(
                            df_v.get("analitica_realizada", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Ferinject IV (500/1000/1500)": int(
                            df_v.get("ferinject_iv", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Furosemida IV en bolo": int(
                            df_v.get("furosemida_iv_bolo", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Infusor Furosemida": int(
                            df_v.get("perfusor_furosemida", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "HD para Diuréticos IV": int(
                            df_v.get(
                                "derivacion_hd_diureticos_iv", pd.Series(dtype=bool)
                            )
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "HD para Levosimendán": int(
                            df_v.get("derivacion_hd_levosimendan", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                    }
                    fig_tec = go.Figure(
                        go.Bar(
                            x=list(conteo_tecnicas.values()),
                            y=list(conteo_tecnicas.keys()),
                            orientation="h",
                            text=list(conteo_tecnicas.values()),
                            textposition="auto",
                            marker_color="#1f77b4",
                        )
                    )
                    fig_tec.update_layout(
                        title="Actividad y Técnicas de Enfermería / Hospital de Día",
                        xaxis_title="Nº de procedimientos",
                        height=380,
                        margin=dict(l=20, r=20, t=50, b=30),
                    )
                    st.plotly_chart(fig_tec, use_container_width=True)

                # Gráfico 2: Desglose de Incidencias Cardiológicas y No Cardiológicas
                with col_g2:
                    conteo_inc = {
                        "Ingreso por IC (Cardio)": int(
                            df_v.get("inc_cardio_ingreso_ic", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Urgencias por IC (Cardio)": int(
                            df_v.get("inc_cardio_urgencias_ic", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Uso Diuréticos IV (Cardio)": int(
                            df_v.get("inc_cardio_diureticos_iv", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Aumento Diuréticos VO (Cardio)": int(
                            df_v.get(
                                "inc_cardio_aumento_diureticos_vo",
                                pd.Series(dtype=bool),
                            )
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Ingreso Otras Cardio": int(
                            df_v.get("inc_cardio_ingreso_otras", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Muerte": int(
                            df_v.get("inc_cardio_muerte", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Ingreso No Cardiológico": int(
                            df_v.get("inc_nocardio_ingreso", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                        "Urgencias No Cardiológicas": int(
                            df_v.get("inc_nocardio_urgencias", pd.Series(dtype=bool))
                            .fillna(False)
                            .astype(bool)
                            .sum()
                        ),
                    }
                    colores_inc = [
                        "#d62728",
                        "#e377c2",
                        "#ff7f0e",
                        "#ffbb78",
                        "#9467bd",
                        "#333333",
                        "#2ca02c",
                        "#98df8a",
                    ]
                    fig_inc = go.Figure(
                        go.Bar(
                            x=list(conteo_inc.values()),
                            y=list(conteo_inc.keys()),
                            orientation="h",
                            text=list(conteo_inc.values()),
                            textposition="auto",
                            marker_color=colores_inc,
                        )
                    )
                    fig_inc.update_layout(
                        title="Incidencias Registradas (Cardiológicas vs. No Cardiológicas)",
                        xaxis_title="Nº de eventos",
                        height=380,
                        margin=dict(l=20, r=20, t=50, b=30),
                    )
                    st.plotly_chart(fig_inc, use_container_width=True)

                col_g3, col_g4 = st.columns(2)
                # Gráfico 3: Distribución de Clase Funcional NYHA (última visita de cada paciente)
                with col_g3:
                    df_ult_vis = df_v.drop_duplicates(
                        subset=["paciente_id"], keep="first"
                    )
                    nyha_counts = (
                        df_ult_vis["clase_funcional_nyha"]
                        .fillna("No esp.")
                        .value_counts()
                        .reindex(["I", "II", "III", "IV"], fill_value=0)
                    )
                    fig_nyha = go.Figure(
                        go.Pie(
                            labels=[f"NYHA {k}" for k in nyha_counts.index],
                            values=nyha_counts.values,
                            hole=0.45,
                        )
                    )
                    fig_nyha.update_layout(
                        title="Situación Funcional Actual de la Cohorte (NYHA)",
                        height=340,
                    )
                    st.plotly_chart(fig_nyha, use_container_width=True)

                # Gráfico 4: Optimización de los 4 Pilares de IC en la cohorte
                with col_g4:
                    pilares_totales = {
                        "ARNI / IECA / ARA-II": 0,
                        "Betabloqueante": 0,
                        "ARM": 0,
                        "iSGLT2": 0,
                    }
                    n_pac_con_visita = len(df_ult_vis)
                    for _, fila_v in df_ult_vis.iterrows():
                        med_txt = str(fila_v.get("medicacion_habitual") or "")
                        p_det = parsear_tratamiento_gaia(med_txt)["pilares"]
                        for k_p in pilares_totales:
                            if p_det.get(k_p):
                                pilares_totales[k_p] += 1

                    porcentajes_pilares = [
                        round((v / n_pac_con_visita) * 100, 1)
                        if n_pac_con_visita > 0
                        else 0.0
                        for v in pilares_totales.values()
                    ]
                    fig_pil = go.Figure(
                        go.Bar(
                            x=list(pilares_totales.keys()),
                            y=porcentajes_pilares,
                            text=[f"{p}%" for p in porcentajes_pilares],
                            textposition="auto",
                            marker_color=["#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e"],
                        )
                    )
                    fig_pil.update_layout(
                        title="% Pacientes con los 4 Pilares de IC Prescritos",
                        yaxis=dict(title="% de Pacientes", range=[0, 105]),
                        height=340,
                    )
                    st.plotly_chart(fig_pil, use_container_width=True)

                # Tabla de Alertas Clínicas Activas (Ganancia ponderal >= 2 kg o NYHA III-IV)
                st.markdown("#### 🚨 Radar de Alertas Clínicas (Última Visita por Paciente)")
                mapa_pac = {p["id"]: p for p in all_pac}
                alertas_lista = []
                for _, fv in df_ult_vis.iterrows():
                    p_info = mapa_pac.get(fv.get("paciente_id"), {})
                    dif_p = fv.get("diferencia_peso_kg")
                    nyha_v = fv.get("clase_funcional_nyha")
                    inc_v = fv.get("incidencias_bool")
                    motivos = []
                    if dif_p is not None and float(dif_p) >= 2.0:
                        motivos.append(f"Ganancia ponderal +{float(dif_p):.1f} kg")
                    if nyha_v in ("III", "IV"):
                        motivos.append(f"NYHA {nyha_v}")
                    if inc_v:
                        motivos.append("Incidencias recientes")

                    if motivos:
                        alertas_lista.append(
                            {
                                "NHC": p_info.get("nhc", "-"),
                                "Paciente": obtener_nombre_completo_paciente(p_info),
                                "Fecha Última Visita": str(fv.get("fecha_visita"))[:10],
                                "Alertas Detectadas": " | ".join(motivos),
                                "Peso (kg)": fv.get("peso_kg"),
                                "TA (mmHg)": f"{fv.get('tas_consulta', '-')}/{fv.get('tad_consulta', '-')}",
                            }
                        )
                if alertas_lista:
                    st.dataframe(
                        pd.DataFrame(alertas_lista),
                        use_container_width=True,
                        hide_index=True,
                    )
                else:
                    st.success(
                        "No hay alertas activas (ganancia >= 2 kg, NYHA III-IV o incidencias) en las últimas visitas."
                    )


if __name__ == "__main__":
    main()
