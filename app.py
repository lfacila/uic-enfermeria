import base64
from datetime import date, datetime
from io import BytesIO
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
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

    def delete(self) -> "_RESTQueryBuilder":
        self._method = "DELETE"
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
    ("vcm", "VCM", "fL"),
    ("plaquetas", "Plaquetas", "10*9/L"),
    ("probnp", "NT-proBNP / ProBNP", "pg/mL"),
    ("ca125", "CA 125", "U/mL"),
    ("cr", "Creatinina", "mg/dL"),
    ("fge", "FGE (CKD-EPI)", "mL/min/1,73m²"),
    ("urea", "Urea", "mg/dL"),
    ("k", "Potasio (K)", "mEq/L"),
    ("na", "Sodio (Na)", "mEq/L"),
    ("acido_urico", "Ácido Úrico", "mg/dL"),
    ("colesterol_total", "Colesterol Total", "mg/dL"),
    ("ldl", "LDL", "mg/dL"),
    ("hdl", "HDL", "mg/dL"),
    ("trigliceridos", "Triglicéridos (TG)", "mg/dL"),
    ("lpa", "Lipoproteína (a)", "mg/dL"),
    ("cac_orina", "Albúmina/Creatinina orina", "mg/g"),
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
    Extrae automáticamente Hb, Hto, VCM, Plaquetas, ProBNP, CA 125, Cr, FGE, Urea,
    K, Na, Ácido Úrico, Colesterol Total, LDL, HDL, TG, Lipoproteína (a),
    Albúmina/Creatinina en orina, GPT, INR, PCR y TpI (si existen) a partir del
    texto copiado del informe de laboratorio del HGUV.
    """
    resultado: Dict[str, Any] = {
        "fecha_analitica": date.today(),
        "hb": None,
        "hto": None,
        "vcm": None,
        "plaquetas": None,
        "probnp": None,
        "ca125": None,
        "cr": None,
        "fge": None,
        "fge_texto": None,
        "urea": None,
        "k": None,
        "na": None,
        "acido_urico": None,
        "colesterol_total": None,
        "ldl": None,
        "hdl": None,
        "trigliceridos": None,
        "lpa": None,
        "cac_orina": None,
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
        "vcm": r"(?:Volumen\s+Corpuscular\s+Medio|(?<!\w)VCM(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "plaquetas": r"(?:Plaquetas)\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "probnp": r"(?:NT-?proBNP|ProBNP|P[eé]ptido\s+natriur[eé]tico(?:\s+tipo\s+B)?)\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?(?:[<>]=?\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "ca125": r"(?:Ant[ií]geno\s+carbohidrato\s+125|CA[\s-]*125)\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?(?:[<>]=?\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "cr": r"(?<!/)(?<!/\s)(?:Ser\s*-\s*)?(?:Creatinina(?!(?:\s+en)?\s+orina)|(?<!\w)Cr(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "urea": r"(?:Ser\s*-\s*)?(?:Urea)\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "k": r"(?:Ser\s*-\s*)?(?:Potasio|(?<!\w)K(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "na": r"(?:Ser\s*-\s*)?(?:Sodio|(?<!\w)Na(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "acido_urico": r"(?:Ser\s*-\s*)?(?:[AÁ]cido\s+[UÚ]rico|Urato)\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "colesterol_total": r"(?:Ser\s*-\s*)?Colesterol(?:\s+total)?(?!\s*(?:de\s+)?(?:HDL|LDL|no|VLDL|esterificado))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "ldl": r"(?:Colesterol\s+(?:de\s+)?LDL|LDL[\s-]*Colesterol|(?<!\w)c-?LDL(?!\w)|(?<!\w)LDL(?!\w))\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "hdl": r"(?<!no\s)(?<!no-)(?:Colesterol\s+(?:de\s+)?HDL|HDL[\s-]*Colesterol|(?<!\w)c-?HDL(?!\w)|(?<!\w)HDL(?!\w))\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "trigliceridos": r"(?:Ser\s*-\s*)?(?:Triglic[eé]ridos|(?<!\w)TG(?!\w))\s*(?:\*+\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "lpa": r"(?:Lipoprote[ií]na\s*\(?a\)?|(?<!\w)Lp\s*\(a\)|(?<!\w)Lpa(?!\w))\s*(?:\*+\s*)?(?:[<>]=?\s*)?([0-9]+(?:[.,][0-9]+)?)",
        "cac_orina": r"(?:(?:Cociente|Relaci[oó]n|[IÍ]ndice)\s+(?:Alb[uú]mina|Microalbuminuria)\s*/\s*Creatinina|Alb[uú]mina\s*/\s*Creatin(?:ina|uria)|Microalbuminuria\s*/\s*Creatinina|(?<!\w)CAC(?:\s+orina)?(?!\w))\s*(?:(?:\([^)]*\))?\s*)?(?:\*+\s*)?(?:[<>]=?\s*)?([0-9]+(?:[.,][0-9]+)?)",
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


def _normalizar_talla_cm(talla_cm: Optional[float]) -> Optional[float]:
    """Convierte 0 o None en None (pendiente de tallar), o redondea talla válida."""
    if talla_cm is None or float(talla_cm) <= 0:
        return None
    val = float(talla_cm)
    return int(round(val)) if val.is_integer() else round(val, 1)


def recalcular_imc_visitas_paciente(
    supabase: Any, paciente_id: Any, talla_cm: Optional[float]
) -> None:
    """
    Recalcula y actualiza el IMC de todas las visitas previas del paciente en Supabase
    cuando se introduce o corrige su talla real.
    """
    talla_norm = _normalizar_talla_cm(talla_cm)
    visitas = obtener_visitas_paciente(supabase, paciente_id)
    if not visitas:
        return
    altura_m = (float(talla_norm) / 100.0) if talla_norm and talla_norm > 0 else None
    for v in visitas:
        peso = v.get("peso_kg")
        if peso is not None:
            nuevo_imc = (
                round(float(peso) / (altura_m**2), 2) if altura_m is not None else None
            )
            if v.get("imc") != nuevo_imc:
                supabase.table("visitas_uic").update({"imc": nuevo_imc}).eq(
                    "id", v["id"]
                ).execute()


def crear_paciente(
    supabase: Any,
    nhc: str,
    nombre: str,
    apellidos: str,
    edad: int,
    sexo: str,
    talla_cm: Optional[float],
) -> Dict[str, Any]:
    """Crea un nuevo paciente en la tabla 'pacientes' con Nombre, Apellidos, Edad, Sexo y Talla (opcional)."""
    talla_val = _normalizar_talla_cm(talla_cm)
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
    nueva_talla_cm: Optional[float],
    nhc: Optional[str] = None,
    recalcular_imc: bool = True,
) -> Dict[str, Any]:
    """Actualiza los datos identificativos, demográficos y talla del paciente (y recalcula IMC si procede)."""
    talla_val = _normalizar_talla_cm(nueva_talla_cm)
    nombre_limpio = nombre.strip()
    apellidos_limpios = apellidos.strip()
    nombre_completo = (
        f"{apellidos_limpios}, {nombre_limpio}"
        if (apellidos_limpios and nombre_limpio)
        else f"{nombre_limpio} {apellidos_limpios}".strip()
    )
    payload: Dict[str, Any] = {
        "nombre": nombre_limpio,
        "apellidos": apellidos_limpios,
        "iniciales_o_nombre": nombre_completo,
        "edad": int(edad),
        "sexo": sexo,
        "talla_cm": talla_val,
    }
    if nhc is not None and nhc.strip():
        payload["nhc"] = nhc.strip()

    res = (
        supabase.table("pacientes")
        .update(payload)
        .eq("id", paciente_id)
        .execute()
    )
    if recalcular_imc:
        recalcular_imc_visitas_paciente(supabase, paciente_id, talla_val)
    return res.data[0] if res.data else payload


def eliminar_paciente(supabase: Any, paciente_id: Any) -> None:
    """Elimina un paciente de Supabase (y en cascada sus visitas y analíticas)."""
    supabase.table("pacientes").delete().eq("id", paciente_id).execute()


def eliminar_visita(supabase: Any, visita_id: Any) -> None:
    """Elimina una visita concreta de 'visitas_uic' sin borrar al paciente."""
    supabase.table("visitas_uic").delete().eq("id", visita_id).execute()


def eliminar_analitica(supabase: Any, analitica_id: Any) -> None:
    """Elimina una analítica concreta de 'analiticas_uic' sin borrar al paciente."""
    supabase.table("analiticas_uic").delete().eq("id", analitica_id).execute()


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
    talla_p = paciente.get("talla_cm")
    st.session_state["talla_editable_cm"] = float(talla_p) if (talla_p and float(talla_p) > 0) else 0.0
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
    talla_cm: Optional[float],
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

    # 5. Entorno Social, Soporte UHD, Ejercicio y Hábitos
    resumen_social = (datos_visita.get("resumen_social_habitos") or "").strip()

    # 6. Adherencia y tratamiento
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

    talla_str = (
        f"{float(talla_cm):g} cm"
        if (talla_cm is not None and float(talla_cm) > 0)
        else "Pendiente de tallar"
    )
    imc_str = (
        f"{float(imc):.2f} kg/m2"
        if (imc is not None)
        else "Pendiente (sin talla)"
    )

    lineas = [
        f"CONSULTA DE ENFERMERÍA - UNIDAD DE INSUFICIENCIA CARDÍACA ({fecha_str})",
        f"Paciente: {nombre_completo}{demog_str} | NHC: {nhc}",
        "-" * 72,
        f"- Incidencias desde última visita: {resumen_incidencias_str}",
        f"- Situación Funcional: Clase funcional NYHA {datos_visita.get('clase_funcional_nyha', '-')}",
        f"- Control Domiciliario: {texto_dom}",
        f"- Antropometría en Consulta: Peso actual {peso:.1f} kg ({dif_str}) | Talla: {talla_str} | IMC: {imc_str}",
        f"- Constantes en Consulta: TA {tas_con}/{tad_con} mmHg | FC {fc_con} lpm",
        f"- Actuaciones / Técnicas de Enfermería: {texto_actuaciones}",
    ]
    if linea_analitica:
        lineas.append(linea_analitica)
    if resumen_social:
        lineas.append(f"- Entorno Social, UHD y Estilo de Vida: {resumen_social}")

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
    titulo: str, texto_plano: str, key_prefix: str, height: int = 280
) -> None:
    """
    Muestra tanto un cuadro de texto plano editable (st.text_area) como un bloque
    con botón de copia rápida en 1 clic (st.code) para pegar en la Historia Clínica.
    Sincroniza automáticamente el contenido del text_area cada vez que cambian los
    checkboxes de enfermería o se añade una analítica.
    """
    clave_textarea = f"{key_prefix}_textarea"
    clave_source = f"{key_prefix}_last_source"

    if st.session_state.get(clave_source) != texto_plano:
        st.session_state[clave_textarea] = texto_plano
        st.session_state[clave_source] = texto_plano

    st.markdown(f"#### 📋 {titulo}")
    col_txt, col_code = st.columns(2)
    with col_txt:
        st.caption(
            "Cuadro de texto plano editable (puede seleccionarlo todo con Ctrl+A / Cmd+A y copiar):"
        )
        texto_editado = st.text_area(
            "Texto plano para Historia Clínica (HC)",
            height=height,
            key=clave_textarea,
            label_visibility="collapsed",
        )
    with col_code:
        st.caption(
            "Copia rápida en 1 clic (pulse el icono de copiar en la esquina superior derecha):"
        )
        st.code(texto_editado, language="text")


# -----------------------------------------------------------------------------
# LOGO FLOTANTE EN ESQUINA ("ÁNGEL - UIC EDITION") CON AMPLIACIÓN AL PULSAR
# -----------------------------------------------------------------------------
@st.cache_data
def _cargar_angel_logo_b64() -> Optional[str]:
    ruta_logo = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "angel_logo.jpg"
    )
    if os.path.exists(ruta_logo):
        try:
            with open(ruta_logo, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            return None
    return None


def render_angel_corner_logo() -> None:
    """
    Muestra el logo de Ángel ('GTA VI - UIC Edition') fijo en la esquina inferior
    derecha de la pantalla. Al pulsarlo se amplía a tamaño completo en un modal.
    """
    b64_img = _cargar_angel_logo_b64()
    if not b64_img:
        return

    html_logo = f"""
    <style>
    #angel-modal-toggle {{
        display: none;
    }}
    .angel-corner-badge {{
        position: fixed;
        bottom: 20px;
        right: 20px;
        z-index: 999990;
        cursor: pointer;
        background: linear-gradient(135deg, #ff6ec4 0%, #7873f5 100%);
        padding: 4px;
        border-radius: 16px;
        box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
        display: flex;
        align-items: center;
        gap: 8px;
        padding-right: 12px;
        user-select: none;
    }}
    .angel-corner-badge:hover {{
        transform: scale(1.07);
        box-shadow: 0 8px 26px rgba(255, 110, 196, 0.6);
    }}
    .angel-corner-badge img {{
        width: 58px;
        height: 72px;
        object-fit: cover;
        border-radius: 12px;
        border: 2px solid #ffffff;
        display: block;
    }}
    .angel-corner-text {{
        color: #ffffff;
        font-family: sans-serif;
        font-size: 12px;
        font-weight: 700;
        line-height: 1.25;
        text-shadow: 0 1px 3px rgba(0, 0, 0, 0.5);
    }}
    .angel-corner-sub {{
        display: block;
        font-size: 10px;
        font-weight: 400;
        opacity: 0.92;
    }}
    .angel-lightbox-overlay {{
        display: none;
        position: fixed;
        inset: 0;
        z-index: 999999;
        background: rgba(0, 0, 0, 0.85);
        backdrop-filter: blur(5px);
        align-items: center;
        justify-content: center;
        flex-direction: column;
        padding: 20px;
        cursor: zoom-out;
    }}
    #angel-modal-toggle:checked ~ .angel-lightbox-overlay {{
        display: flex;
    }}
    .angel-lightbox-overlay img {{
        max-height: 86vh;
        max-width: 92vw;
        border-radius: 16px;
        border: 3px solid #ff6ec4;
        box-shadow: 0 15px 50px rgba(0, 0, 0, 0.8);
    }}
    .angel-lightbox-close {{
        margin-top: 12px;
        background: #ffffff;
        color: #111111;
        padding: 6px 18px;
        border-radius: 999px;
        font-family: sans-serif;
        font-size: 13px;
        font-weight: 700;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    }}
    </style>

    <input type="checkbox" id="angel-modal-toggle" />

    <label for="angel-modal-toggle" class="angel-corner-badge" title="Pulsar para ampliar logo UIC Edition">
        <img src="data:image/jpeg;base64,{b64_img}" alt="Ángel UIC Edition" />
        <div class="angel-corner-text">
            🕶️ Ángel UIC<br/>
            <span class="angel-corner-sub">🔍 Pulsar para ampliar</span>
        </div>
    </label>

    <label for="angel-modal-toggle" class="angel-lightbox-overlay">
        <img src="data:image/jpeg;base64,{b64_img}" alt="Ángel GTA VI UIC Edition Ampliado" />
        <div class="angel-lightbox-close">✕ Pulsar en cualquier lugar para cerrar</div>
    </label>
    """
    st.markdown(html_logo, unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# EXPORTACIÓN A EXCEL (.XLSX) DE TODOS LOS DATOS
# -----------------------------------------------------------------------------
_EXCEL_ILLEGAL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _limpiar_df_para_excel(df: pd.DataFrame) -> pd.DataFrame:
    """Elimina zonas horarias y caracteres de control ilegales para compatibilidad total con openpyxl."""
    if df.empty:
        return df
    df_out = df.copy()
    for col in df_out.columns:
        if pd.api.types.is_datetime64_any_dtype(df_out[col]):
            try:
                df_out[col] = df_out[col].dt.tz_localize(None)
            except Exception:
                df_out[col] = df_out[col].astype(str)
        elif df_out[col].dtype == object:
            def _clean_cell(val: Any) -> Any:
                if isinstance(val, list):
                    val = ", ".join(map(str, val))
                if isinstance(val, str):
                    return _EXCEL_ILLEGAL_CHARS_RE.sub("", val)
                return val

            df_out[col] = df_out[col].apply(_clean_cell)
    return df_out


def _estilizar_hoja_excel(ws: Any) -> None:
    """Aplica estilo de cabecera, filtros, inmovilizado superior y ancho automático."""
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=10)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    if ws.max_row >= 1 and ws.max_column >= 1:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_align

    for col_idx in range(1, ws.max_column + 1):
        col_letter = get_column_letter(col_idx)
        max_len = 10
        for row_idx in range(1, min(ws.max_row + 1, 250)):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val is not None:
                lineas_val = str(val).splitlines()
                val_str = lineas_val[0] if lineas_val else ""
                if len(val_str) > max_len:
                    max_len = len(val_str)
        ws.column_dimensions[col_letter].width = min(max_len + 4, 42)


def _enriquecer_con_paciente(
    df_datos: pd.DataFrame, df_pac: pd.DataFrame
) -> pd.DataFrame:
    """Añade NHC, Nombre, Apellidos, Edad, Sexo y Talla al inicio de un DataFrame."""
    if df_datos.empty or df_pac.empty or "paciente_id" not in df_datos.columns:
        return df_datos
    cols_pac = [
        c
        for c in ["id", "nhc", "nombre", "apellidos", "iniciales_o_nombre", "edad", "sexo", "talla_cm"]
        if c in df_pac.columns
    ]
    df_p_sub = df_pac[cols_pac].rename(columns={"id": "paciente_id"})
    df_merged = df_datos.merge(df_p_sub, on="paciente_id", how="left")

    cols_ident = [
        c
        for c in ["nhc", "apellidos", "nombre", "iniciales_o_nombre", "edad", "sexo", "talla_cm"]
        if c in df_merged.columns
    ]
    otras_cols = [c for c in df_merged.columns if c not in cols_ident]
    return df_merged[cols_ident + otras_cols]


def generar_excel_completo(
    all_pac: List[Dict[str, Any]],
    all_vis: List[Dict[str, Any]],
    all_ana: List[Dict[str, Any]],
) -> bytes:
    """
    Genera un archivo Excel (.xlsx) en memoria con 4 hojas estructuradas:
    1. Visitas_Completas (visitas + datos demográficos del paciente)
    2. Analiticas (resultados analíticos + datos del paciente)
    3. Resumen_Por_Paciente (1 fila por paciente con su última visita y última analítica)
    4. Pacientes (maestro de pacientes)
    """
    df_p = pd.DataFrame(all_pac) if all_pac else pd.DataFrame(columns=["id", "nhc", "nombre", "apellidos", "edad", "sexo", "talla_cm"])
    df_v = pd.DataFrame(all_vis) if all_vis else pd.DataFrame(columns=["id", "paciente_id", "fecha_visita"])
    df_a = pd.DataFrame(all_ana) if all_ana else pd.DataFrame(columns=["id", "paciente_id", "fecha_analitica"])

    # Ordenar fechas de forma descendente si existen
    if not df_v.empty and "fecha_visita" in df_v.columns:
        sort_v = [c for c in ["fecha_visita", "created_at"] if c in df_v.columns]
        df_v = df_v.sort_values(by=sort_v, ascending=False)
    if not df_a.empty and "fecha_analitica" in df_a.columns:
        sort_a = [c for c in ["fecha_analitica", "created_at"] if c in df_a.columns]
        df_a = df_a.sort_values(by=sort_a, ascending=False)

    # Hoja 1: Visitas enriquecidas
    df_vis_enr = _enriquecer_con_paciente(df_v, df_p)

    # Hoja 2: Analíticas enriquecidas (poniendo texto_bruto al final si existe)
    df_ana_enr = _enriquecer_con_paciente(df_a, df_p)
    if "texto_bruto" in df_ana_enr.columns:
        cols_sin_bruto = [c for c in df_ana_enr.columns if c != "texto_bruto"]
        df_ana_enr = df_ana_enr[cols_sin_bruto + ["texto_bruto"]]

    # Hoja 3: Resumen 1 fila por paciente (datos paciente + última visita + última analítica)
    df_resumen = df_p.copy()
    if not df_resumen.empty and "id" in df_resumen.columns:
        if not df_v.empty and "paciente_id" in df_v.columns:
            df_v_ult = df_v.drop_duplicates(subset=["paciente_id"], keep="first").copy()
            df_v_ult = df_v_ult.drop(columns=[c for c in ["id", "created_at"] if c in df_v_ult.columns])
            df_resumen = df_resumen.merge(
                df_v_ult, left_on="id", right_on="paciente_id", how="left", suffixes=("", "_visita")
            )
            if "paciente_id" in df_resumen.columns:
                df_resumen = df_resumen.drop(columns=["paciente_id"])

        if not df_a.empty and "paciente_id" in df_a.columns:
            df_a_ult = df_a.drop_duplicates(subset=["paciente_id"], keep="first").copy()
            cols_drop_a = [c for c in ["id", "created_at", "texto_bruto"] if c in df_a_ult.columns]
            df_a_ult = df_a_ult.drop(columns=cols_drop_a)
            df_a_ult = df_a_ult.rename(
                columns={
                    c: f"ult_ana_{c}"
                    for c in df_a_ult.columns
                    if c != "paciente_id"
                }
            )
            df_resumen = df_resumen.merge(
                df_a_ult, left_on="id", right_on="paciente_id", how="left"
            )
            if "paciente_id" in df_resumen.columns:
                df_resumen = df_resumen.drop(columns=["paciente_id"])

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        hojas = [
            ("Visitas_Completas", _limpiar_df_para_excel(df_vis_enr)),
            ("Analiticas", _limpiar_df_para_excel(df_ana_enr)),
            ("Resumen_Por_Paciente", _limpiar_df_para_excel(df_resumen)),
            ("Pacientes", _limpiar_df_para_excel(df_p)),
        ]
        for nombre_hoja, df_hoja in hojas:
            df_hoja.to_excel(writer, sheet_name=nombre_hoja, index=False)
            _estilizar_hoja_excel(writer.sheets[nombre_hoja])

    buffer.seek(0)
    return buffer.getvalue()


def obtener_todos_los_datos_uic(
    supabase: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Descarga todos los registros de pacientes, visitas y analíticas desde Supabase."""
    all_pac = supabase.table("pacientes").select("*").execute().data or []
    all_vis = (
        supabase.table("visitas_uic")
        .select("*")
        .order("fecha_visita", desc=True)
        .execute()
        .data
        or []
    )
    try:
        all_ana = (
            supabase.table("analiticas_uic")
            .select("*")
            .order("fecha_analitica", desc=True)
            .execute()
            .data
            or []
        )
    except Exception:
        all_ana = []
    return all_pac, all_vis, all_ana


