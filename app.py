import os
import re
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st
from supabase import create_client

# -----------------------------------------------------------------------------
# CONFIGURACIÓN DE LA PÁGINA
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Consulta de Enfermería - Unidad de IC",
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
    """
    Inicializa y devuelve el cliente de Supabase usando st.secrets["SUPABASE_URL"]
    y st.secrets["SUPABASE_KEY"]. Soporta tanto claves clásicas JWT como las nuevas
    claves 'sb_publishable_...' / 'sb_secret_...'.
    """
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

    st.markdown("## 🫀 Unidad de Insuficiencia Cardíaca — Consulta de Enfermería")
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
# FUNCIONES DE ACCESO A DATOS (SUPABASE)
# -----------------------------------------------------------------------------
def buscar_paciente_por_nhc(supabase: Any, nhc: str) -> Optional[Dict[str, Any]]:
    """Busca un paciente por NHC en la tabla 'pacientes'."""
    res = supabase.table("pacientes").select("*").eq("nhc", nhc).limit(1).execute()
    if res.data:
        return res.data[0]
    return None


def crear_paciente(
    supabase: Any, nhc: str, iniciales_o_nombre: str, talla_cm: float
) -> Dict[str, Any]:
    """Crea un nuevo registro en la tabla 'pacientes'."""
    talla_val = (
        int(round(talla_cm))
        if float(talla_cm).is_integer()
        else round(float(talla_cm), 1)
    )
    payload = {
        "nhc": nhc,
        "iniciales_o_nombre": iniciales_o_nombre.strip(),
        "talla_cm": talla_val,
    }
    res = supabase.table("pacientes").insert(payload).execute()
    return res.data[0]


def actualizar_talla_paciente(
    supabase: Any, paciente_id: Any, nueva_talla_cm: float
) -> None:
    """Actualiza la talla de un paciente existente en la tabla 'pacientes'."""
    talla_val = (
        int(round(nueva_talla_cm))
        if float(nueva_talla_cm).is_integer()
        else round(float(nueva_talla_cm), 1)
    )
    supabase.table("pacientes").update({"talla_cm": talla_val}).eq(
        "id", paciente_id
    ).execute()


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


def insertar_visita_pre_consulta(
    supabase: Any,
    payload_completo: Dict[str, Any],
    columnas_extendidas: List[str],
) -> None:
    """
    Inserta la visita en 'visitas_uic'. Si la base de datos aún no tiene creadas
    las columnas extendidas de incidencias, hace fallback automático a las columnas
    base para garantizar que nunca falle el guardado.
    """
    try:
        supabase.table("visitas_uic").insert(payload_completo).execute()
    except Exception:
        payload_base = {
            k: v for k, v in payload_completo.items() if k not in columnas_extendidas
        }
        supabase.table("visitas_uic").insert(payload_base).execute()


def cargar_estado_paciente(
    paciente: Dict[str, Any], visitas: List[Dict[str, Any]]
) -> None:
    """Actualiza st.session_state con los datos del paciente activo y su última visita."""
    st.session_state["paciente_actual"] = paciente
    st.session_state["visitas_paciente"] = visitas
    st.session_state["paciente_no_encontrado"] = False

    ultima_visita = visitas[0] if visitas else None
    med_previa = (
        (ultima_visita.get("medicacion_habitual") or "") if ultima_visita else ""
    )
    st.session_state["pre_medicacion_habitual"] = med_previa
    st.session_state["talla_editable_cm"] = float(paciente.get("talla_cm") or 165.0)
    st.session_state["texto_pre_consulta"] = None
    st.session_state["texto_post_consulta"] = None


# -----------------------------------------------------------------------------
# GENERACIÓN DE TEXTOS PLANOS PARA HISTORIA CLÍNICA (HC)
# -----------------------------------------------------------------------------
def _leer_plantilla_local() -> Optional[str]:
    """Lee 'plantilla_actual.html' si existe en el directorio de la aplicación."""
    ruta_plantilla = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "plantilla_actual.html"
    )
    if os.path.exists(ruta_plantilla):
        try:
            with open(ruta_plantilla, "r", encoding="utf-8") as f:
                contenido = f.read()
            texto_limpio = re.sub(r"<[^>]+>", "", contenido).strip()
            return texto_limpio if texto_limpio else None
        except Exception:
            return None
    return None


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


def generar_texto_pre_consulta(
    paciente: Dict[str, Any],
    datos_visita: Dict[str, Any],
    talla_cm: float,
    resumen_incidencias_str: str,
) -> str:
    """
    Genera el texto plano formateado de la valoración pre-consulta de enfermería
    para copiar y pegar directamente en la Historia Clínica (HC).
    """
    _ = _leer_plantilla_local()

    fecha_str = str(datos_visita.get("fecha_visita", date.today().isoformat()))[:10]
    nhc = paciente.get("nhc", "-")
    nombre = paciente.get("iniciales_o_nombre", "-")

    # Control domiciliario de TA
    if datos_visita.get("aporta_hoja_ta_domicilio"):
        tas_dom = datos_visita.get("tas_media_domicilio")
        tad_dom = datos_visita.get("tad_media_domicilio")
        texto_ta_dom = (
            f"Aporta hoja de registro de TA domiciliaria (TA media: {tas_dom}/{tad_dom} mmHg)."
        )
    else:
        texto_ta_dom = "No aporta hoja de registro domiciliario."

    # Antropometría y constantes en consulta
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
    ecg_str = "Sí" if datos_visita.get("ecg_realizado") else "No"

    # Adherencia y tratamiento
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

    lineas = [
        f"CONSULTA DE ENFERMERÍA - UNIDAD DE INSUFICIENCIA CARDÍACA ({fecha_str})",
        f"Paciente: {nombre} (NHC: {nhc})",
        "-" * 72,
        f"- Incidencias desde última visita: {resumen_incidencias_str}",
        f"- Situación Funcional: Clase funcional NYHA {datos_visita.get('clase_funcional_nyha', '-')}",
        f"- Control TA Domiciliaria: {texto_ta_dom}",
        f"- Antropometría: Peso actual {peso:.1f} kg ({dif_str}) | Talla: {talla_cm:g} cm | IMC: {imc:.2f} kg/m2",
        f"- Constantes en Consulta: TA {tas_con}/{tad_con} mmHg | FC {fc_con} lpm | ECG realizado: {ecg_str}",
        f"- Adherencia Terapéutica: {cumplimiento_str}.",
        "- Medicación Habitual Actual:",
        f"{med_habitual}",
    ]
    return "\n".join(lineas)


def generar_texto_post_consulta(
    paciente: Dict[str, Any],
    datos_post: Dict[str, Any],
    fecha_visita: str,
) -> str:
    """
    Genera el texto plano con las técnicas y tratamientos administrados
    en la actuación post-consulta médica para pegar en la hoja de enfermería / HC.
    """
    nhc = paciente.get("nhc", "-")
    nombre = paciente.get("iniciales_o_nombre", "-")
    fecha_str = str(fecha_visita)[:10]

    actuaciones = []

    if datos_post.get("furosemida_iv_bolo"):
        dosis_furo = datos_post.get("dosis_furosemida_iv_mg")
        dosis_txt = f"{dosis_furo} mg" if dosis_furo is not None else "dosis según pauta"
        actuaciones.append(
            f"- Administración de Furosemida (Seguril) IV en bolo: {dosis_txt}."
        )

    if datos_post.get("perfusor_furosemida"):
        actuaciones.append("- Colocación / administración de Perfusor de Furosemida IV.")

    if datos_post.get("ferinject_iv"):
        dosis_fe = datos_post.get("dosis_ferinject_mg")
        actuaciones.append(
            f"- Administración de Hierro carboximaltosa (Ferinject) IV: {dosis_fe} mg."
        )

    if datos_post.get("derivacion_hd_levosimendan"):
        actuaciones.append(
            "- Derivación / programación en Hospital de Día para Levosimendán."
        )

    if datos_post.get("analitica_realizada"):
        actuaciones.append("- Extracción de analítica sanguínea realizada.")

    if not actuaciones:
        actuaciones.append(
            "- Sin técnicas ni administración de medicación parenteral en esta visita."
        )

    notas = (datos_post.get("notas_post_consulta") or "").strip()

    lineas = [
        f"ACTUACIÓN POST-CONSULTA ENFERMERÍA - UNIDAD DE IC / HOSPITAL DE DÍA ({fecha_str})",
        f"Paciente: {nombre} (NHC: {nhc})",
        "-" * 72,
        "Técnicas y tratamientos realizados:",
        *actuaciones,
    ]
    if notas:
        lineas.extend(["", f"Observaciones / Notas post-consulta:\n{notas}"])

    return "\n".join(lineas)