# -----------------------------------------------------------------------------
# INTERFAZ PRINCIPAL
# -----------------------------------------------------------------------------
def main() -> None:
    render_angel_corner_logo()

    if not check_password():
        return

    # Inicializar cliente Supabase
    try:
        supabase = get_supabase_client()
    except Exception as exc:
        st.error(
            f"Error al inicializar el cliente de Supabase. Verifique `.streamlit/secrets.toml`: {exc}"
        )
        return

    # Cabecera superior con exportación rápida a Excel y botón de cierre de sesión
    col_title, col_export_top, col_logout = st.columns([5.2, 1.5, 1.1])
    with col_title:
        st.title("🫀 Consulta de Enfermería — Unidad de Insuficiencia Cardíaca (HGUV)")
    with col_export_top:
        st.write("")
        if st.button("📥 Preparar Excel", use_container_width=True, help="Prepara el Excel completo con todos los datos guardados hasta ahora"):
            try:
                p_all, v_all, a_all = obtener_todos_los_datos_uic(supabase)
                st.session_state["excel_global_bytes"] = generar_excel_completo(
                    p_all, v_all, a_all
                )
                st.session_state["excel_global_nombre"] = (
                    f"UIC_HGUV_Base_Completa_{date.today().isoformat()}.xlsx"
                )
            except Exception as exc:
                st.error(f"Error al generar Excel: {exc}")
    with col_logout:
        st.write("")
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    if st.session_state.get("excel_global_bytes"):
        st.download_button(
            label="⬇️ Descargar ahora el Excel completo (.xlsx)",
            data=st.session_state["excel_global_bytes"],
            file_name=st.session_state.get(
                "excel_global_nombre",
                f"UIC_HGUV_Base_Completa_{date.today().isoformat()}.xlsx",
            ),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True,
            key="dl_excel_top",
        )

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
                    "Talla (cm) — 0 si aún no la sabe",
                    min_value=0.0,
                    max_value=230.0,
                    value=0.0,
                    step=1.0,
                    help="Si no conoce la talla ahora, déjelo en 0 (Pendiente de tallar). Podrá introducirla en cualquier momento después.",
                )

            crear_btn = st.form_submit_button(
                "➕ Crear Paciente e Iniciar Consulta",
                type="primary",
                use_container_width=True,
            )

            if crear_btn:
                if not nombre_nuevo.strip() or not apellidos_nuevo.strip():
                    st.error("Por favor, indique el Nombre y los Apellidos del paciente.")
                elif 0 < talla_cm_nuevo < 100:
                    st.error(
                        "Si introduce una talla, debe estar entre 100 y 230 cm (o déjelo en 0 si aún no la sabe)."
                    )
                else:
                    try:
                        nuevo_pac = crear_paciente(
                            supabase,
                            nhc=nhc_nuevo,
                            nombre=nombre_nuevo,
                            apellidos=apellidos_nuevo,
                            edad=int(edad_nueva),
                            sexo=sexo_nuevo,
                            talla_cm=talla_cm_nuevo if talla_cm_nuevo >= 100 else None,
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
        talla_guardada_num = (
            float(talla_guardada)
            if (talla_guardada is not None and float(talla_guardada) > 0)
            else 0.0
        )
        talla_guardada_str = (
            f"{talla_guardada_num:g} cm"
            if talla_guardada_num > 0
            else "⚠️ Pendiente de tallar"
        )

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
            with st.form(f"form_edit_demog_{paciente_actual['id']}"):
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
                    edit_talla = st.number_input(
                        "Talla (cm, 0=Pend.)",
                        min_value=0.0,
                        max_value=230.0,
                        value=talla_guardada_num,
                        step=1.0,
                        key="edit_pac_talla_top",
                    )

                if st.form_submit_button("💾 Guardar cambios del paciente"):
                    if 0 < edit_talla < 100:
                        st.error("Si introduce talla, debe estar entre 100 y 230 cm (o 0 si pendiente).")
                    else:
                        try:
                            pac_act = actualizar_datos_paciente(
                                supabase,
                                paciente_id=paciente_actual["id"],
                                nombre=edit_nombre,
                                apellidos=edit_apellidos,
                                edad=int(edit_edad),
                                sexo=edit_sexo,
                                nueva_talla_cm=edit_talla,
                            )
                            st.session_state["paciente_actual"] = pac_act
                            st.session_state["talla_editable_cm"] = float(edit_talla)
                            st.session_state["visitas_paciente"] = obtener_visitas_paciente(
                                supabase, paciente_actual["id"]
                            )
                            st.success("Datos del paciente actualizados (y el IMC recalculado en sus visitas).")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Error al actualizar paciente: {exc}")
        talla_actual_cm = float(
            st.session_state.get("talla_editable_cm", talla_guardada_num)
        )

    st.divider()

    # =========================================================================
    # PESTAÑAS PRINCIPALES (DASHBOARD Y LISTADO SIEMPRE ESTÁN DISPONIBLES)
    # =========================================================================
    (
        tab_consulta,
        tab_analitica,
        tab_tratamiento,
        tab_historico,
        tab_dashboard,
        tab_pacientes,
    ) = st.tabs(
        [
            "🩺 1. Consulta y Actuaciones Enfermería",
            "🧪 2. Analítica (Extractor HGUV)",
            "💊 3. Tratamiento (Abucasis / GAIA)",
            "📈 4. Evolución e Histórico",
            "📊 5. Cuadro de Mandos (Dashboard UIC)",
            "👥 6. Listado de Pacientes",
        ]
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

            col_peso, col_talla_cons, col_dif, col_imc = st.columns(4)
            with col_peso:
                peso_kg = st.number_input(
                    "Peso actual en consulta (kg)",
                    min_value=25.0,
                    max_value=250.0,
                    value=float(valor_peso_inicial),
                    step=0.1,
                    format="%.1f",
                )

            with col_talla_cons:
                talla_actual_cm = float(
                    st.number_input(
                        "Talla (cm) — 0 = Pendiente",
                        min_value=0.0,
                        max_value=230.0,
                        value=float(
                            st.session_state.get("talla_editable_cm", talla_guardada_num)
                        ),
                        step=1.0,
                        key=f"talla_consulta_{paciente_actual['id']}",
                        help="Puede modificar o añadir la talla aquí en cualquier momento (aunque ya haya guardado la visita).",
                    )
                )
                st.session_state["talla_editable_cm"] = talla_actual_cm

            if peso_previo_val is not None:
                diferencia_peso_kg: Optional[float] = round(
                    peso_kg - peso_previo_val, 2
                )
            else:
                diferencia_peso_kg = None

            if talla_actual_cm >= 100:
                altura_m = talla_actual_cm / 100.0
                imc_calculado: Optional[float] = round(peso_kg / (altura_m**2), 2)
            else:
                imc_calculado = None

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
                if imc_calculado is not None:
                    st.metric(
                        label="IMC calculado",
                        value=f"{imc_calculado:.2f} kg/m²",
                        delta=f"Talla: {talla_actual_cm:g} cm",
                        delta_color="off",
                    )
                else:
                    st.metric(
                        label="IMC calculado",
                        value="Pendiente",
                        delta="Sin talla (0 cm)",
                        delta_color="off",
                    )

            if 0 < talla_actual_cm < 100:
                st.warning(
                    "⚠️ Si introduce una talla, debe ser de al menos 100 cm (o déjela en 0 si está pendiente)."
                )
            elif talla_actual_cm <= 0:
                st.warning(
                    "📏 **Talla pendiente de registrar:** Cuando la tenga, introdúzcala aquí arriba y pulse **Guardar talla y recalcular IMC** (no usaremos 165 cm falsos)."
                )
            elif abs(talla_actual_cm - talla_guardada_num) >= 0.1:
                if st.button(
                    f"📏 Guardar nueva talla ({talla_actual_cm:g} cm) en ficha y recalcular IMC de visitas ya guardadas",
                    use_container_width=True,
                    key=f"btn_sync_talla_{paciente_actual['id']}",
                ):
                    try:
                        pac_act = actualizar_datos_paciente(
                            supabase,
                            paciente_id=paciente_actual["id"],
                            nombre=str(
                                paciente_actual.get("nombre")
                                or paciente_actual.get("iniciales_o_nombre")
                                or ""
                            ),
                            apellidos=str(paciente_actual.get("apellidos") or ""),
                            edad=int(paciente_actual.get("edad") or 70),
                            sexo=str(paciente_actual.get("sexo") or "Hombre"),
                            nueva_talla_cm=talla_actual_cm,
                            recalcular_imc=True,
                        )
                        st.session_state["paciente_actual"] = pac_act
                        st.session_state["visitas_paciente"] = obtener_visitas_paciente(
                            supabase, paciente_actual["id"]
                        )
                        st.success(
                            f"✅ Talla actualizada a {talla_actual_cm:g} cm y el IMC recalculado en todas las visitas guardadas."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Error al actualizar la talla: {exc}")

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
            # BLOQUE E: VARIABLES SOCIALES, SOPORTE UHD, EJERCICIO Y HÁBITOS (1 CLIC)
            # -----------------------------------------------------------------
            st.markdown(
                "#### 5. Entorno Social, Soporte UHD, Ejercicio Físico y Hábitos *(Selección rápida — se precarga de la visita previa)*"
            )

            col_soc1, col_soc2, col_soc3 = st.columns(3)

            with col_soc1:
                st.markdown("##### 🏠 Convivencia y Soporte UHD")
                vive_solo_prev = (
                    bool(ultima_visita.get("vive_solo"))
                    if (ultima_visita and ultima_visita.get("vive_solo") is not None)
                    else False
                )
                vive_solo_sel = st.radio(
                    "¿Vive solo/a?",
                    options=["No", "Sí"],
                    index=1 if vive_solo_prev else 0,
                    horizontal=True,
                )
                vive_solo_bool = vive_solo_sel == "Sí"

                ops_convive = [
                    "Esposa/o o Pareja",
                    "Hijo/a",
                    "Esposa/o e hijos",
                    "Otros familiares",
                    "Cuidador/a formal",
                    "Residencia",
                ]
                if not vive_solo_bool:
                    conv_prev = (
                        ultima_visita.get("convive_con") if ultima_visita else None
                    )
                    idx_conv = (
                        ops_convive.index(conv_prev)
                        if conv_prev in ops_convive
                        else 0
                    )
                    convive_con = st.selectbox(
                        "¿Con quién vive?", options=ops_convive, index=idx_conv
                    )
                else:
                    convive_con = "Vive solo/a"

                ops_acomp = [
                    "Sí (Esposa/o o Pareja)",
                    "Sí (Hijo/a)",
                    "Sí (Otro familiar / Cuidador)",
                    "No (Viene solo/a)",
                ]
                acomp_prev = (
                    ultima_visita.get("viene_acompanado") if ultima_visita else None
                )
                idx_acomp = (
                    ops_acomp.index(acomp_prev) if acomp_prev in ops_acomp else 0
                )
                viene_acompanado = st.selectbox(
                    "¿Viene acompañado/a a consulta?",
                    options=ops_acomp,
                    index=idx_acomp,
                )

                uhd_prev = (
                    bool(ultima_visita.get("soporte_24h_uhd"))
                    if (
                        ultima_visita
                        and ultima_visita.get("soporte_24h_uhd") is not None
                    )
                    else (not vive_solo_bool)
                )
                soporte_24h_uhd = st.checkbox(
                    "🚑 Vive con alguien 24h que pueda ayudarle (Apto UHD)",
                    value=uhd_prev,
                )

            with col_soc2:
                st.markdown("##### 🏃 Situación Laboral y Ejercicio Físico")
                ops_lab = [
                    "Jubilado/a",
                    "Pensionista",
                    "Activo/a",
                    "Tareas del hogar / Otros",
                ]
                lab_prev = (
                    ultima_visita.get("situacion_laboral") if ultima_visita else None
                )
                idx_lab = ops_lab.index(lab_prev) if lab_prev in ops_lab else 0
                situacion_laboral = st.selectbox(
                    "Situación laboral", options=ops_lab, index=idx_lab
                )

                ops_ej = [
                    "Camina",
                    "Gimnasio / Rehabilitación",
                    "Nada (Sedentario)",
                    "Otros (bicicleta, natación...)",
                ]
                ej_prev = ultima_visita.get("tipo_ejercicio") if ultima_visita else None
                idx_ej = ops_ej.index(ej_prev) if ej_prev in ops_ej else 0
                tipo_ejercicio = st.selectbox(
                    "Ejercicio físico habitual", options=ops_ej, index=idx_ej
                )

                ejercicio_duracion: Optional[str] = None
                ejercicio_frecuencia: Optional[str] = None
                if tipo_ejercicio != "Nada (Sedentario)":
                    ops_dur = ["< 15 min", "15 - 30 min", "30 - 60 min", "> 60 min"]
                    dur_prev = (
                        ultima_visita.get("ejercicio_duracion")
                        if ultima_visita
                        else None
                    )
                    idx_dur = ops_dur.index(dur_prev) if dur_prev in ops_dur else 2

                    ops_frec = [
                        "1 - 2 veces/semana",
                        "3 - 4 veces/semana",
                        "5 - 6 veces/semana",
                        "Diario (7 días/semana)",
                    ]
                    frec_prev = (
                        ultima_visita.get("ejercicio_frecuencia")
                        if ultima_visita
                        else None
                    )
                    idx_frec = ops_frec.index(frec_prev) if frec_prev in ops_frec else 2

                    c_ej1, c_ej2 = st.columns(2)
                    with c_ej1:
                        ejercicio_duracion = st.selectbox(
                            "¿Cuánto tiempo?", options=ops_dur, index=idx_dur
                        )
                    with c_ej2:
                        ejercicio_frecuencia = st.selectbox(
                            "¿Cuántas veces/sem?", options=ops_frec, index=idx_frec
                        )

            with col_soc3:
                st.markdown("##### 🧂 Autonomía, Movilidad y Hábitos")
                ops_auto = [
                    "Autónomo/a (medicación y peso)",
                    "Supervisado por familiar/cuidador",
                    "Dependiente de cuidador",
                ]
                auto_prev = (
                    ultima_visita.get("autonomia_autocuidado")
                    if ultima_visita
                    else None
                )
                idx_auto = ops_auto.index(auto_prev) if auto_prev in ops_auto else 0
                autonomia_autocuidado = st.selectbox(
                    "Gestión de medicación y autocuidado",
                    options=ops_auto,
                    index=idx_auto,
                )

                ops_mov = [
                    "Autónomo sin ayudas",
                    "Bastón / Muleta",
                    "Andador",
                    "Silla de ruedas / Movilidad muy reducida",
                ]
                mov_prev = (
                    ultima_visita.get("movilidad_basica") if ultima_visita else None
                )
                idx_mov = ops_mov.index(mov_prev) if mov_prev in ops_mov else 0
                movilidad_basica = st.selectbox(
                    "Movilidad básica", options=ops_mov, index=idx_mov
                )

                ops_dieta = [
                    "Cumple dieta hiposódica",
                    "Cumplimiento parcial",
                    "No cumple / Transgresiones",
                ]
                dieta_prev = (
                    ultima_visita.get("dieta_hiposodica") if ultima_visita else None
                )
                idx_dieta = (
                    ops_dieta.index(dieta_prev) if dieta_prev in ops_dieta else 0
                )
                dieta_hiposodica = st.selectbox(
                    "Dieta hiposódica y control hídrico",
                    options=ops_dieta,
                    index=idx_dieta,
                )

                # Tabaco (Sí / No / Exfumador y cuánto si fuma)
                ops_tabaco = ["No", "Exfumador/a", "Sí"]
                tab_prev = (
                    ultima_visita.get("tabaco_estado") if ultima_visita else None
                )
                idx_tab = (
                    ops_tabaco.index(tab_prev) if tab_prev in ops_tabaco else 0
                )
                tabaco_estado = st.radio(
                    "🚬 Tabaco",
                    options=ops_tabaco,
                    index=idx_tab,
                    horizontal=True,
                )
                tabaco_cantidad: Optional[str] = None
                if tabaco_estado == "Sí":
                    ops_tab_cant = [
                        "< 5 cig/día",
                        "5 - 10 cig/día",
                        "10 - 20 cig/día (1 paq.)",
                        "> 20 cig/día",
                    ]
                    tc_prev = (
                        ultima_visita.get("tabaco_cantidad")
                        if ultima_visita
                        else None
                    )
                    idx_tc = (
                        ops_tab_cant.index(tc_prev)
                        if tc_prev in ops_tab_cant
                        else 1
                    )
                    tabaco_cantidad = st.selectbox(
                        "¿Cuánto tabaco?", options=ops_tab_cant, index=idx_tc
                    )

                # Alcohol (Sí / No y cuánto si consume)
                ops_alcohol = ["No", "Sí"]
                alc_prev = (
                    ultima_visita.get("alcohol_estado") if ultima_visita else None
                )
                idx_alc = (
                    ops_alcohol.index(alc_prev) if alc_prev in ops_alcohol else 0
                )
                alcohol_estado = st.radio(
                    "🍷 Alcohol",
                    options=ops_alcohol,
                    index=idx_alc,
                    horizontal=True,
                )
                alcohol_cantidad: Optional[str] = None
                if alcohol_estado == "Sí":
                    ops_alc_cant = [
                        "Ocasional (fines de semana)",
                        "1 copa/cerveza al día",
                        "2 copas al día",
                        "> 2 copas al día / habitual",
                    ]
                    ac_prev = (
                        ultima_visita.get("alcohol_cantidad")
                        if ultima_visita
                        else None
                    )
                    idx_ac = (
                        ops_alc_cant.index(ac_prev)
                        if ac_prev in ops_alc_cant
                        else 0
                    )
                    alcohol_cantidad = st.selectbox(
                        "¿Cuánto alcohol?", options=ops_alc_cant, index=idx_ac
                    )

            # Construir línea de resumen social y hábitos para la HC
            convivencia_txt = (
                "Vive solo/a"
                if vive_solo_bool
                else f"Vive acompañado/a ({convive_con})"
            )
            uhd_txt = "Sí (Apto UHD)" if soporte_24h_uhd else "No"
            if tipo_ejercicio == "Nada (Sedentario)":
                ejercicio_txt = "Sedentario (no realiza ejercicio)"
            else:
                ejercicio_txt = (
                    f"{tipo_ejercicio} ({ejercicio_duracion}, {ejercicio_frecuencia})"
                )

            tabaco_txt = (
                f"Sí ({tabaco_cantidad})"
                if tabaco_estado == "Sí"
                else tabaco_estado
            )
            alcohol_txt = (
                f"Sí ({alcohol_cantidad})"
                if alcohol_estado == "Sí"
                else "No"
            )

            resumen_social_habitos = (
                f"{convivencia_txt} | Soporte 24h para UHD: {uhd_txt} | "
                f"Acude acompañado: {viene_acompanado} | Situación laboral: {situacion_laboral} | "
                f"Actividad física: {ejercicio_txt} | Tabaco: {tabaco_txt} | Alcohol: {alcohol_txt} | "
                f"Autocuidado: {autonomia_autocuidado} | Movilidad: {movilidad_basica} | Dieta: {dieta_hiposodica}."
            )

            st.markdown("---")
            # -----------------------------------------------------------------
            # BLOQUE F: ADHERENCIA, MEDICACIÓN Y NOTAS
            # -----------------------------------------------------------------
            st.markdown("#### 6. Tratamiento Habitual, Adherencia y Observaciones")
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

            # Construir el payload de la visita actual en tiempo real para que el texto plano
            # refleje al instante todas las casillas marcadas por enfermería y la analítica
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
                "imc": float(imc_calculado) if imc_calculado is not None else None,
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
                "vive_solo": bool(vive_solo_bool),
                "convive_con": convive_con,
                "viene_acompanado": viene_acompanado,
                "soporte_24h_uhd": bool(soporte_24h_uhd),
                "situacion_laboral": situacion_laboral,
                "tipo_ejercicio": tipo_ejercicio,
                "ejercicio_duracion": ejercicio_duracion,
                "ejercicio_frecuencia": ejercicio_frecuencia,
                "autonomia_autocuidado": autonomia_autocuidado,
                "movilidad_basica": movilidad_basica,
                "dieta_hiposodica": dieta_hiposodica,
                "tabaco_estado": tabaco_estado,
                "tabaco_cantidad": tabaco_cantidad,
                "alcohol_estado": alcohol_estado,
                "alcohol_cantidad": alcohol_cantidad,
                "resumen_social_habitos": resumen_social_habitos,
                "notas_post_consulta": notas_post_consulta.strip() or None,
                "actuacion_post_completada": bool(hay_actuacion_post),
            }

            # Si el usuario ha extraído una analítica en la Pestaña 2 (tanto si ya la grabó
            # como si está en el Paso 1 en borrador), incluirla automáticamente en el texto plano
            analiticas_guardadas_pac = st.session_state.get("analiticas_paciente") or []
            borrador_ana_activo = st.session_state.get("borrador_analitica")
            if borrador_ana_activo and any(
                borrador_ana_activo.get(k) is not None
                for k, _, _ in PARAMETROS_ANALITICA_META
            ):
                analiticas_para_texto_hc = [borrador_ana_activo] + list(
                    analiticas_guardadas_pac
                )
            else:
                analiticas_para_texto_hc = list(analiticas_guardadas_pac)

            texto_hc_en_vivo = generar_texto_consulta_enfermeria(
                paciente=st.session_state["paciente_actual"],
                datos_visita=payload_visita,
                talla_cm=talla_actual_cm,
                resumen_incidencias_str=resumen_incidencias_str,
                analiticas_paciente=analiticas_para_texto_hc,
            )

            st.write("")
            guardar_pre_btn = st.button(
                "💾 Guardar Consulta de Enfermería en Base de Datos",
                type="primary",
                use_container_width=True,
            )

            if guardar_pre_btn:
                if 0 < talla_actual_cm < 100:
                    st.error("Si introduce talla, debe estar entre 100 y 230 cm (o déjela en 0 si aún está pendiente).")
                else:
                    try:
                        # Si se modificó la talla (o se añadió por primera vez), guardarla también en ficha y recalcular IMC previo
                        if abs(talla_actual_cm - talla_guardada_num) >= 0.1:
                            pac_act = actualizar_datos_paciente(
                                supabase,
                                paciente_id=paciente_actual["id"],
                                nombre=str(
                                    paciente_actual.get("nombre")
                                    or paciente_actual.get("iniciales_o_nombre")
                                    or ""
                                ),
                                apellidos=str(paciente_actual.get("apellidos") or ""),
                                edad=int(paciente_actual.get("edad") or 70),
                                sexo=str(paciente_actual.get("sexo") or "Hombre"),
                                nueva_talla_cm=talla_actual_cm,
                                recalcular_imc=True,
                            )
                            st.session_state["paciente_actual"] = pac_act

                        supabase.table("visitas_uic").insert(payload_visita).execute()

                        visitas_actualizadas = obtener_visitas_paciente(
                            supabase, paciente_actual["id"]
                        )
                        st.session_state["visitas_paciente"] = visitas_actualizadas
                        st.session_state["texto_pre_consulta"] = texto_hc_en_vivo
                        st.success("✅ Consulta guardada correctamente en Supabase.")
                    except Exception as exc:
                        st.error(f"Error al guardar la consulta: {exc}")

            st.markdown("---")
            mostrar_cuadro_copia_hc(
                titulo="Texto Plano de Enfermería + Analítica para Copiar a la Historia Clínica (Se actualiza en vivo)",
                texto_plano=texto_hc_en_vivo,
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

                # Actualizar el borrador en sesión con cualquier ajuste manual que haya hecho el usuario
                st.session_state["borrador_analitica"] = dict(valores_editados)

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
                            "vcm": valores_editados.get("vcm"),
                            "plaquetas": valores_editados.get("plaquetas"),
                            "probnp": valores_editados.get("probnp"),
                            "ca125": valores_editados.get("ca125"),
                            "cr": valores_editados.get("cr"),
                            "fge": valores_editados.get("fge"),
                            "fge_texto": valores_editados.get("fge_texto"),
                            "urea": valores_editados.get("urea"),
                            "k": valores_editados.get("k"),
                            "na": valores_editados.get("na"),
                            "acido_urico": valores_editados.get("acido_urico"),
                            "colesterol_total": valores_editados.get("colesterol_total"),
                            "ldl": valores_editados.get("ldl"),
                            "hdl": valores_editados.get("hdl"),
                            "trigliceridos": valores_editados.get("trigliceridos"),
                            "lpa": valores_editados.get("lpa"),
                            "cac_orina": valores_editados.get("cac_orina"),
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
                            "vcm",
                            "plaquetas",
                            "probnp",
                            "ca125",
                            "cr",
                            "fge_texto",
                            "fge",
                            "urea",
                            "k",
                            "na",
                            "acido_urico",
                            "colesterol_total",
                            "ldl",
                            "hdl",
                            "trigliceridos",
                            "lpa",
                            "cac_orina",
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

                st.markdown("---")
                excel_pac_bytes = generar_excel_completo(
                    [paciente_actual], visitas_hist, analiticas_hist
                )
                st.download_button(
                    label=f"📥 Descargar Excel de este paciente ({paciente_actual.get('nhc', 'paciente')}.xlsx)",
                    data=excel_pac_bytes,
                    file_name=f"UIC_Paciente_{paciente_actual.get('nhc', 'ficha')}_{date.today().isoformat()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                    key="dl_excel_paciente",
                )

    # =========================================================================
    # PESTAÑA 5: CUADRO DE MANDOS / DASHBOARD GLOBAL DE LA UNIDAD DE IC
    # =========================================================================
    with tab_dashboard:
        st.subheader("📊 Cuadro de Mandos Integral — Unidad de Insuficiencia Cardíaca")

        try:
            all_pac, all_vis, all_ana = obtener_todos_los_datos_uic(supabase)
        except Exception as exc:
            st.error(f"Error al cargar datos del Cuadro de Mandos: {exc}")
            all_pac, all_vis, all_ana = [], [], []

        if not all_pac and not all_vis:
            st.info(
                "Todavía no hay pacientes ni visitas registradas en la base de datos para mostrar estadísticas."
            )
        else:
            excel_completo_bytes = generar_excel_completo(all_pac, all_vis, all_ana)
            col_exp_info, col_exp_btn = st.columns([3, 2])
            with col_exp_info:
                st.info(
                    f"📦 **Exportación Excel lista:** {len(all_pac)} pacientes, "
                    f"{len(all_vis)} visitas y {len(all_ana)} analíticas "
                    "(4 pestañas: *Visitas_Completas*, *Analiticas*, *Resumen_Por_Paciente* y *Pacientes*)."
                )
            with col_exp_btn:
                st.write("")
                st.download_button(
                    label="📥 Descargar Excel Completo (.xlsx)",
                    data=excel_completo_bytes,
                    file_name=f"UIC_HGUV_Base_Completa_{date.today().isoformat()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary",
                    use_container_width=True,
                    key="dl_excel_dashboard",
                )

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

    # =========================================================================
    # PESTAÑA 6: DIRECTORIO EJECUTIVO DE PACIENTES (SELECCIÓN DIRECTA EN TABLA)
    # =========================================================================
    with tab_pacientes:
        st.subheader("👥 Directorio Ejecutivo de Pacientes")
        st.caption(
            "Haga clic directamente sobre cualquier fila de la tabla para ver su ficha ejecutiva, "
            "abrirla en consulta, modificar datos (ej. talla) o eliminar registros."
        )

        try:
            pac_listado, vis_listado, ana_listado = obtener_todos_los_datos_uic(supabase)
        except Exception as exc:
            st.error(f"Error al cargar el listado de pacientes: {exc}")
            pac_listado, vis_listado, ana_listado = [], [], []

        if not pac_listado:
            st.info("Todavía no hay pacientes registrados en la base de datos.")
        else:
            # Mapas de última visita, conteo de visitas y analíticas por paciente_id
            conteo_vis: Dict[Any, int] = {}
            ultima_vis_por_pac: Dict[Any, Dict[str, Any]] = {}
            for v in vis_listado:
                pid = v.get("paciente_id")
                conteo_vis[pid] = conteo_vis.get(pid, 0) + 1
                if pid not in ultima_vis_por_pac:
                    ultima_vis_por_pac[pid] = v

            conteo_ana: Dict[Any, int] = {}
            for a in ana_listado:
                pid = a.get("paciente_id")
                conteo_ana[pid] = conteo_ana.get(pid, 0) + 1

            # Construir registros enriquecidos y detectar estados clínicos clave
            registros_dir: List[Dict[str, Any]] = []
            n_talla_pendiente = 0
            n_con_alerta = 0

            for p in pac_listado:
                pid = p.get("id")
                uv = ultima_vis_por_pac.get(pid)
                talla_v = p.get("talla_cm")
                tiene_talla = talla_v is not None and float(talla_v) > 0
                if not tiene_talla:
                    n_talla_pendiente += 1

                alerta_flags: List[str] = []
                if uv:
                    dif_p = uv.get("diferencia_peso_kg")
                    if dif_p is not None and float(dif_p) >= 2.0:
                        alerta_flags.append(f"+{float(dif_p):.1f} kg")
                    if uv.get("clase_funcional_nyha") in ("III", "IV"):
                        alerta_flags.append(f"NYHA {uv.get('clase_funcional_nyha')}")
                    if uv.get("incidencias_bool"):
                        alerta_flags.append("Incidencia")
                if alerta_flags:
                    n_con_alerta += 1

                if not tiene_talla:
                    estado_str = "⚠️ Talla pendiente"
                elif alerta_flags:
                    estado_str = "🚨 " + " · ".join(alerta_flags)
                elif uv:
                    estado_str = "✅ Seguimiento OK"
                else:
                    estado_str = "🆕 Sin visitas"

                peso_ult = (
                    f"{float(uv['peso_kg']):.1f} kg"
                    if (uv and uv.get("peso_kg") is not None)
                    else "—"
                )
                ta_ult = (
                    f"{uv.get('tas_consulta')}/{uv.get('tad_consulta')}"
                    if (
                        uv
                        and uv.get("tas_consulta") is not None
                        and uv.get("tad_consulta") is not None
                    )
                    else "—"
                )
                nyha_ult = (
                    str(uv.get("clase_funcional_nyha"))
                    if (uv and uv.get("clase_funcional_nyha"))
                    else "—"
                )
                fecha_ult = (
                    str(uv.get("fecha_visita"))[:10]
                    if (uv and uv.get("fecha_visita"))
                    else "Sin visitas"
                )

                registros_dir.append(
                    {
                        "_id": pid,
                        "_tiene_talla": tiene_talla,
                        "_tiene_alerta": bool(alerta_flags),
                        "_sin_visitas": uv is None,
                        "NHC": str(p.get("nhc") or ""),
                        "Paciente": obtener_nombre_completo_paciente(p),
                        "Edad": int(p["edad"]) if p.get("edad") is not None else None,
                        "Sexo": str(p.get("sexo") or "—"),
                        "Talla": f"{float(talla_v):g} cm" if tiene_talla else "⚠️ Pendiente",
                        "Último Peso": peso_ult,
                        "Última TA": ta_ult,
                        "NYHA": nyha_ult,
                        "Última Visita": fecha_ult,
                        "Visitas": conteo_vis.get(pid, 0),
                        "Analíticas": conteo_ana.get(pid, 0),
                        "Estado": estado_str,
                    }
                )

            # 1. Tarjetas ejecutivas superiores (KPIs)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("👥 Pacientes Totales", len(pac_listado))
            m2.metric("🩺 Visitas Registradas", len(vis_listado))
            m3.metric("⚠️ Talla Pendiente", n_talla_pendiente)
            m4.metric("🚨 Con Alerta Clínica", n_con_alerta)

            st.markdown("")

            # 2. Barra de búsqueda y filtros rápidos tipo píldora
            col_busq, col_f_rapido = st.columns([2.2, 2.8])
            with col_busq:
                filtro_txt = st.text_input(
                    "🔎 Buscar paciente (NHC, Nombre o Apellidos)",
                    placeholder="Escriba NHC, nombre o apellidos...",
                    key="filtro_listado_pac",
                ).strip().lower()
            with col_f_rapido:
                filtro_estado = st.radio(
                    "Filtro rápido",
                    options=[
                        "Todos",
                        "⚠️ Talla pendiente",
                        "🚨 Con alerta",
                        "🆕 Sin visitas",
                    ],
                    horizontal=True,
                    key="filtro_rapido_pac",
                )

            df_base = pd.DataFrame(registros_dir).sort_values(
                by=["Paciente", "NHC"], ascending=True
            )

            mask = pd.Series(True, index=df_base.index)
            if filtro_txt:
                mask &= (
                    df_base["NHC"].str.lower().str.contains(filtro_txt, na=False)
                    | df_base["Paciente"].str.lower().str.contains(filtro_txt, na=False)
                    | df_base["_id"].astype(str).str.lower().str.contains(filtro_txt, na=False)
                )
            if filtro_estado == "⚠️ Talla pendiente":
                mask &= ~df_base["_tiene_talla"]
            elif filtro_estado == "🚨 Con alerta":
                mask &= df_base["_tiene_alerta"]
            elif filtro_estado == "🆕 Sin visitas":
                mask &= df_base["_sin_visitas"]

            df_filtrado = df_base[mask].reset_index(drop=True)
            ids_ordenados = df_filtrado["_id"].tolist()
            cols_visibles = [
                "NHC",
                "Paciente",
                "Edad",
                "Sexo",
                "Talla",
                "Último Peso",
                "Última TA",
                "NYHA",
                "Última Visita",
                "Visitas",
                "Analíticas",
                "Estado",
            ]

            # 3. Tabla interactiva con selección directa de fila (sin desplegable)
            evento_tabla = st.dataframe(
                df_filtrado[cols_visibles],
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                key="tabla_directorio_pacientes",
                column_config={
                    "NHC": st.column_config.TextColumn("NHC", width="small"),
                    "Paciente": st.column_config.TextColumn("Apellidos, Nombre", width="medium"),
                    "Edad": st.column_config.NumberColumn("Edad", format="%d a", width="small"),
                    "Visitas": st.column_config.NumberColumn("Vis.", width="small"),
                    "Analíticas": st.column_config.NumberColumn("Analít.", width="small"),
                },
            )

            filas_sel = (
                evento_tabla.selection.rows
                if (evento_tabla and hasattr(evento_tabla, "selection"))
                else []
            )

            if not filas_sel or filas_sel[0] >= len(ids_ordenados):
                st.info(
                    "👆 **Haga clic directamente en la casilla izquierda de cualquier fila de la tabla** "
                    "para abrir la ficha ejecutiva del paciente (cargar en consulta, editar talla/datos o eliminar)."
                )
            else:
                pac_sel_id = ids_ordenados[filas_sel[0]]
                mapa_pac_id = {p["id"]: p for p in pac_listado}
                pac_sel = mapa_pac_id[pac_sel_id]
                uv_sel = ultima_vis_por_pac.get(pac_sel_id)
                talla_sel_val = pac_sel.get("talla_cm")
                talla_sel_num = (
                    float(talla_sel_val)
                    if (talla_sel_val is not None and float(talla_sel_val) > 0)
                    else 0.0
                )

                # 4. Tarjeta Ejecutiva Contextual del paciente seleccionado
                with st.container(border=True):
                    c_head_info, c_head_btn = st.columns([3, 1.5])
                    with c_head_info:
                        st.markdown(
                            f"### 👤 {obtener_nombre_completo_paciente(pac_sel)}"
                        )
                        talla_badge = (
                            f"{talla_sel_num:g} cm"
                            if talla_sel_num > 0
                            else "⚠️ Pendiente de tallar"
                        )
                        st.markdown(
                            f"**NHC:** `{pac_sel.get('nhc', '-')}` · "
                            f"**Edad:** {pac_sel.get('edad') or '-'} años · "
                            f"**Sexo:** {pac_sel.get('sexo') or '-'} · "
                            f"**Talla:** {talla_badge}"
                        )
                        if uv_sel:
                            st.caption(
                                f"Última visita: {str(uv_sel.get('fecha_visita'))[:10]} | "
                                f"Peso: {uv_sel.get('peso_kg', '-')} kg | "
                                f"IMC: {uv_sel.get('imc') or 'Pendiente'} | "
                                f"TA: {uv_sel.get('tas_consulta', '-')}/{uv_sel.get('tad_consulta', '-')} mmHg | "
                                f"NYHA: {uv_sel.get('clase_funcional_nyha', '-')} | "
                                f"ID: {pac_sel_id}"
                            )
                        else:
                            st.caption(f"Sin visitas registradas aún · ID: {pac_sel_id}")

                    with c_head_btn:
                        st.write("")
                        if st.button(
                            "📂 Abrir en Consulta",
                            type="primary",
                            use_container_width=True,
                            key=f"btn_cargar_dir_{pac_sel_id}",
                        ):
                            vis_sel = obtener_visitas_paciente(supabase, pac_sel_id)
                            ana_sel = obtener_analiticas_paciente(supabase, pac_sel_id)
                            st.session_state["nhc_buscado"] = str(
                                pac_sel.get("nhc") or ""
                            )
                            cargar_estado_paciente(pac_sel, vis_sel, ana_sel)
                            st.success(
                                f"✅ Paciente cargado arriba. Ya puede trabajar en la Pestaña 1, 2, 3 o 4."
                            )
                            st.rerun()

                    # Pestañas internas limpias dentro de la tarjeta: Editar, Visitas/Analíticas o Eliminar paciente
                    sub_edit, sub_visitas, sub_del = st.tabs(
                        [
                            "✏️ Editar ficha / talla",
                            f"🩺 Visitas ({conteo_vis.get(pac_sel_id, 0)}) y Analíticas ({conteo_ana.get(pac_sel_id, 0)})",
                            "🗑️ Eliminar paciente",
                        ]
                    )

                    with sub_edit:
                        with st.form(f"form_editar_dir_{pac_sel_id}"):
                            fe1, fe2, fe3 = st.columns(3)
                            with fe1:
                                dir_nhc = st.text_input(
                                    "NHC *",
                                    value=str(pac_sel.get("nhc") or ""),
                                )
                            with fe2:
                                dir_nombre = st.text_input(
                                    "Nombre *",
                                    value=str(
                                        pac_sel.get("nombre")
                                        or pac_sel.get("iniciales_o_nombre")
                                        or ""
                                    ),
                                )
                            with fe3:
                                dir_apellidos = st.text_input(
                                    "Apellidos *",
                                    value=str(pac_sel.get("apellidos") or ""),
                                )

                            fe4, fe5, fe6 = st.columns(3)
                            with fe4:
                                dir_edad = st.number_input(
                                    "Edad (años)",
                                    min_value=15,
                                    max_value=115,
                                    value=int(pac_sel.get("edad") or 70),
                                    step=1,
                                )
                            with fe5:
                                ops_sx = ["Hombre", "Mujer", "Otro"]
                                idx_sx = (
                                    ops_sx.index(pac_sel.get("sexo"))
                                    if pac_sel.get("sexo") in ops_sx
                                    else 0
                                )
                                dir_sexo = st.selectbox(
                                    "Sexo", options=ops_sx, index=idx_sx
                                )
                            with fe6:
                                dir_talla = st.number_input(
                                    "Talla (cm, 0 = Pendiente)",
                                    min_value=0.0,
                                    max_value=230.0,
                                    value=talla_sel_num,
                                    step=1.0,
                                )

                            if st.form_submit_button(
                                "💾 Guardar Cambios y Recalcular IMC",
                                type="primary",
                                use_container_width=True,
                            ):
                                if not dir_nhc.strip() or not dir_nombre.strip():
                                    st.error("El NHC y el Nombre son obligatorios.")
                                elif 0 < dir_talla < 100:
                                    st.error(
                                        "Si introduce talla, debe estar entre 100 y 230 cm (o 0 si pendiente)."
                                    )
                                else:
                                    try:
                                        pac_actualizado = actualizar_datos_paciente(
                                            supabase,
                                            paciente_id=pac_sel_id,
                                            nhc=dir_nhc,
                                            nombre=dir_nombre,
                                            apellidos=dir_apellidos,
                                            edad=int(dir_edad),
                                            sexo=dir_sexo,
                                            nueva_talla_cm=dir_talla,
                                            recalcular_imc=True,
                                        )
                                        if (
                                            st.session_state.get("paciente_actual")
                                            and st.session_state["paciente_actual"].get(
                                                "id"
                                            )
                                            == pac_sel_id
                                        ):
                                            st.session_state["paciente_actual"] = (
                                                pac_actualizado
                                            )
                                            st.session_state["nhc_buscado"] = str(
                                                pac_actualizado.get("nhc")
                                                or dir_nhc.strip()
                                            )
                                            st.session_state["talla_editable_cm"] = (
                                                float(dir_talla)
                                            )
                                            st.session_state["visitas_paciente"] = (
                                                obtener_visitas_paciente(
                                                    supabase, pac_sel_id
                                                )
                                            )
                                        st.success(
                                            "✅ Ficha actualizada y el IMC recalculado en sus visitas."
                                        )
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(
                                            f"Error al actualizar el paciente: {exc}"
                                        )

                    with sub_visitas:
                        vis_pac_sel = [
                            v for v in vis_listado if v.get("paciente_id") == pac_sel_id
                        ]
                        ana_pac_sel = [
                            a for a in ana_listado if a.get("paciente_id") == pac_sel_id
                        ]

                        st.markdown("##### 🩺 Visitas registradas de este paciente")
                        if not vis_pac_sel:
                            st.info("Este paciente no tiene visitas registradas.")
                        else:
                            filas_vis_dir = []
                            for v in vis_pac_sel:
                                act_txt = ", ".join(
                                    construir_lineas_actuaciones_enfermeria(v)
                                ) or "—"
                                filas_vis_dir.append(
                                    {
                                        "Fecha": str(v.get("fecha_visita") or "")[:10],
                                        "NYHA": v.get("clase_funcional_nyha") or "—",
                                        "Peso (kg)": v.get("peso_kg"),
                                        "IMC": v.get("imc"),
                                        "TA (mmHg)": (
                                            f"{v.get('tas_consulta')}/{v.get('tad_consulta')}"
                                            if (
                                                v.get("tas_consulta") is not None
                                                and v.get("tad_consulta") is not None
                                            )
                                            else "—"
                                        ),
                                        "FC": v.get("fc_consulta"),
                                        "Incidencias": (
                                            v.get("detalle_incidencias") or "Sí"
                                        )
                                        if v.get("incidencias_bool")
                                        else "No",
                                        "Actuaciones": act_txt,
                                    }
                                )

                            ev_vis = st.dataframe(
                                pd.DataFrame(filas_vis_dir),
                                use_container_width=True,
                                hide_index=True,
                                on_select="rerun",
                                selection_mode="single-row",
                                key=f"tabla_vis_pac_{pac_sel_id}",
                            )
                            vis_rows_sel = (
                                ev_vis.selection.rows
                                if (ev_vis and hasattr(ev_vis, "selection"))
                                else []
                            )
                            if not vis_rows_sel or vis_rows_sel[0] >= len(vis_pac_sel):
                                st.caption(
                                    "👆 Haga clic en la casilla izquierda de una visita si desea eliminar esa visita concreta (sin borrar al paciente)."
                                )
                            else:
                                v_elegida = vis_pac_sel[vis_rows_sel[0]]
                                v_id = v_elegida["id"]
                                st.warning(
                                    f"Visita seleccionada: **{str(v_elegida.get('fecha_visita'))[:10]}** "
                                    f"(Peso: {v_elegida.get('peso_kg', '-')} kg | "
                                    f"TA: {v_elegida.get('tas_consulta', '-')}/{v_elegida.get('tad_consulta', '-')} mmHg | "
                                    f"NYHA: {v_elegida.get('clase_funcional_nyha', '-')}). "
                                    "Solo se borrará esta visita; el paciente y el resto de sus visitas se mantendrán intactos."
                                )
                                c_chk_v, c_btn_v = st.columns([2, 1.5])
                                with c_chk_v:
                                    conf_del_vis = st.checkbox(
                                        "Confirmo eliminar solo esta visita",
                                        key=f"chk_del_vis_{v_id}",
                                    )
                                with c_btn_v:
                                    if st.button(
                                        "🗑️ Eliminar Solo Esta Visita",
                                        type="primary",
                                        disabled=not conf_del_vis,
                                        use_container_width=True,
                                        key=f"btn_del_vis_{v_id}",
                                    ):
                                        try:
                                            eliminar_visita(supabase, v_id)
                                            if (
                                                st.session_state.get("paciente_actual")
                                                and st.session_state[
                                                    "paciente_actual"
                                                ].get("id")
                                                == pac_sel_id
                                            ):
                                                st.session_state[
                                                    "visitas_paciente"
                                                ] = obtener_visitas_paciente(
                                                    supabase, pac_sel_id
                                                )
                                            st.success(
                                                "✅ Visita eliminada correctamente (el paciente se mantiene)."
                                            )
                                            st.rerun()
                                        except Exception as exc:
                                            st.error(
                                                f"Error al eliminar la visita: {exc}"
                                            )

                        if ana_pac_sel:
                            st.markdown("---")
                            st.markdown("##### 🧪 Analíticas registradas de este paciente")
                            filas_ana_dir = [
                                {
                                    "Fecha": str(a.get("fecha_analitica") or "")[:10],
                                    "Hb": a.get("hb"),
                                    "ProBNP": a.get("probnp"),
                                    "Cr": a.get("cr"),
                                    "FGE": a.get("fge_texto") or a.get("fge"),
                                    "K": a.get("k"),
                                    "Na": a.get("na"),
                                }
                                for a in ana_pac_sel
                            ]
                            ev_ana = st.dataframe(
                                pd.DataFrame(filas_ana_dir),
                                use_container_width=True,
                                hide_index=True,
                                on_select="rerun",
                                selection_mode="single-row",
                                key=f"tabla_ana_pac_{pac_sel_id}",
                            )
                            ana_rows_sel = (
                                ev_ana.selection.rows
                                if (ev_ana and hasattr(ev_ana, "selection"))
                                else []
                            )
                            if not ana_rows_sel or ana_rows_sel[0] >= len(ana_pac_sel):
                                st.caption(
                                    "👆 Opcional: haga clic en una fila para eliminar solo una analítica concreta."
                                )
                            else:
                                a_elegida = ana_pac_sel[ana_rows_sel[0]]
                                a_id = a_elegida["id"]
                                c_chk_a, c_btn_a = st.columns([2, 1.5])
                                with c_chk_a:
                                    conf_del_ana = st.checkbox(
                                        f"Confirmo eliminar analítica del {str(a_elegida.get('fecha_analitica'))[:10]}",
                                        key=f"chk_del_ana_{a_id}",
                                    )
                                with c_btn_a:
                                    if st.button(
                                        "🗑️ Eliminar Solo Esta Analítica",
                                        type="primary",
                                        disabled=not conf_del_ana,
                                        use_container_width=True,
                                        key=f"btn_del_ana_{a_id}",
                                    ):
                                        try:
                                            eliminar_analitica(supabase, a_id)
                                            if (
                                                st.session_state.get("paciente_actual")
                                                and st.session_state[
                                                    "paciente_actual"
                                                ].get("id")
                                                == pac_sel_id
                                            ):
                                                st.session_state[
                                                    "analiticas_paciente"
                                                ] = obtener_analiticas_paciente(
                                                    supabase, pac_sel_id
                                                )
                                            st.success(
                                                "✅ Analítica eliminada correctamente."
                                            )
                                            st.rerun()
                                        except Exception as exc:
                                            st.error(
                                                f"Error al eliminar la analítica: {exc}"
                                            )

                    with sub_del:
                        st.warning(
                            f"Esta acción eliminará permanentemente a **{obtener_nombre_completo_paciente(pac_sel)}** "
                            f"(NHC `{pac_sel.get('nhc')}`) junto con **{conteo_vis.get(pac_sel_id, 0)} visitas** y "
                            f"**{conteo_ana.get(pac_sel_id, 0)} analíticas** asociadas."
                        )
                        confirmar_borrado = st.checkbox(
                            "Confirmo que deseo eliminar este paciente permanentemente",
                            key=f"chk_del_{pac_sel_id}",
                        )
                        if st.button(
                            "🗑️ Eliminar Definitivamente",
                            type="primary",
                            disabled=not confirmar_borrado,
                            use_container_width=True,
                            key=f"btn_del_{pac_sel_id}",
                        ):
                            try:
                                eliminar_paciente(supabase, pac_sel_id)
                                if (
                                    st.session_state.get("paciente_actual")
                                    and st.session_state["paciente_actual"].get("id")
                                    == pac_sel_id
                                ):
                                    limpiar_paciente_actual()
                                st.success(
                                    "Paciente eliminado correctamente de Supabase."
                                )
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Error al eliminar el paciente: {exc}")


if __name__ == "__main__":
    main()