def mostrar_cuadro_copia_hc(
    titulo: str, texto_plano: str, key_prefix: str
) -> None:
    """
    Muestra tanto un cuadro de texto plano editable (st.text_area) como un bloque
    con botón de copia rápida en 1 clic (st.code) para pegar en la Historia Clínica.
    """
    st.markdown(f"#### 📋 {titulo}")
    col_txt, col_code = st.columns(2)
    with col_txt:
        st.caption(
            "Cuadro de texto plano (puede editarlo o seleccionarlo todo con Ctrl+A / Cmd+A y copiar):"
        )
        texto_editado = st.text_area(
            "Texto plano para Historia Clínica (HC)",
            value=texto_plano,
            height=280,
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
        st.title("🫀 Consulta de Enfermería — Unidad de Insuficiencia Cardíaca")
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
    st.subheader("1. Identificación del Paciente")

    col_nhc, col_btn = st.columns([3, 1])
    with col_nhc:
        nhc_input = st.text_input(
            "Número de Historia Clínica (NHC)",
            value=st.session_state.get("nhc_buscado", ""),
            placeholder="Introduzca el NHC del paciente...",
        )
    with col_btn:
        st.write("")
        st.write("")
        buscar_clicked = st.button(
            "🔍 Buscar / Iniciar", type="primary", use_container_width=True
        )

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
                    cargar_estado_paciente(paciente, visitas)
                else:
                    st.session_state["paciente_actual"] = None
                    st.session_state["visitas_paciente"] = []
                    st.session_state["paciente_no_encontrado"] = True
            except Exception as exc:
                st.error(f"Error al consultar Supabase: {exc}")

    # -------------------------------------------------------------------------
    # CASO A: PACIENTE NO EXISTE -> FORMULARIO DE ALTA
    # -------------------------------------------------------------------------
    if st.session_state.get("paciente_no_encontrado"):
        nhc_nuevo = st.session_state.get("nhc_buscado", nhc_input.strip())
        st.warning(
            f"No existe ningún paciente registrado con el NHC **{nhc_nuevo}**. "
            "Complete los siguientes datos para darlo de alta:"
        )
        with st.form("form_nuevo_paciente"):
            col_nom, col_talla = st.columns(2)
            with col_nom:
                iniciales_o_nombre = st.text_input(
                    "Iniciales o Nombre del Paciente *",
                    placeholder="Ej. J.G.M. o Nombre Apellidos",
                )
            with col_talla:
                talla_cm_nuevo = st.number_input(
                    "Talla (cm) *",
                    min_value=100.0,
                    max_value=230.0,
                    value=165.0,
                    step=1.0,
                )
            crear_btn = st.form_submit_button(
                "➕ Crear Paciente e Iniciar Consulta", type="primary"
            )

            if crear_btn:
                if not iniciales_o_nombre.strip():
                    st.error("Debe indicar las iniciales o el nombre del paciente.")
                else:
                    try:
                        nuevo_pac = crear_paciente(
                            supabase,
                            nhc=nhc_nuevo,
                            iniciales_o_nombre=iniciales_o_nombre,
                            talla_cm=talla_cm_nuevo,
                        )
                        cargar_estado_paciente(nuevo_pac, [])
                        st.success(
                            f"Paciente {iniciales_o_nombre} (NHC: {nhc_nuevo}) creado correctamente."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"No se pudo crear el paciente en Supabase: {exc}")

    # -------------------------------------------------------------------------
    # CASO B: PACIENTE EXISTE -> RESUMEN Y PESTAÑAS CLÍNICAS
    # -------------------------------------------------------------------------
    paciente_actual = st.session_state.get("paciente_actual")
    if not paciente_actual:
        return

    visitas_paciente: List[Dict[str, Any]] = st.session_state.get(
        "visitas_paciente", []
    )
    ultima_visita: Optional[Dict[str, Any]] = (
        visitas_paciente[0] if visitas_paciente else None
    )

    # Preparar datos de resumen de la última visita
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

    talla_guardada = paciente_actual.get("talla_cm")
    talla_guardada_str = f"{talla_guardada} cm" if talla_guardada else "No registrada"

    st.info(
        f"**Paciente registrado:** {paciente_actual.get('iniciales_o_nombre')} "
        f"(**NHC:** `{paciente_actual.get('nhc')}`)  \n"
        f"📏 **Talla guardada:** {talla_guardada_str} | "
        f"📅 **Última visita:** {ult_fecha} | "
        f"⚖️ **Último peso:** {ult_peso_str} | "
        f"🩺 **Última TA:** {ult_ta_str}  \n"
        f"💊 **Medicación habitual previa (precargada abajo):** {ult_med}"
    )

    # Talla editable por si no se midió antes o requiere corrección
    col_talla_edit, col_talla_btn, _ = st.columns([2, 1, 3])
    with col_talla_edit:
        talla_actual_cm = st.number_input(
            "Talla del paciente (cm) — Editable",
            min_value=100.0,
            max_value=230.0,
            value=float(
                st.session_state.get("talla_editable_cm", talla_guardada or 165.0)
            ),
            step=1.0,
            key="input_talla_paciente",
        )
        st.session_state["talla_editable_cm"] = talla_actual_cm
    with col_talla_btn:
        st.write("")
        st.write("")
        if st.button("Actualizar talla", use_container_width=True):
            try:
                actualizar_talla_paciente(
                    supabase, paciente_actual["id"], talla_actual_cm
                )
                st.session_state["paciente_actual"]["talla_cm"] = talla_actual_cm
                st.success("Talla actualizada.")
                st.rerun()
            except Exception as exc:
                st.error(f"Error al actualizar la talla: {exc}")

    st.divider()

    # =========================================================================
    # PESTAÑAS DEL FLUJO CLÍNICO
    # =========================================================================
    tab_pre, tab_post, tab_hist = st.tabs(
        [
            "📋 1. Valoración Pre-Consulta (Enfermería)",
            "💉 2. Actuación Post-Consulta Médica",
            "📈 3. Histórico y Gráficas",
        ]
    )

    # =========================================================================
    # PESTAÑA 1: VALORACIÓN PRE-CONSULTA (ENFERMERÍA)
    # =========================================================================
    with tab_pre:
        st.subheader("Valoración Pre-Consulta de Enfermería")

        col_fecha, col_nyha = st.columns(2)
        with col_fecha:
            fecha_visita_input = st.date_input("Fecha de la visita", value=date.today())
        with col_nyha:
            st.markdown("##### Situación Funcional")
            clase_nyha = st.selectbox(
                "Clase Funcional NYHA",
                options=["I", "II", "III", "IV"],
                index=1,
            )

        st.markdown("---")
        # 1. Evolución e Incidencias (Cardiológicas y No Cardiológicas)
        st.markdown("#### Evolución e Incidencias")
        incidencias_bool = st.checkbox("¿Ha tenido incidencias desde la última visita?")

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
                st.markdown("##### 🫀 Incidencias Cardiológicas *(puede marcar varias)*")
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
                st.markdown("##### 🏥 Incidencias No Cardiológicas *(puede marcar varias)*")
                inc_nocardio_ingreso = st.checkbox("Ingreso no cardiológico")
                inc_nocardio_urgencias = st.checkbox("Urgencias no cardiológicas")

            detalle_incidencias_libre = st.text_area(
                "Detalle adicional de las incidencias (fechas, motivo, hospital, evolución...)",
                placeholder="Describa fechas o detalles clínicos de los ingresos, urgencias o ajustes de diurético...",
            )

        # Listas de incidencias marcadas
        cardio_seleccionadas: List[str] = []
        if inc_cardio_muerte:
            cardio_seleccionadas.append("Muerte")
        if inc_cardio_ingreso_ic:
            cardio_seleccionadas.append("Ingreso por IC")
        if inc_cardio_ingreso_otras:
            cardio_seleccionadas.append("Ingreso por otras patologías cardiológicas")
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
        # 2. Control Domiciliario de TA
        st.markdown("#### Control Domiciliario de TA")
        aporta_hoja_ta_domicilio = st.checkbox(
            "Aporta hoja de registro de TA domiciliaria"
        )

        if aporta_hoja_ta_domicilio:
            col_tas_dom, col_tad_dom = st.columns(2)
            with col_tas_dom:
                tas_media_domicilio: Optional[int] = int(
                    st.number_input(
                        "TAS media domicilio (mmHg)",
                        min_value=50,
                        max_value=260,
                        value=120,
                        step=1,
                    )
                )
            with col_tad_dom:
                tad_media_domicilio: Optional[int] = int(
                    st.number_input(
                        "TAD media domicilio (mmHg)",
                        min_value=30,
                        max_value=160,
                        value=75,
                        step=1,
                    )
                )
        else:
            tas_media_domicilio = None
            tad_media_domicilio = None
            st.caption("ℹ️ Registro: *No aporta hoja de registro domiciliario*.")

        st.markdown("---")
        # 3. Antropometría y Constantes
        st.markdown("#### Antropometría y Constantes en Consulta")

        peso_previo_val: Optional[float] = None
        if ultima_visita and ultima_visita.get("peso_kg") is not None:
            try:
                peso_previo_val = float(ultima_visita["peso_kg"])
            except (TypeError, ValueError):
                peso_previo_val = None

        valor_peso_inicial = peso_previo_val if peso_previo_val is not None else 70.0

        col_peso, col_dif, col_imc = st.columns(3)
        with col_peso:
            peso_kg = st.number_input(
                "Peso actual (kg)",
                min_value=25.0,
                max_value=250.0,
                value=float(valor_peso_inicial),
                step=0.1,
                format="%.1f",
            )

        # Cálculo automático en tiempo real de diferencia de peso e IMC
        if peso_previo_val is not None:
            diferencia_peso_kg: Optional[float] = round(peso_kg - peso_previo_val, 2)
        else:
            diferencia_peso_kg = None

        altura_m = talla_actual_cm / 100.0 if talla_actual_cm > 0 else 1.65
        imc_calculado = round(peso_kg / (altura_m**2), 2)

        with col_dif:
            if diferencia_peso_kg is not None:
                signo_delta = "+" if diferencia_peso_kg > 0 else ""
                st.metric(
                    label="Diferencia de peso vs. visita previa",
                    value=f"{signo_delta}{diferencia_peso_kg:.2f} kg",
                    delta=f"Previo: {peso_previo_val:.1f} kg",
                    delta_color="inverse",
                )
            else:
                st.metric(
                    label="Diferencia de peso vs. visita previa",
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

        # Alerta roja si el peso sube >= 2 kg
        if diferencia_peso_kg is not None and diferencia_peso_kg >= 2.0:
            st.error("ALERTA CLÍNICA: Ganancia ponderal >= 2 kg")

        col_tas_c, col_tad_c, col_fc_c, col_ecg = st.columns(4)
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
        with col_ecg:
            st.write("")
            st.write("")
            ecg_realizado = st.checkbox("ECG realizado")

        st.markdown("---")
        # 4. Tratamiento y Adherencia
        st.markdown("#### Tratamiento y Adherencia")
        medicacion_habitual = st.text_area(
            "Medicación habitual (precargada de la visita anterior si existe)",
            key="pre_medicacion_habitual",
            height=130,
            placeholder="Detalle los fármacos y dosis actuales del paciente...",
        )
        buen_cumplimiento = st.checkbox("Buen cumplimiento terapéutico", value=True)
        observaciones_cumplimiento = st.text_input(
            "Observaciones de cumplimiento",
            placeholder="Ej. Olvidos ocasionales, dudas con pauta diurética, trae blíster...",
        )

        st.write("")
        guardar_pre_btn = st.button(
            "💾 Guardar Pre-Consulta y Generar Texto para HC",
            type="primary",
            use_container_width=True,
        )

        if guardar_pre_btn:
            try:
                # Si se modificó la talla respecto a la guardada en BD, actualizarla también
                if talla_actual_cm != float(paciente_actual.get("talla_cm") or 0):
                    actualizar_talla_paciente(
                        supabase, paciente_actual["id"], talla_actual_cm
                    )
                    st.session_state["paciente_actual"]["talla_cm"] = talla_actual_cm

                acudio_urgencias_any = bool(
                    inc_cardio_urgencias_ic or inc_nocardio_urgencias
                )
                aumento_diureticos_any = bool(
                    inc_cardio_diureticos_iv or inc_cardio_aumento_diureticos_vo
                )

                columnas_extendidas = [
                    "inc_cardio_muerte",
                    "inc_cardio_ingreso_ic",
                    "inc_cardio_ingreso_otras",
                    "inc_cardio_urgencias_ic",
                    "inc_cardio_diureticos_iv",
                    "inc_cardio_aumento_diureticos_vo",
                    "inc_nocardio_ingreso",
                    "inc_nocardio_urgencias",
                ]

                payload_pre: Dict[str, Any] = {
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
                    "medicacion_habitual": medicacion_habitual.strip(),
                    "buen_cumplimiento": bool(buen_cumplimiento),
                    "observaciones_cumplimiento": observaciones_cumplimiento.strip()
                    or None,
                    "actuacion_post_completada": False,
                }

                insertar_visita_pre_consulta(
                    supabase, payload_pre, columnas_extendidas
                )

                # Refrescar visitas del paciente en sesión
                visitas_actualizadas = obtener_visitas_paciente(
                    supabase, paciente_actual["id"]
                )
                st.session_state["visitas_paciente"] = visitas_actualizadas

                # Generar texto plano para Historia Clínica
                st.session_state["texto_pre_consulta"] = generar_texto_pre_consulta(
                    paciente=st.session_state["paciente_actual"],
                    datos_visita=payload_pre,
                    talla_cm=talla_actual_cm,
                    resumen_incidencias_str=resumen_incidencias_str,
                )
                st.success("✅ Valoración pre-consulta guardada correctamente en Supabase.")
            except Exception as exc:
                st.error(f"Error al guardar la valoración pre-consulta: {exc}")

        if st.session_state.get("texto_pre_consulta"):
            st.markdown("---")
            mostrar_cuadro_copia_hc(
                titulo="Texto Plano para Copiar a la Historia Clínica (Pre-Consulta)",
                texto_plano=st.session_state["texto_pre_consulta"],
                key_prefix="pre_hc",
            )

    # =========================================================================
    # PESTAÑA 2: ACTUACIÓN POST-CONSULTA MÉDICA
    # =========================================================================
    with tab_post:
        st.subheader("Actuación Post-Consulta Médica (Hospital de Día / Consulta)")

        visitas_para_post = st.session_state.get("visitas_paciente", [])
        if not visitas_para_post:
            st.info(
                "El paciente aún no tiene visitas registradas. "
                "Guarde primero la **Valoración Pre-Consulta** en la Pestaña 1."
            )
        else:
            opciones_visita = {
                v["id"]: (
                    f"Visita del {str(v.get('fecha_visita'))[:10]} "
                    f"(Peso: {v.get('peso_kg', '-')} kg | TA: {v.get('tas_consulta', '-')}/{v.get('tad_consulta', '-')} mmHg | "
                    f"Post-consulta: {'Completada ✅' if v.get('actuacion_post_completada') else 'Pendiente ⏳'})"
                )
                for v in visitas_para_post
            }

            visita_id_sel = st.selectbox(
                "Seleccione la visita a completar / actualizar",
                options=list(opciones_visita.keys()),
                format_func=lambda vid: opciones_visita[vid],
            )

            visita_sel = next(
                (v for v in visitas_para_post if v["id"] == visita_id_sel),
                visitas_para_post[0],
            )

            st.markdown("---")
            st.markdown("#### Tratamientos y Técnicas Administradas")

            # 1. Furosemida IV en bolo + dosis (mg)
            col_furo_chk, col_furo_dosis = st.columns([2, 2])
            with col_furo_chk:
                furosemida_iv_bolo = st.checkbox(
                    "Furosemida (Seguril) IV en bolo",
                    value=bool(visita_sel.get("furosemida_iv_bolo") or False),
                    key=f"furo_bolo_{visita_id_sel}",
                )
            with col_furo_dosis:
                dosis_furo_previa = visita_sel.get("dosis_furosemida_iv_mg")
                dosis_furosemida_iv_mg: Optional[int] = None
                if furosemida_iv_bolo:
                    dosis_furosemida_iv_mg = int(
                        st.number_input(
                            "Dosis de Furosemida IV (mg)",
                            min_value=10,
                            max_value=500,
                            value=int(dosis_furo_previa) if dosis_furo_previa else 40,
                            step=10,
                            key=f"furo_dosis_{visita_id_sel}",
                        )
                    )

            # 2. Perfusor de Furosemida IV
            perfusor_furosemida = st.checkbox(
                "Perfusor de Furosemida IV",
                value=bool(visita_sel.get("perfusor_furosemida") or False),
                key=f"perfusor_furo_{visita_id_sel}",
            )

            # 3. Hierro carboximaltosa (Ferinject) IV + selector de dosis
            col_fe_chk, col_fe_dosis = st.columns([2, 2])
            with col_fe_chk:
                ferinject_iv = st.checkbox(
                    "Hierro carboximaltosa (Ferinject) IV",
                    value=bool(visita_sel.get("ferinject_iv") or False),
                    key=f"ferinject_chk_{visita_id_sel}",
                )
            with col_fe_dosis:
                opciones_ferinject = [500, 1000, 1500, 2000]
                dosis_fe_previa = visita_sel.get("dosis_ferinject_mg")
                idx_fe = (
                    opciones_ferinject.index(int(dosis_fe_previa))
                    if (
                        dosis_fe_previa
                        and int(dosis_fe_previa) in opciones_ferinject
                    )
                    else 1
                )
                dosis_ferinject_mg: Optional[int] = None
                if ferinject_iv:
                    dosis_ferinject_mg = int(
                        st.selectbox(
                            "Dosis de Ferinject (mg)",
                            options=opciones_ferinject,
                            index=idx_fe,
                            key=f"ferinject_dosis_{visita_id_sel}",
                        )
                    )

            # 4. Derivación a Hospital de Día para Levosimendán
            derivacion_hd_levosimendan = st.checkbox(
                "Derivación a Hospital de Día para Levosimendán",
                value=bool(visita_sel.get("derivacion_hd_levosimendan") or False),
                key=f"levosimendan_chk_{visita_id_sel}",
            )

            # 5. Extracción de analítica realizada
            analitica_realizada = st.checkbox(
                "Extracción de analítica realizada",
                value=bool(visita_sel.get("analitica_realizada") or False),
                key=f"analitica_chk_{visita_id_sel}",
            )

            # 6. Notas post-consulta
            notas_post_consulta = st.text_area(
                "Notas post-consulta",
                value=visita_sel.get("notas_post_consulta") or "",
                placeholder="Vía canalizada, tolerancia al tratamiento, educación sanitaria realizada, próxima cita...",
                key=f"notas_post_{visita_id_sel}",
            )

            st.write("")
            actualizar_post_btn = st.button(
                "💾 Actualizar Visita y Generar Texto de Actuación",
                type="primary",
                use_container_width=True,
            )

            if actualizar_post_btn:
                try:
                    payload_post: Dict[str, Any] = {
                        "actuacion_post_completada": True,
                        "furosemida_iv_bolo": bool(furosemida_iv_bolo),
                        "dosis_furosemida_iv_mg": (
                            int(dosis_furosemida_iv_mg) if furosemida_iv_bolo else None
                        ),
                        "perfusor_furosemida": bool(perfusor_furosemida),
                        "ferinject_iv": bool(ferinject_iv),
                        "dosis_ferinject_mg": (
                            int(dosis_ferinject_mg) if ferinject_iv else None
                        ),
                        "derivacion_hd_levosimendan": bool(derivacion_hd_levosimendan),
                        "analitica_realizada": bool(analitica_realizada),
                        "notas_post_consulta": notas_post_consulta.strip() or None,
                    }

                    supabase.table("visitas_uic").update(payload_post).eq(
                        "id", visita_id_sel
                    ).execute()

                    # Refrescar lista de visitas en sesión
                    visitas_actualizadas = obtener_visitas_paciente(
                        supabase, paciente_actual["id"]
                    )
                    st.session_state["visitas_paciente"] = visitas_actualizadas

                    st.session_state["texto_post_consulta"] = generar_texto_post_consulta(
                        paciente=st.session_state["paciente_actual"],
                        datos_post=payload_post,
                        fecha_visita=str(
                            visita_sel.get("fecha_visita", date.today().isoformat())
                        ),
                    )
                    st.success(
                        "✅ Actuación post-consulta actualizada correctamente en Supabase."
                    )
                except Exception as exc:
                    st.error(f"Error al actualizar la visita en Supabase: {exc}")

            if st.session_state.get("texto_post_consulta"):
                st.markdown("---")
                mostrar_cuadro_copia_hc(
                    titulo="Texto Plano de Actuación Post-Consulta para Hoja de Enfermería / HC",
                    texto_plano=st.session_state["texto_post_consulta"],
                    key_prefix="post_hc",
                )

    # =========================================================================
    # PESTAÑA 3: HISTÓRICO Y GRÁFICAS
    # =========================================================================
    with tab_hist:
        st.subheader("Histórico de Visitas y Evolución Clínica")

        visitas_hist = st.session_state.get("visitas_paciente", [])
        if not visitas_hist:
            st.info("No hay visitas registradas todavía para este paciente.")
        else:
            df_visitas = pd.DataFrame(visitas_hist)
            df_visitas["fecha_dt"] = pd.to_datetime(
                df_visitas["fecha_visita"], errors="coerce"
            )
            sort_cols = ["fecha_dt"] + (
                ["created_at"] if "created_at" in df_visitas.columns else []
            )
            df_plot = df_visitas.sort_values(by=sort_cols, ascending=True).copy()
            df_plot["fecha_label"] = (
                df_plot["fecha_dt"]
                .dt.strftime("%Y-%m-%d")
                .fillna(df_plot["fecha_visita"].astype(str))
            )

            # Gráfica interactiva con Plotly: Evolución de Peso y Tensión Arterial (TAS / TAD)
            fig = make_subplots(
                rows=2,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.12,
                subplot_titles=(
                    "Evolución del Peso Corporal (kg)",
                    "Evolución de la Tensión Arterial Sistólica y Diastólica (mmHg)",
                ),
            )

            # Subplot 1: Peso (kg)
            fig.add_trace(
                go.Scatter(
                    x=df_plot["fecha_label"],
                    y=df_plot["peso_kg"],
                    mode="lines+markers+text",
                    name="Peso (kg)",
                    text=[
                        f"{p:.1f}" if pd.notnull(p) else "" for p in df_plot["peso_kg"]
                    ],
                    textposition="top center",
                    line=dict(color="#1f77b4", width=3),
                    marker=dict(size=9),
                    hovertemplate="Fecha: %{x}<br>Peso: %{y:.1f} kg<extra></extra>",
                ),
                row=1,
                col=1,
            )

            # Subplot 2: TAS y TAD en consulta
            fig.add_trace(
                go.Scatter(
                    x=df_plot["fecha_label"],
                    y=df_plot["tas_consulta"],
                    mode="lines+markers",
                    name="TAS Consulta (mmHg)",
                    line=dict(color="#d62728", width=2.5),
                    marker=dict(size=8, symbol="circle"),
                    hovertemplate="Fecha: %{x}<br>TAS: %{y} mmHg<extra></extra>",
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
                    marker=dict(size=8, symbol="diamond"),
                    hovertemplate="Fecha: %{x}<br>TAD: %{y} mmHg<extra></extra>",
                ),
                row=2,
                col=1,
            )

            fig.update_yaxes(title_text="Peso (kg)", row=1, col=1)
            fig.update_yaxes(title_text="TA (mmHg)", row=2, col=1)
            fig.update_xaxes(title_text="Fecha de visita", row=2, col=1)
            fig.update_layout(
                height=580,
                hovermode="x unified",
                legend=dict(
                    orientation="h", yanchor="bottom", y=1.04, xanchor="right", x=1
                ),
                margin=dict(l=40, r=40, t=70, b=40),
            )

            st.plotly_chart(fig, use_container_width=True)

            st.markdown("#### 📋 Tabla Histórica de Visitas")
            columnas_ordenadas = [
                "fecha_visita",
                "clase_funcional_nyha",
                "peso_kg",
                "diferencia_peso_kg",
                "imc",
                "tas_consulta",
                "tad_consulta",
                "fc_consulta",
                "ecg_realizado",
                "aporta_hoja_ta_domicilio",
                "tas_media_domicilio",
                "tad_media_domicilio",
                "incidencias_bool",
                "inc_cardio_muerte",
                "inc_cardio_ingreso_ic",
                "inc_cardio_ingreso_otras",
                "inc_cardio_urgencias_ic",
                "inc_cardio_diureticos_iv",
                "inc_cardio_aumento_diureticos_vo",
                "inc_nocardio_ingreso",
                "inc_nocardio_urgencias",
                "acudio_urgencias",
                "aumento_diureticos_ambulatorio",
                "detalle_incidencias",
                "buen_cumplimiento",
                "observaciones_cumplimiento",
                "medicacion_habitual",
                "actuacion_post_completada",
                "furosemida_iv_bolo",
                "dosis_furosemida_iv_mg",
                "perfusor_furosemida",
                "ferinject_iv",
                "dosis_ferinject_mg",
                "derivacion_hd_levosimendan",
                "analitica_realizada",
                "notas_post_consulta",
            ]
            cols_existentes = [
                c for c in columnas_ordenadas if c in df_visitas.columns
            ]
            st.dataframe(
                df_visitas[cols_existentes],
                use_container_width=True,
                hide_index=True,
            )


if __name__ == "__main__":
    main()
