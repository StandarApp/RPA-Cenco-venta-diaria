"""
RPA - Cencosud Venta Diaria
============================
Flujo:
  1. Login (Chile + Supermercados + Keycloak SSO)
  2. Menú Comercial → Ventas
  3. Setear fecha AYER en ambos campos (Desde / Hasta)
  4. Click Generar Informe
  5. Doble click en celda adyacente a 1974206 → modal Detalle de Producto
  6. Click botón ↓ @ (1075, 192) — con detección de pérdida de conexión y reintento
  7. Click SELECCIONAR en modal Formato de Descarga
  8. Click link Ventas(detalleProducto)*.zip → descarga

FIXES v2 (2026-05-05):
  - step6: detecta banner "Se perdió conexión" ENTRE cada intento de click
  - step6: si Vaadin se cae y se recupera, verifica si el modal sigue abierto;
           si se cerró, retorna False para que run() reintente desde step5
  - step6: agrega _popup_descarga_visible() como helper unificado
  - step_inv4: misma lógica de detección de conexión perdida
  - run(): si step6 retorna False (modal cerrado), reintenta step5+step6 hasta 3 veces

FIXES v3 (2026-05-05):
  - _cerrar_modales_abiertos(): prioriza CANCELAR, espera activa post-click, Escape fallback
  - step_inv1: verifica título del panel (no solo Generar Informe), sube ciclos a 8
  - step_inv6: busca link dentro del modal Descargar Archivo activo, no por href genérico

FIXES v4 (2026-05-05):
  - run(): reemplaza _cerrar_modales_abiertos() por reset completo de sesión Vaadin:
           goto BASE_URL + step2_login() antes de iniciar inventario. Los modales
           que quedan en estado inconsistente tras pérdida de conexión son inmunes
           a clicks en el DOM — la única solución real es destruir la sesión del servidor.
"""

import asyncio
import os
import logging
import zipfile
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import pandas as pd
from supabase import create_client

load_dotenv()

# ─────────────────────────────────────────────
# SUPABASE — cliente y funciones de subida
# ─────────────────────────────────────────────

def _supabase_client():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise ValueError("SUPABASE_URL y SUPABASE_KEY deben estar definidas en las variables de entorno")
    return create_client(url, key)


def _buscar_columna(df, candidatos):
    cols_lower = {c.strip().lower(): c for c in df.columns}
    for candidato in candidatos:
        key = candidato.strip().lower()
        if key in cols_lower:
            return cols_lower[key]
    raise ValueError(
        f"No se encontro ninguna de las columnas {candidatos} "
        f"en el DataFrame. Columnas disponibles: {list(df.columns)}"
    )


def _leer_excel_de_zip(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        nombres = z.namelist()
        log.info(f"  Archivos dentro del ZIP: {nombres}")
        for nombre in nombres:
            ext = Path(nombre).suffix.lower()
            if ext in (".xlsx", ".xls", ".csv"):
                with z.open(nombre) as f:
                    if ext == ".csv":
                        import io
                        raw = f.read()
                        for sep in (";", ",", "\t"):
                            try:
                                df = pd.read_csv(io.BytesIO(raw), sep=sep, thousands=".", decimal=",")
                                if len(df.columns) > 1:
                                    return df
                            except Exception:
                                pass
                    else:
                        return pd.read_excel(f, thousands=".", decimal=",")
        raise ValueError(f"No se encontro ningun archivo Excel/CSV dentro de {zip_path}")


def _limpiar_numero(valor):
    if pd.isna(valor):
        return None
    s = str(valor).strip()
    s = s.replace("$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def subir_inventario(zip_path):
    log.info("=" * 50)
    log.info("Subiendo INVENTARIO a Supabase...")
    log.info("=" * 50)
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    sb = _supabase_client()
    df = _leer_excel_de_zip(zip_path)
    log.info(f"  Filas leidas del Excel: {len(df)}")
    log.info(f"  Columnas: {list(df.columns)}")
    col_local  = _buscar_columna(df, ["Local", "ID Local", "cod_local", "sucursal", "Cod. Local", "Codigo Local", "Cod Local"])
    col_desc   = _buscar_columna(df, ["Nombre Local", "descripcion", "nombre", "Descripcion Local", "Descripcion"])
    col_stock  = _buscar_columna(df, ["stock_un", "stock", "Inv. Actual(Un)", "Inv. Actual (Un)", "Stock (Un)", "Stock(Un)"])
    registros = []
    for _, fila in df.iterrows():
        stock = _limpiar_numero(fila[col_stock])
        if stock is None:
            continue
        registros.append({
            "fecha":             fecha_hoy,
            "cod_local":         str(fila[col_local]).strip(),
            "descripcion_local": str(fila[col_desc]).strip(),
            "stock_un":          stock,
        })
    if not registros:
        log.warning("  No hay registros validos para subir a inventarios_cencosud")
        return
    log.info(f"  Eliminando registros de fecha {fecha_hoy} en inventarios_cencosud...")
    sb.table("inventarios_cencosud").delete().eq("fecha", fecha_hoy).execute()
    log.info(f"  Subiendo {len(registros)} registros a inventarios_cencosud...")
    chunk = 500
    for i in range(0, len(registros), chunk):
        sb.table("inventarios_cencosud").upsert(registros[i:i+chunk]).execute()
        log.info(f"    Batch {i//chunk + 1}: {min(i+chunk, len(registros))}/{len(registros)}")
    log.info(f"Inventario subido: {len(registros)} filas en inventarios_cencosud")


def subir_ventas(zip_path):
    log.info("=" * 50)
    log.info("Subiendo VENTAS a Supabase...")
    log.info("=" * 50)
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    sb = _supabase_client()
    df = _leer_excel_de_zip(zip_path)
    log.info(f"  Filas leidas del Excel: {len(df)}")
    log.info(f"  Columnas: {list(df.columns)}")
    col_local   = _buscar_columna(df, ["Local", "ID Local", "cod_local", "sucursal", "Cod. Local", "Codigo Local", "Cod Local"])
    col_desc    = _buscar_columna(df, ["Nombre Local", "descripcion", "nombre", "Descripcion Local", "Descripcion"])
    col_vta_un  = _buscar_columna(df, ["venta periodo(un)", "unidades", "Venta Periodo(Un)", "Vta. Periodo (Un)", "Vta. Periodo(Un)"])
    col_vta_clp = _buscar_columna(df, ["Ventas", "Monto", "venta periodo publico", "Venta Periodo Publico ($)", "Vta. Periodo Publico ($)"])
    registros = []
    for _, fila in df.iterrows():
        vta_un  = _limpiar_numero(fila[col_vta_un])
        vta_clp = _limpiar_numero(fila[col_vta_clp])
        if vta_un is None and vta_clp is None:
            continue
        registros.append({
            "fecha":                     fecha_hoy,
            "cod_local":                 str(fila[col_local]).strip(),
            "descripcion_local":         str(fila[col_desc]).strip(),
            "venta_periodo_un":          vta_un,
            "venta_periodo_publico_clp": vta_clp,
        })
    if not registros:
        log.warning("  No hay registros validos para subir a ventas_cencosud")
        return
    log.info(f"  Eliminando registros de fecha {fecha_hoy} en ventas_cencosud...")
    sb.table("ventas_cencosud").delete().eq("fecha", fecha_hoy).execute()
    log.info(f"  Subiendo {len(registros)} registros a ventas_cencosud...")
    chunk = 500
    for i in range(0, len(registros), chunk):
        sb.table("ventas_cencosud").upsert(registros[i:i+chunk]).execute()
        log.info(f"    Batch {i//chunk + 1}: {min(i+chunk, len(registros))}/{len(registros)}")
    log.info(f"Ventas subidas: {len(registros)} filas en ventas_cencosud")

BASE_URL     = "https://www.cenconlineb2b.com/"
DOWNLOAD_DIR = Path("downloads")
LOG_DIR      = Path("logs")
DOWNLOAD_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / f"rpa_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("VentaDiaria")


def _es_dashboard(url):
    return (
        "cenconlineb2b.com" in url
        and "ssocencosud" not in url
        and "BBRe-commerce/main" in url
    )


def _fecha_ayer():
    """
    Retorna fecha de ayer en hora de Chile (America/Santiago).
    Si la variable de entorno FORCE_DATE está definida (formato DD-MM-YYYY),
    la usa directamente — útil para pruebas sin esperar que la plataforma
    actualice los datos del día real.
    Ejemplo: FORCE_DATE=04-05-2026
    """
    force = os.getenv("FORCE_DATE", "03-05-2026").strip()  # TEMPORAL: forzar fecha para pruebas
    if force:
        log.info(f"  ⚠️  FORCE_DATE activo: usando '{force}' como fecha de ayer")
        return force
    try:
        import zoneinfo
        tz_chile = zoneinfo.ZoneInfo("America/Santiago")
        ahora_chile = datetime.now(tz_chile)
    except Exception:
        ahora_chile = datetime.utcnow() - timedelta(hours=4)
    ayer = ahora_chile - timedelta(days=1)
    return ayer.strftime("%d-%m-%Y")


class VentaDiariaRPA:
    def __init__(self, username, password, headless=True):
        self.username = username
        self.password = password
        self.headless  = headless
        self.page      = None

    async def _wait(self, min_ms=500, max_ms=1200):
        import random
        await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))

    async def _screenshot(self, name):
        path = LOG_DIR / f"{name}_{datetime.now().strftime('%H%M%S')}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True, timeout=8000)
            log.info(f"Screenshot: {path}")
        except Exception as e:
            log.warning(f"Screenshot omitido ({name}): {type(e).__name__}")

    async def _click_vaadin_real(self, texto):
        coords = await self.page.evaluate(f"""
            () => {{
                const spans = document.querySelectorAll('.v-menubar-menuitem-caption');
                for (const span of spans) {{
                    if (span.textContent.trim() === '{texto}') {{
                        const rect = span.parentElement.getBoundingClientRect();
                        return {{
                            x: rect.left + rect.width / 2,
                            y: rect.top + rect.height / 2,
                            visible: rect.width > 0 && rect.height > 0
                        }};
                    }}
                }}
                return null;
            }}
        """)
        if not coords or not coords.get("visible"):
            return False
        x, y = coords["x"], coords["y"]
        log.info(f"  Vaadin click '{texto}' @ ({x:.0f}, {y:.0f})")
        await self.page.mouse.move(x, y)
        await asyncio.sleep(0.2)
        await self.page.mouse.click(x, y)
        return True

    # ─────────────────────────────────────────────
    # HELPERS DE CONEXIÓN Y DETECCIÓN DE POPUPS
    # ─────────────────────────────────────────────

    async def _esperar_conexion(self, timeout_s=30):
        """Espera hasta que desaparezca el banner de reconexión de Vaadin."""
        for i in range(timeout_s * 2):
            desconectado = await self.page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        const t = el.textContent || '';
                        if (t.includes('perdió conexión') ||
                            t.includes('Reconectando') ||
                            t.includes('Lost connection')) {
                            if (el.offsetParent !== null) return true;
                        }
                    }
                    return false;
                }
            """)
            if not desconectado:
                return True
            log.warning(f"  Conexión perdida — esperando [{i+1}/{timeout_s*2}]...")
            await asyncio.sleep(0.5)
        log.error("  Timeout esperando reconexión")
        return False

    async def _conexion_perdida_ahora(self):
        """Retorna True si el banner de pérdida de conexión está visible en este momento."""
        return await self.page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('*')) {
                    const t = el.textContent || '';
                    if ((t.includes('perdió conexión') ||
                         t.includes('Reconectando') ||
                         t.includes('Lost connection')) &&
                        el.offsetParent !== null) return true;
                }
                return false;
            }
        """)

    async def _popup_descarga_visible(self):
        """
        Retorna True si apareció el modal/popup REAL de descarga.
        Requiere 'Formato de Descarga' o 'Dato Fuente' — NO usa 'SELECCIONAR'
        como detector standalone porque ese texto existe en otros modales.
        """
        return await self.page.evaluate("""
            () => {
                // Modal 'Formato de Descarga' — título específico
                for (const el of document.querySelectorAll('*')) {
                    const t = el.textContent.trim();
                    if (el.offsetParent === null) continue;
                    // Título del modal de formato
                    if (t === 'Formato de Descarga') return true;
                    // Popup con opciones de Dato Fuente
                    if (t.includes('Dato Fuente') && t.length < 80) return true;
                    // Modal Descargar Archivo (aparece después de SELECCIONAR)
                    if (t === 'Descargar Archivo') return true;
                }
                // Popup de menú contextual de Vaadin
                for (const sel of [
                    'td.gwt-MenuItem',
                    '.v-menubar-popup td',
                    '.v-contextmenu td'
                ]) {
                    for (const el of document.querySelectorAll(sel)) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.top > 0 &&
                            el.textContent.trim().length > 2) return true;
                    }
                }
                return false;
            }
        """)

    async def _modal_detalle_visible(self, titulo="Detalle de Producto"):
        """Retorna True si el modal con ese título sigue abierto y visible."""
        return await self.page.evaluate(f"""
            () => {{
                for (const el of document.querySelectorAll('*')) {{
                    if (el.textContent.trim() === '{titulo}' &&
                        el.offsetParent !== null) return true;
                }}
                return false;
            }}
        """)

    # ─────────────────────────────────────────────
    # PASOS VENTAS
    # ─────────────────────────────────────────────

    async def step1_select_pais_y_unidad(self):
        log.info("Paso 1: Chile + Supermercados")
        # En el reset de sesión ya estamos en BASE_URL, así que el goto es opcional
        # pero lo dejamos para el flujo inicial. Si ya estamos en el dashboard, saltar.
        if not _es_dashboard(self.page.url):
            try:
                await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass  # Ignorar timeout del goto — la página puede ya estar cargada
        await self._screenshot("paso1_inicio")
        if _es_dashboard(self.page.url):
            log.info("Sesión activa — saltando")
            return
        pais = await self.page.wait_for_selector("select", timeout=10000)
        await pais.select_option(label="Chile")
        await self._wait(800, 1200)
        await self.page.wait_for_timeout(1500)
        selects = await self.page.query_selector_all("select")
        if len(selects) >= 2:
            await selects[1].select_option(label="Supermercados")
            await self._wait(800, 1200)
        btn = await self.page.wait_for_selector("#btnIngresar", timeout=8000)
        # Usar click con no_wait_after=True para no bloquear esperando la navegación.
        # La redirección SSO puede tardar >30s — step2_login() maneja la espera vía polling.
        try:
            await btn.click(no_wait_after=True, timeout=5000)
        except Exception:
            # Si el click mismo falla, intentar via JS como fallback
            await self.page.evaluate("document.getElementById('btnIngresar').click()")
        log.info(f"Paso 1 OK | URL: {self.page.url}")

    async def step2_login(self):
        log.info("Paso 2: Login")
        if _es_dashboard(self.page.url):
            log.info("✅ Sesión activa")
            return True

        # Esperar hasta 60s a que aparezca #kc-login o el dashboard.
        # El redirect SSO post-Ingresar puede tardar bastante, especialmente
        # en el reset de sesión donde la redirección parte desde BASE_URL.
        kc_listo = False
        for i in range(60):
            await asyncio.sleep(1)
            if _es_dashboard(self.page.url):
                log.info("✅ Sesión activa (detectada durante espera)")
                return True
            try:
                el = await self.page.query_selector("#kc-login")
                if el:
                    kc_listo = True
                    break
            except Exception:
                pass
            log.info(f"  Esperando Keycloak [{i+1}/60] url={self.page.url[:60]}")

        if not kc_listo:
            if _es_dashboard(self.page.url):
                return True
            return False
        await self.page.evaluate("""
            ([u, p]) => {
                const un = document.getElementById('username');
                un.value = u;
                un.dispatchEvent(new Event('input', {bubbles: true}));
                un.dispatchEvent(new Event('change', {bubbles: true}));
                const pw = document.getElementById('password');
                pw.value = p;
                pw.dispatchEvent(new Event('input', {bubbles: true}));
                pw.dispatchEvent(new Event('change', {bubbles: true}));
                document.getElementById('kc-login').click();
            }
        """, [self.username, self.password])
        for i in range(60):
            await asyncio.sleep(2)
            url = self.page.url
            log.info(f"  [{i+1:02d}/60] {url}")
            if _es_dashboard(url):
                break
        else:
            log.error("Timeout login")
            return False
        try:
            await self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        log.info("✅ Login OK")
        return True

    async def step3_navegar_ventas(self):
        log.info("Paso 3: Comercial → Ventas")
        try:
            await self.page.wait_for_selector(".v-menubar-menuitem-caption", timeout=20000)
        except Exception:
            pass
        await self._wait(2000, 3000)

        for ciclo in range(5):
            log.info(f"  Ciclo {ciclo+1}/5")
            ok = await self._click_vaadin_real("Comercial")
            if not ok:
                await asyncio.sleep(3)
                continue
            await asyncio.sleep(1.5)
            ok = await self._click_vaadin_real("Ventas")
            if not ok:
                await asyncio.sleep(3)
                continue
            for espera in range(20):
                await asyncio.sleep(2)
                ok2 = await self.page.evaluate("""
                    () => [...document.querySelectorAll('*')]
                        .some(e => e.children.length===0 &&
                                   e.textContent.trim()==='Generar Informe')
                """)
                log.info(f"    [{espera+1}/20] Generar Informe={ok2}")
                if ok2:
                    log.info("  ✅ Ventas cargado")
                    break
            else:
                continue
            break

        await self._screenshot("paso3_ventas_cargado")
        log.info("Paso 3 completado")

    async def step4_setear_fecha_y_generar(self):
        log.info("Paso 4: Verificar fecha HASTA y setear DESDE")
        fecha_ayer = _fecha_ayer()
        log.info(f"  Fecha esperada (ayer): {fecha_ayer}")
        await self._screenshot("paso4_antes")

        fechas = await self.page.evaluate("""
            () => {
                const campos = [];
                const inputs = document.querySelectorAll(
                    '.v-datefield input, .v-datefield-textfield'
                );
                for (const inp of inputs) {
                    const val = inp.value || inp.textContent || '';
                    const r = inp.getBoundingClientRect();
                    if (r.width > 0 && val.match(/[0-9]{2}-[0-9]{2}-[0-9]{4}/)) {
                        campos.push({
                            val: val.trim(),
                            x: Math.round(r.left + r.width/2),
                            y: Math.round(r.top + r.height/2)
                        });
                    }
                }
                return campos;
            }
        """)
        log.info(f"  Campos v-datefield encontrados: {fechas}")

        if not fechas:
            fechas = await self.page.evaluate("""
                () => {
                    const campos = [];
                    for (const el of document.querySelectorAll('input')) {
                        const val = el.value || '';
                        if (val.match(/[0-9]{2}-[0-9]{2}-[0-9]{4}/)) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0) {
                                campos.push({
                                    val: val.trim(),
                                    x: Math.round(r.left + r.width/2),
                                    y: Math.round(r.top + r.height/2)
                                });
                            }
                        }
                    }
                    return campos;
                }
            """)
            log.info(f"  Campos input fallback: {fechas}")

        fecha_hasta = fechas[1]["val"] if len(fechas) >= 2 else (fechas[0]["val"] if fechas else "")

        log.info(f"  Fecha HASTA leída: '{fecha_hasta}'")
        log.info(f"  Fecha ayer esperada: '{fecha_ayer}'")

        if fecha_hasta != fecha_ayer:
            msg = (f"Fecha no disponible. "
                   f"La plataforma muestra HASTA={fecha_hasta} "
                   f"pero se esperaba {fecha_ayer}. "
                   f"El reporte de ayer aun no esta disponible.")
            log.warning(f"  ⚠️ {msg}")
            aviso = DOWNLOAD_DIR / "fecha_no_disponible.txt"
            aviso.write_text(msg)
            log.info(f"  Archivo creado: {aviso}")
            raise Exception(f"FECHA_NO_DISPONIBLE: {msg}")

        log.info(f"  ✅ Fecha HASTA correcta: {fecha_hasta}")

        # Usar la misma fecha que _fecha_ayer() — respeta FORCE_DATE si está activo
        fecha_ayer_str = _fecha_ayer()  # DD-MM-YYYY
        try:
            ayer_dt = datetime.strptime(fecha_ayer_str, "%d-%m-%Y")
        except Exception:
            try:
                import zoneinfo
                tz_chile = zoneinfo.ZoneInfo("America/Santiago")
                ayer_dt = datetime.now(tz_chile) - timedelta(days=1)
            except Exception:
                ayer_dt = datetime.utcnow() - timedelta(hours=4) - timedelta(days=1)
        dia_ayer  = ayer_dt.day
        mes_ayer  = ayer_dt.month
        anio_ayer = ayer_dt.year
        log.info(f"  Abriendo calendario DESDE para seleccionar día {dia_ayer}/{mes_ayer}/{anio_ayer}")

        icono_cal = await self.page.evaluate("""
            () => {
                const campos = document.querySelectorAll('.v-datefield');
                if (campos.length === 0) return null;
                const primero = campos[0];
                const btn = primero.querySelector(
                    'button, .v-datefield-button, [class*="calendar"], [class*="button"]'
                );
                if (!btn) return null;
                const r = btn.getBoundingClientRect();
                if (r.width > 0)
                    return {x: Math.round(r.left + r.width/2),
                            y: Math.round(r.top + r.height/2)};
                return null;
            }
        """)
        if not icono_cal:
            icono_cal = {"x": 93, "y": 514}
            log.warning(f"  Ícono calendario no encontrado — coord fija {icono_cal}")

        log.info(f"  Click ícono calendario DESDE @ ({icono_cal['x']}, {icono_cal['y']})")
        await self.page.mouse.move(icono_cal["x"], icono_cal["y"])
        await asyncio.sleep(0.3)
        await self.page.mouse.click(icono_cal["x"], icono_cal["y"])
        await asyncio.sleep(1.0)
        await self._screenshot("paso4_calendario_abierto")

        dia_clickeado = await self.page.evaluate(f"""
            () => {{
                const dia = {dia_ayer};
                const selectores = [
                    '.v-datefield-calendarpanel td',
                    '.v-datefield-calendarpanel span',
                    '.v-overlay-container td',
                    '.v-overlay-container span',
                    '.v-popup td',
                    '.v-popup span',
                ];
                for (const sel of selectores) {{
                    for (const el of document.querySelectorAll(sel)) {{
                        if (el.textContent.trim() === String(dia)) {{
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0 && r.top > 0) {{
                                return {{
                                    x: Math.round(r.left + r.width/2),
                                    y: Math.round(r.top + r.height/2),
                                    text: el.textContent.trim(),
                                    cls: el.className
                                }};
                            }}
                        }}
                    }}
                }}
                return null;
            }}
        """)

        if dia_clickeado:
            log.info(f"  Día {dia_ayer} encontrado @ ({dia_clickeado['x']}, {dia_clickeado['y']})")
            await self.page.mouse.move(dia_clickeado["x"], dia_clickeado["y"])
            await asyncio.sleep(0.2)
            await self.page.mouse.click(dia_clickeado["x"], dia_clickeado["y"])
            await asyncio.sleep(0.5)
            log.info(f"  ✅ DESDE seteado a día {dia_ayer} via calendario")
        else:
            log.warning(f"  ⚠️ Día {dia_ayer} no encontrado en calendario — DESDE sin cambios")

        await self._screenshot("paso4_fecha_seteada_calendario")
        await self._screenshot("paso4_fecha_seteada")
        await asyncio.sleep(1)

        for _ in range(10):
            await asyncio.sleep(1)
            coords = await self.page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        if (el.children.length===0 &&
                            el.textContent.trim()==='Generar Informe') {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0)
                                return {x: Math.round(r.left+r.width/2),
                                        y: Math.round(r.top+r.height/2)};
                        }
                    }
                    return null;
                }
            """)
            if coords:
                log.info(f"  Generar Informe @ ({coords['x']}, {coords['y']})")
                await self.page.mouse.move(coords["x"], coords["y"])
                await asyncio.sleep(0.3)
                await self.page.mouse.click(coords["x"], coords["y"])
                break

        await asyncio.sleep(5)
        await self._screenshot("paso4_informe_generado")
        log.info("Paso 4 completado")

    async def step5_dobleclick_1974206(self):
        log.info("Paso 5: Doble click en 1974206")
        await self._wait(1000, 2000)

        # Si hubo pérdida de conexión al generar el informe, esperar reconexión
        # antes de buscar la celda — si no, la tabla está vacía y falla
        await self._esperar_conexion()

        JS_CELDA = """
            () => {
                for (const sel of ['td','span','div','a']) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.textContent.trim()==='1974206') {
                            const r = el.getBoundingClientRect();
                            if (r.width>0 && r.left>0 && r.top>0) {
                                const sig = el.nextElementSibling;
                                const sr = sig ? sig.getBoundingClientRect() : null;
                                return {
                                    x: sr ? Math.round(sr.left+sr.width/2) : Math.round(r.right+80),
                                    y: Math.round(r.top+r.height/2)
                                };
                            }
                        }
                    }
                }
                return null;
            }
        """

        # Esperar hasta 30s a que la celda 1974206 aparezca en la tabla
        # (puede tardar si Vaadin acaba de reconectar y está re-renderizando)
        coords = None
        for espera in range(15):
            coords = await self.page.evaluate(JS_CELDA)
            if coords:
                break
            log.info(f"  Esperando celda 1974206 [{espera+1}/15]...")
            await asyncio.sleep(2)

        if not coords:
            raise Exception("Celda 1974206 no encontrada")
        log.info(f"  Celda adyacente @ ({coords['x']}, {coords['y']})")

        modal_listo = False
        for intento in range(6):
            log.info(f"  dblclick [{intento+1}/6]")
            await self.page.mouse.move(coords["x"], coords["y"])
            await asyncio.sleep(0.2)
            await self.page.mouse.dblclick(coords["x"], coords["y"], delay=100)
            await self._screenshot(f"paso5_dblclick_{intento+1}")
            log.info("  Esperando 12s...")
            await asyncio.sleep(12)
            estado = await self.page.evaluate("""
                () => {
                    const n = document.querySelectorAll('.v-grid-body .v-grid-cell').length;
                    let modal = false;
                    for (const el of document.querySelectorAll('*')) {
                        if (el.textContent.trim()==='Detalle de Producto' &&
                            el.offsetParent !== null) { modal = true; break; }
                    }
                    return {n: n, ok: n > 50 || modal};
                }
            """)
            log.info(f"  Celdas: {estado['n']} — {'✅' if estado['ok'] else '❌'}")
            if estado["ok"]:
                modal_listo = True
                break
            coords = await self.page.evaluate(JS_CELDA) or coords

        if not modal_listo:
            log.warning("  ⚠️ Modal no detectado")

        await self._screenshot("paso5_modal_abierto")
        log.info("Paso 5 completado")
        return modal_listo

    async def step6_click_boton_descarga(self):
        """
        Click en botón ↓ del modal Detalle de Producto.

        FIX v2:
        - Llama _esperar_conexion() ANTES de cada intento.
        - Después de cada click, detecta si Vaadin perdió conexión.
        - Si se pierde conexión, espera reconexión y verifica si el modal sigue abierto.
        - Si el modal se cerró tras la reconexión, retorna False para que run()
          pueda reintentar desde step5.
        - Retorna True si el popup de descarga quedó abierto, False si no.
        """
        log.info("Paso 6: Click en botón ↓ del modal")

        # Esperar que la conexión esté estable antes de empezar
        await self._esperar_conexion()
        await self._screenshot("paso6_antes")

        candidatos = [
            (1075, 192), (1064, 180), (1070, 185), (1070, 192),
            (1080, 192), (1060, 192), (1075, 185), (1075, 180),
        ]

        for x, y in candidatos:
            # ── Verificar conexión antes de cada intento ──────────────────
            if await self._conexion_perdida_ahora():
                log.warning(f"  ⚠️ Conexión perdida antes de probar ({x}, {y}) — esperando...")
                recuperado = await self._esperar_conexion()
                if not recuperado:
                    log.error("  No se recuperó la conexión — abortando paso 6")
                    return False
                await asyncio.sleep(1.5)  # dar tiempo a Vaadin para re-renderizar

                # Si el modal se cerró durante la reconexión no tiene sentido
                # seguir con las coords — hay que reintentar desde step5
                if not await self._modal_detalle_visible("Detalle de Producto"):
                    log.warning("  Modal se cerró tras reconexión — se necesita reintentar step5")
                    return False

            # ── Intento 1: click normal de mouse ──────────────────────────
            log.info(f"  Click normal ({x}, {y})...")
            await self.page.mouse.move(x, y)
            await asyncio.sleep(0.2)
            await self.page.mouse.click(x, y)
            await asyncio.sleep(1.2)

            if await self._popup_descarga_visible():
                log.info(f"  ✅ Popup abierto @ ({x}, {y}) — click normal")
                await self._screenshot("paso6_post_click")
                log.info("Paso 6 completado")
                return True

            # ── Detectar pérdida de conexión post-click ───────────────────
            if await self._conexion_perdida_ahora():
                log.warning(f"  ⚠️ Conexión perdida tras click ({x}, {y}) — esperando reconexión...")
                await self._screenshot(f"paso6_conexion_perdida_{x}_{y}")
                recuperado = await self._esperar_conexion()
                await asyncio.sleep(2.0)  # Vaadin necesita tiempo para re-renderizar

                if not recuperado:
                    log.error("  Timeout en reconexión — abortando paso 6")
                    return False

                if not await self._modal_detalle_visible("Detalle de Producto"):
                    log.warning("  Modal cerrado tras reconexión — reintentar step5")
                    return False

                # Conexión recuperada y modal sigue abierto → continuar con JS dispatch
                log.info("  Conexión recuperada y modal sigue abierto — probando JS dispatch...")

            # ── Intento 2: JS dispatch (mousedown + mouseup + click) ──────
            log.info(f"  JS dispatch ({x}, {y})...")
            await self.page.evaluate(f"""
                () => {{
                    for (const el of document.elementsFromPoint({x}, {y})) {{
                        ['mousedown','mouseup','click'].forEach(ev =>
                            el.dispatchEvent(new MouseEvent(ev,
                                {{bubbles:true, cancelable:true, clientX:{x}, clientY:{y}}}))
                        );
                    }}
                }}
            """)
            await asyncio.sleep(1.2)

            if await self._popup_descarga_visible():
                log.info(f"  ✅ Popup abierto @ ({x}, {y}) — JS dispatch")
                await self._screenshot("paso6_post_click")
                log.info("Paso 6 completado")
                return True

            # ── Detectar pérdida de conexión post JS dispatch ─────────────
            if await self._conexion_perdida_ahora():
                log.warning(f"  ⚠️ Conexión perdida tras JS dispatch ({x}, {y}) — esperando...")
                await self._screenshot(f"paso6_conexion_perdida_js_{x}_{y}")
                recuperado = await self._esperar_conexion()
                await asyncio.sleep(2.0)

                if not recuperado:
                    log.error("  Timeout en reconexión — abortando paso 6")
                    return False

                if not await self._modal_detalle_visible("Detalle de Producto"):
                    log.warning("  Modal cerrado tras reconexión — reintentar step5")
                    return False

            log.info(f"  ({x},{y}) sin resultado")

        # Agotamos todos los candidatos sin éxito
        await self._screenshot("paso6_post_click")
        log.warning("  ⚠️ Ninguna coordenada abrió el popup de descarga")
        log.info("Paso 6 completado (sin éxito confirmado)")
        return False

    async def step7_seleccionar_formato(self):
        log.info("Paso 7: Click SELECCIONAR")
        for i in range(16):
            await asyncio.sleep(0.5)
            visible = await self.page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        if (el.textContent.trim().includes('Formato de Descarga') &&
                            el.offsetParent !== null) return true;
                    }
                    return false;
                }
            """)
            if visible:
                log.info(f"  Modal visible [{i+1}]")
                break
        await self._screenshot("paso7_modal_formato")

        coords = await self.page.evaluate("""
            () => {
                for (const el of document.querySelectorAll(
                    'button, .v-button, .gwt-Button, span, div'
                )) {
                    if (el.textContent.trim().toUpperCase() === 'SELECCIONAR') {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && r.left > 0)
                            return {x: Math.round(r.left+r.width/2),
                                    y: Math.round(r.top+r.height/2),
                                    tag: el.tagName};
                    }
                }
                return null;
            }
        """)
        if coords:
            log.info(f"  SELECCIONAR @ ({coords['x']}, {coords['y']})")
            await self.page.mouse.move(coords["x"], coords["y"])
            await asyncio.sleep(0.2)
            await self.page.mouse.click(coords["x"], coords["y"])
        else:
            log.warning("  SELECCIONAR no encontrado — coord fija (575, 449)")
            await self.page.mouse.move(575, 449)
            await asyncio.sleep(0.2)
            await self.page.mouse.click(575, 449)
        log.info("Paso 7 completado")

    async def step8_descargar_archivo(self):
        log.info("Paso 8: Click en link de descarga")
        link_el = None
        for espera in range(20):
            await asyncio.sleep(0.5)
            link_el = await self.page.query_selector(
                "a[href*='Ventas'], a[href*='ventas'], a[href*='.zip'], a[href*='.xls']"
            )
            if link_el and await link_el.is_visible():
                texto = (await link_el.inner_text()).strip()
                log.info(f"  [{espera+1}] Link: '{texto}'")
                break
            log.info(f"  [{espera+1}] Esperando link...")
        await self._screenshot("paso8_modal_descarga")
        if not link_el:
            raise Exception("Link de descarga no encontrado")
        try:
            async with self.page.expect_download(timeout=60000) as dl_info:
                await link_el.click()
            download = await dl_info.value
            dest = DOWNLOAD_DIR / download.suggested_filename
            await download.save_as(str(dest))
            log.info(f"  ✅ Descargado: {dest}")
            await self._screenshot("paso8_descarga_ok")
            return str(dest)
        except Exception as e:
            log.error(f"  Error descarga: {e}")
            await self._screenshot("error_descarga")
            return None

    # ─────────────────────────────────────────────
    # PASOS INVENTARIO (encadenado tras ventas)
    # ─────────────────────────────────────────────

    async def _cerrar_modales_abiertos(self):
        """
        Cierra todos los modales/popups de Vaadin que estén abiertos.

        FIX v3:
        - Prioriza CANCELAR sobre la X — es el botón diseñado para cerrar
          el modal "Formato de Descarga" limpiamente sin disparar acciones.
        - Después de cada click, espera activamente (hasta 2s) a que ese
          modal desaparezca del DOM antes de buscar el siguiente.
        - Verifica al final que no quede ningún modal visible.
        - Usa Escape como fallback final si quedan modales resistentes.
        """
        log.info("  Cerrando modales abiertos...")
        cerrado_alguno = False

        JS_HAY_MODAL = """
            () => {
                for (const sel of [
                    '.v-window', '.v-window-wrap', '.v-dialog',
                    '.v-overlay-container .v-window'
                ]) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.offsetParent !== null) return true;
                    }
                }
                return false;
            }
        """

        JS_BUSCAR_BOTON_CIERRE = """
            () => {
                // 1. CANCELAR primero — cierre limpio de modales de formato/descarga
                for (const el of document.querySelectorAll(
                    'button, .v-button, .gwt-Button, span, div'
                )) {
                    const t = el.textContent.trim().toUpperCase();
                    if (t === 'CANCELAR' && el.offsetParent !== null) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0)
                            return {x: Math.round(r.left+r.width/2),
                                    y: Math.round(r.top+r.height/2),
                                    tipo: 'CANCELAR'};
                    }
                }
                // 2. v-window-closebox (X estándar de ventanas Vaadin)
                for (const sel of [
                    '.v-window-closebox',
                    '[class*="closebox"]'
                ]) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.offsetParent !== null) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0)
                                return {x: Math.round(r.left+r.width/2),
                                        y: Math.round(r.top+r.height/2),
                                        tipo: 'closebox'};
                        }
                    }
                }
                // 3. Texto "X" literal en botón pequeño de modal
                for (const el of document.querySelectorAll('button, span, div')) {
                    if (el.textContent.trim() === 'X' && el.offsetParent !== null) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.width < 60 && r.top > 0)
                            return {x: Math.round(r.left+r.width/2),
                                    y: Math.round(r.top+r.height/2),
                                    tipo: 'X-texto'};
                    }
                }
                return null;
            }
        """

        for intento in range(8):
            hay_modal = await self.page.evaluate(JS_HAY_MODAL)
            if not hay_modal:
                break

            boton = await self.page.evaluate(JS_BUSCAR_BOTON_CIERRE)
            if not boton:
                # No encontramos botón pero hay modal — intentar Escape
                log.warning(f"  Modal visible pero sin botón cierre — Escape [{intento+1}]")
                await self.page.keyboard.press("Escape")
            else:
                log.info(f"  Click '{boton['tipo']}' @ ({boton['x']}, {boton['y']})")
                await self.page.mouse.move(boton["x"], boton["y"])
                await asyncio.sleep(0.3)
                await self.page.mouse.click(boton["x"], boton["y"])

            cerrado_alguno = True

            # Esperar activamente a que este modal desaparezca (hasta 2s)
            for t in range(4):
                await asyncio.sleep(0.5)
                if not await self.page.evaluate(JS_HAY_MODAL):
                    break

        # Verificación final
        queda_modal = await self.page.evaluate(JS_HAY_MODAL)
        if queda_modal:
            log.warning("  ⚠️ Aún hay modales visibles tras cierre — continuando igual")
        elif cerrado_alguno:
            log.info("  ✅ Modales cerrados")
            await asyncio.sleep(1.0)  # dar tiempo a Vaadin para re-renderizar
        else:
            log.info("  No había modales abiertos")

    async def step_inv1_navegar_inventario(self):
        """
        Abastecimiento → Detalle de Inventario.

        FIX v2:
        - Espera hasta 3s a que el submenú de Abastecimiento sea visible antes de
          intentar clickear "Detalle de Inventario" (antes fallaba silenciosamente
          porque el submenú no había aparecido todavía).
        - Loguea explícitamente si "Detalle de Inventario" no fue encontrado para
          distinguirlo de "Abastecimiento no encontrado".
        - Sube ciclos máximos a 8 para mayor resiliencia.
        """
        log.info("INV Paso 1: Abastecimiento → Detalle de Inventario")
        await self._wait(2000, 3000)

        for ciclo in range(8):
            log.info(f"  Ciclo {ciclo+1}/8")
            ok = await self._click_vaadin_real("Abastecimiento")
            if not ok:
                log.info("  'Abastecimiento' no encontrado — reintentando en 3s")
                await asyncio.sleep(3)
                continue

            # Esperar a que el submenú de Abastecimiento sea visible
            submenu_visible = False
            for t in range(6):  # hasta 3s
                await asyncio.sleep(0.5)
                submenu_visible = await self.page.evaluate("""
                    () => {
                        for (const el of document.querySelectorAll(
                            '.v-menubar-menuitem-caption, .v-menubar-popup *'
                        )) {
                            if (el.textContent.trim() === 'Detalle de Inventario') {
                                const r = el.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) return true;
                            }
                        }
                        return false;
                    }
                """)
                if submenu_visible:
                    break
            if not submenu_visible:
                log.info(f"  Submenú 'Detalle de Inventario' no visible tras 3s — reintentando")
                await asyncio.sleep(2)
                continue

            ok = await self._click_vaadin_real("Detalle de Inventario")
            if not ok:
                log.info("  'Detalle de Inventario' no clickeable — reintentando en 3s")
                await asyncio.sleep(3)
                continue

            for espera in range(20):
                await asyncio.sleep(2)
                # Verificar que el panel activo sea "Detalle de Inventario"
                # No basta con encontrar "Generar Informe" — puede estar en el panel de ventas
                ok2 = await self.page.evaluate("""
                    () => {
                        // Buscar indicadores específicos del panel Detalle de Inventario
                        const textos = ['Detalle de Inventario', 'Inventario'];
                        for (const t of textos) {
                            for (const el of document.querySelectorAll(
                                '.v-panel-caption, .v-label, h2, h3, .v-slot > .v-label'
                            )) {
                                if (el.textContent.trim().includes(t) &&
                                    el.offsetParent !== null) {
                                    // Además debe existir Generar Informe visible
                                    const tieneBoton = [...document.querySelectorAll('*')]
                                        .some(e => e.children.length===0 &&
                                                   e.textContent.trim()==='Generar Informe' &&
                                                   e.offsetParent !== null);
                                    return tieneBoton;
                                }
                            }
                        }
                        return false;
                    }
                """)
                log.info(f"    [{espera+1}/20] Panel Inventario+GenInforme={ok2}")
                if ok2:
                    log.info("  ✅ Detalle de Inventario cargado")
                    break
            else:
                continue
            break

        await self._screenshot("inv_paso1_inventario_cargado")
        log.info("INV Paso 1 completado")

    async def step_inv2_generar_informe(self):
        """Click en Generar Informe del panel de inventario."""
        log.info("INV Paso 2: Generar Informe")
        for _ in range(10):
            await asyncio.sleep(1)
            coords = await self.page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        if (el.children.length===0 &&
                            el.textContent.trim()==='Generar Informe') {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0)
                                return {x: Math.round(r.left+r.width/2),
                                        y: Math.round(r.top+r.height/2)};
                        }
                    }
                    return null;
                }
            """)
            if coords:
                log.info(f"  Generar Informe @ ({coords['x']}, {coords['y']})")
                await self.page.mouse.move(coords["x"], coords["y"])
                await asyncio.sleep(0.3)
                await self.page.mouse.click(coords["x"], coords["y"])
                break

        for espera in range(30):
            await asyncio.sleep(2)
            tiene = await self.page.evaluate("""
                () => [...document.querySelectorAll('*')]
                    .some(e => e.textContent.trim()==='1974206')
            """)
            log.info(f"  [{espera+1}/30] 1974206 visible={tiene}")
            if tiene:
                log.info("  ✅ Tabla con 1974206 cargada")
                break

        await self._screenshot("inv_paso2_informe_generado")
        log.info("INV Paso 2 completado")

    async def step_inv3_dobleclick_1974206(self):
        """Doble click en celda adyacente a 1974206 → modal locales."""
        log.info("INV Paso 3: Doble click en 1974206")
        await self._wait(1000, 2000)

        JS_CELDA = """
            () => {
                for (const sel of ['td','span','div','a']) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.textContent.trim()==='1974206') {
                            const r = el.getBoundingClientRect();
                            if (r.width>0 && r.left>0 && r.top>0) {
                                const sig = el.nextElementSibling;
                                const sr = sig ? sig.getBoundingClientRect() : null;
                                return {
                                    x: sr ? Math.round(sr.left+sr.width/2) : Math.round(r.right+80),
                                    y: Math.round(r.top+r.height/2)
                                };
                            }
                        }
                    }
                }
                return null;
            }
        """

        JS_MODAL_OK = """
            () => {
                const n = document.querySelectorAll('.v-grid-body .v-grid-cell').length;
                let modal = false;
                for (const el of document.querySelectorAll('*')) {
                    if (el.textContent.trim()==='Detalle de Inventario' &&
                        el.offsetParent !== null) { modal = true; break; }
                }
                return {n: n, ok: n > 20 || modal};
            }
        """

        # Esperar hasta 30s a que la celda aparezca (puede tardar tras reconexión)
        coords = None
        for espera in range(15):
            coords = await self.page.evaluate(JS_CELDA)
            if coords:
                break
            log.info(f"  Esperando celda 1974206 en inventario [{espera+1}/15]...")
            await asyncio.sleep(2)

        if not coords:
            raise Exception("Celda 1974206 no encontrada en inventario")
        log.info(f"  Celda adyacente @ ({coords['x']}, {coords['y']})")

        modal_listo = False
        for intento in range(6):
            log.info(f"  dblclick [{intento+1}/6]")
            await self.page.mouse.move(coords["x"], coords["y"])
            await asyncio.sleep(0.2)
            await self.page.mouse.dblclick(coords["x"], coords["y"], delay=100)
            await self._screenshot(f"inv_paso3_dblclick_{intento+1}")
            log.info("  Esperando 12s...")
            await asyncio.sleep(12)
            estado = await self.page.evaluate(JS_MODAL_OK)
            log.info(f"  Celdas: {estado['n']} modal={estado['ok']}")
            if estado["ok"]:
                modal_listo = True
                log.info("  ✅ Modal inventario cargado")
                break
            coords = await self.page.evaluate(JS_CELDA) or coords

        if not modal_listo:
            log.warning("  ⚠️ Modal no detectado — continuando igual")

        await self._screenshot("inv_paso3_modal_abierto")
        log.info("INV Paso 3 completado")
        return modal_listo

    async def step_inv4_click_boton_descarga(self):
        """
        Click en botón ↓ del modal inventario.

        FIX v2: misma lógica que step6 — detecta pérdida de conexión entre
        intentos y retorna False si el modal se cierra, para que run() pueda
        reintentar desde step_inv3.
        """
        log.info("INV Paso 4: Click botón ↓ del modal inventario")

        await self._esperar_conexion()
        await self._screenshot("inv_paso4_antes")

        # Primero intentar localizar el botón ↓ en el DOM del modal actual
        # (más robusto que coordenadas fijas, ya que el modal de inventario
        #  puede tener un tamaño/posición diferente al de ventas)
        coords_boton = await self.page.evaluate("""
            () => {
                // Buscar dentro del modal Detalle de Inventario
                for (const el of document.querySelectorAll('*')) {
                    if (el.textContent.trim() === 'Detalle de Inventario' &&
                        el.offsetParent !== null) {
                        let contenedor = el;
                        for (let i = 0; i < 15; i++) {
                            contenedor = contenedor.parentElement;
                            if (!contenedor) break;
                            // Botón ↓ azul: v-button con ícono de descarga
                            for (const sel of [
                                '.v-button.toolbar-button',
                                '.v-button[class*="download"]',
                                '.v-button[class*="export"]',
                                '[class*="bbr-popupbutton"]'
                            ]) {
                                for (const btn of contenedor.querySelectorAll(sel)) {
                                    const r = btn.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0 &&
                                        r.width < 50 && r.top > 50)
                                        return {x: Math.round(r.left+r.width/2),
                                                y: Math.round(r.top+r.height/2),
                                                fuente: 'DOM'};
                                }
                            }
                            // Cualquier botón pequeño (≤40px) en la esquina superior
                            // del modal que esté a la derecha (x > 900)
                            for (const btn of contenedor.querySelectorAll(
                                '.v-button, button'
                            )) {
                                const r = btn.getBoundingClientRect();
                                if (r.width > 0 && r.width <= 40 &&
                                    r.left > 900 && r.top > 50 && r.top < 250)
                                    return {x: Math.round(r.left+r.width/2),
                                            y: Math.round(r.top+r.height/2),
                                            fuente: 'DOM-esquina'};
                            }
                        }
                    }
                }
                return null;
            }
        """)

        if coords_boton:
            log.info(f"  Botón ↓ encontrado en DOM @ ({coords_boton['x']}, {coords_boton['y']}) [{coords_boton['fuente']}]")
            candidatos_dom = [(coords_boton['x'], coords_boton['y'])]
        else:
            log.warning("  Botón ↓ no encontrado en DOM — usando candidatos por coordenada")
            candidatos_dom = []

        # Coordenadas fijas: primero las del modal de inventario (más angosto),
        # luego las del modal de ventas como fallback
        candidatos_fijos = [
            (1117, 157),  # esquina sup-der del modal inventario (confirmado en screenshot)
            (1064, 180), (1075, 192), (1070, 185), (1070, 192),
            (1080, 192), (1060, 192), (1075, 185), (1075, 180),
        ]
        candidatos = candidatos_dom + candidatos_fijos

        for x, y in candidatos:
            # ── Verificar conexión antes de cada intento ──────────────────
            if await self._conexion_perdida_ahora():
                log.warning(f"  ⚠️ Conexión perdida antes de probar ({x}, {y}) — esperando...")
                recuperado = await self._esperar_conexion()
                if not recuperado:
                    return False
                await asyncio.sleep(1.5)
                if not await self._modal_detalle_visible("Detalle de Inventario"):
                    log.warning("  Modal inventario cerrado tras reconexión — reintentar inv3")
                    return False

            # ── Intento 1: click normal ───────────────────────────────────
            log.info(f"  Probando ({x}, {y})...")
            await self.page.mouse.move(x, y)
            await asyncio.sleep(0.2)
            await self.page.mouse.click(x, y)
            await asyncio.sleep(1.5)

            if await self._popup_descarga_visible():
                log.info(f"  ✅ Popup abierto @ ({x}, {y}) — click normal")
                await self._screenshot("inv_paso4_post_click")
                log.info("INV Paso 4 completado")
                return True

            # ── Detectar pérdida de conexión post-click ───────────────────
            if await self._conexion_perdida_ahora():
                log.warning(f"  ⚠️ Conexión perdida tras click ({x}, {y}) — esperando...")
                await self._screenshot(f"inv_paso4_conexion_perdida_{x}_{y}")
                recuperado = await self._esperar_conexion()
                await asyncio.sleep(2.0)
                if not recuperado:
                    return False
                if not await self._modal_detalle_visible("Detalle de Inventario"):
                    log.warning("  Modal cerrado — reintentar inv3")
                    return False

            # ── Intento 2: JS dispatch ────────────────────────────────────
            log.info(f"  JS dispatch ({x}, {y})...")
            await self.page.evaluate(f"""
                () => {{
                    for (const el of document.elementsFromPoint({x}, {y})) {{
                        ['mousedown','mouseup','click'].forEach(ev =>
                            el.dispatchEvent(new MouseEvent(ev,
                                {{bubbles:true, cancelable:true, clientX:{x}, clientY:{y}}}))
                        );
                    }}
                }}
            """)
            await asyncio.sleep(1.5)

            if await self._popup_descarga_visible():
                log.info(f"  ✅ Popup abierto @ ({x}, {y}) — JS dispatch")
                await self._screenshot("inv_paso4_post_click")
                log.info("INV Paso 4 completado")
                return True

            if await self._conexion_perdida_ahora():
                log.warning(f"  ⚠️ Conexión perdida tras JS dispatch ({x}, {y}) — esperando...")
                recuperado = await self._esperar_conexion()
                await asyncio.sleep(2.0)
                if not recuperado:
                    return False
                if not await self._modal_detalle_visible("Detalle de Inventario"):
                    log.warning("  Modal cerrado — reintentar inv3")
                    return False

            log.info(f"  ({x},{y}) sin resultado")

        await self._screenshot("inv_paso4_post_click")
        log.warning("  ⚠️ Ninguna coordenada abrió el popup de descarga (inventario)")
        log.info("INV Paso 4 completado (sin éxito confirmado)")
        return False

    async def step_inv5_seleccionar_dato_fuente(self):
        """
        Seleccionar 'Descargar Dato Fuente Período' del popup o
        click SELECCIONAR si apareció Formato de Descarga directamente.
        """
        log.info("INV Paso 5: Seleccionar Dato Fuente / Formato")
        await asyncio.sleep(1)

        estado = await self.page.evaluate("""
            () => {
                for (const sel of ['td.gwt-MenuItem','.v-menubar-popup td','.v-contextmenu td']) {
                    for (const el of document.querySelectorAll(sel)) {
                        const r = el.getBoundingClientRect();
                        const t = el.textContent.trim();
                        if (r.width > 0 && r.top > 0 && t.length > 2)
                            return {tipo: 'popup', text: t,
                                    x: Math.round(r.left+r.width/2),
                                    y: Math.round(r.top+r.height/2)};
                    }
                }
                for (const el of document.querySelectorAll('*')) {
                    if (el.textContent.trim().includes('Formato de Descarga') &&
                        el.offsetParent !== null) return {tipo: 'formato'};
                }
                return {tipo: 'ninguno'};
            }
        """)
        log.info(f"  Estado: {estado}")

        if estado['tipo'] == 'popup':
            item = await self.page.evaluate("""
                () => {
                    for (const sel of ['td.gwt-MenuItem','.v-menubar-popup td','.v-contextmenu td']) {
                        for (const el of document.querySelectorAll(sel)) {
                            const t = el.textContent.trim();
                            if (t.includes('Dato') || t.includes('Fuente')) {
                                const r = el.getBoundingClientRect();
                                if (r.width > 0 && r.top > 0)
                                    return {x: Math.round(r.left+r.width/2),
                                            y: Math.round(r.top+r.height/2), text: t};
                            }
                        }
                    }
                    const items = [];
                    for (const sel of ['td.gwt-MenuItem','.v-menubar-popup td']) {
                        for (const el of document.querySelectorAll(sel)) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.top > 0)
                                items.push({x: Math.round(r.left+r.width/2),
                                            y: Math.round(r.top+r.height/2),
                                            text: el.textContent.trim()});
                        }
                        if (items.length > 0) break;
                    }
                    return items[1] || items[0] || null;
                }
            """)
            if item:
                log.info(f"  Click popup '{item['text']}' @ ({item['x']}, {item['y']})")
                await self.page.mouse.move(item["x"], item["y"])
                await asyncio.sleep(0.3)
                await self.page.mouse.click(item["x"], item["y"])
            else:
                log.warning("  Item popup no encontrado — coord fija (1253, 209)")
                await self.page.mouse.move(1253, 209)
                await asyncio.sleep(0.3)
                await self.page.mouse.click(1253, 209)

        elif estado['tipo'] == 'formato':
            coords = await self.page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll(
                        'button, .v-button, .gwt-Button, span, div'
                    )) {
                        if (el.textContent.trim().toUpperCase() === 'SELECCIONAR') {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.left > 0)
                                return {x: Math.round(r.left+r.width/2),
                                        y: Math.round(r.top+r.height/2)};
                        }
                    }
                    return null;
                }
            """)
            if coords:
                log.info(f"  SELECCIONAR @ ({coords['x']}, {coords['y']})")
                await self.page.mouse.move(coords["x"], coords["y"])
                await asyncio.sleep(0.2)
                await self.page.mouse.click(coords["x"], coords["y"])
            else:
                log.warning("  SELECCIONAR no encontrado — coord fija (575, 449)")
                await self.page.mouse.move(575, 449)
                await asyncio.sleep(0.2)
                await self.page.mouse.click(575, 449)
        else:
            log.warning("  Ni popup ni formato detectado")

        await asyncio.sleep(2)
        await self._screenshot("inv_paso5_post_seleccion")
        log.info("INV Paso 5 completado")

    async def step_inv6_descargar_archivo(self):
        """
        Click en link de descarga del inventario.

        FIX v2:
        El selector genérico `a[href*='.zip']` capturaba el link de Ventas que
        quedaba visible en el modal anterior, descargando el archivo equivocado.

        Estrategia corregida:
        1. Buscar el link dentro del modal "Descargar Archivo" que esté activo
           (el más reciente / el que tiene el modal padre visible).
        2. Si hay varios links .zip visibles, tomar el que está dentro del modal
           más al frente (z-index más alto o el último en el DOM).
        3. Fallback: el link visible más reciente en el DOM.
        """
        log.info("INV Paso 6: Click en link de descarga inventario")

        link_el = None
        for espera in range(20):
            await asyncio.sleep(0.5)

            # Buscar el link dentro del modal "Descargar Archivo" activo
            # Prioridad: link dentro de un modal con título "Descargar Archivo"
            link_info = await self.page.evaluate("""
                () => {
                    // Buscar todos los modales "Descargar Archivo" visibles
                    const modales = [];
                    for (const el of document.querySelectorAll('*')) {
                        if (el.textContent.trim() === 'Descargar Archivo' &&
                            el.offsetParent !== null) {
                            // Subir hasta encontrar el contenedor del modal
                            let contenedor = el;
                            for (let i = 0; i < 10; i++) {
                                contenedor = contenedor.parentElement;
                                if (!contenedor) break;
                                const link = contenedor.querySelector('a[href]');
                                if (link) {
                                    const r = link.getBoundingClientRect();
                                    if (r.width > 0 && r.height > 0) {
                                        return {
                                            texto: link.textContent.trim(),
                                            found: true
                                        };
                                    }
                                }
                            }
                        }
                    }
                    return {found: false};
                }
            """)

            if link_info and link_info.get("found"):
                log.info(f"  [{espera+1}] Link en modal 'Descargar Archivo': '{link_info['texto']}'")
                # Ahora obtener el ElementHandle real de ese link
                link_el = await self.page.evaluate_handle("""
                    () => {
                        for (const el of document.querySelectorAll('*')) {
                            if (el.textContent.trim() === 'Descargar Archivo' &&
                                el.offsetParent !== null) {
                                let contenedor = el;
                                for (let i = 0; i < 10; i++) {
                                    contenedor = contenedor.parentElement;
                                    if (!contenedor) break;
                                    const link = contenedor.querySelector('a[href]');
                                    if (link) {
                                        const r = link.getBoundingClientRect();
                                        if (r.width > 0 && r.height > 0) return link;
                                    }
                                }
                            }
                        }
                        return null;
                    }
                """)
                # evaluate_handle puede retornar JSHandle de null
                is_null = await self.page.evaluate("el => el === null", link_el)
                if not is_null:
                    break
                link_el = None

            log.info(f"  [{espera+1}] Esperando link inventario en modal 'Descargar Archivo'...")

        await self._screenshot("inv_paso6_modal_descarga")

        if not link_el:
            raise Exception("Link inventario no encontrado en modal 'Descargar Archivo'")

        try:
            async with self.page.expect_download(timeout=60000) as dl_info:
                await link_el.click()
            download = await dl_info.value
            dest = DOWNLOAD_DIR / download.suggested_filename
            await download.save_as(str(dest))
            log.info(f"  ✅ Inventario descargado: {dest}")
            await self._screenshot("inv_paso6_descarga_ok")
            return str(dest)
        except Exception as e:
            log.error(f"  Error descarga inventario: {e}")
            await self._screenshot("inv_error_descarga")
            return None

    # ─────────────────────────────────────────────
    # RUNNER PRINCIPAL
    # ─────────────────────────────────────────────

    async def run(self):
        result = {"success": False, "archivo_ventas": None,
                  "archivo_inventario": None, "error": None}

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=["--ignore-certificate-errors", "--no-sandbox",
                      "--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                ignore_https_errors=True,
                viewport={"width": 1280, "height": 720},
                accept_downloads=True,
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            self.page = await context.new_page()

            try:
                await self.step1_select_pais_y_unidad()
                if not await self.step2_login():
                    result["error"] = "Login fallido"
                    return result

                await self.step3_navegar_ventas()
                await self.step4_setear_fecha_y_generar()

                # ── Ventas: step5 + step6 con reintento por pérdida de conexión ──
                MAX_REINTENTOS_VENTAS = 3
                popup_ventas_ok = False
                for reintento in range(MAX_REINTENTOS_VENTAS):
                    if reintento > 0:
                        log.warning(f"  🔁 Reintento ventas [{reintento}/{MAX_REINTENTOS_VENTAS-1}] — rehaciendo step5")
                        await self._screenshot(f"paso5_reintento_{reintento}")
                    await self.step5_dobleclick_1974206()
                    popup_ventas_ok = await self.step6_click_boton_descarga()
                    if popup_ventas_ok:
                        break
                    log.warning(f"  step6 retornó False (reintento {reintento+1}/{MAX_REINTENTOS_VENTAS})")

                if not popup_ventas_ok:
                    log.error("  ❌ No se pudo abrir popup de descarga en ventas tras reintentos")
                    raise Exception("Popup descarga ventas no abierto tras reintentos")

                await self.step7_seleccionar_formato()
                archivo = await self.step8_descargar_archivo()

                if archivo:
                    result["archivo_ventas"] = archivo
                    log.info(f"✅ Ventas descargado | {archivo}")

                    # ── Inventario encadenado ──────────────────────────────
                    log.info("=" * 50)
                    log.info("Iniciando RPA Inventario...")
                    log.info("=" * 50)

                    # RESET DE SESIÓN VAADIN antes de inventario.
                    # Navegar a BASE_URL destruye toda la sesión Vaadin del servidor
                    # (modales, estado de paneles, cache de datos) y reconstruye
                    # la aplicación desde cero. Es la única forma 100% confiable
                    # de limpiar modales que quedaron en estado inconsistente
                    # tras pérdidas de conexión — ningún click en el DOM los cierra.
                    # El goto a BASE_URL lleva a la pantalla de selección País/Unidad,
                    # no directamente al Keycloak — hay que pasar por step1 primero.
                    log.info("  Reset sesión: goto BASE_URL + step1 + login")
                    await self.page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
                    await self._screenshot("inv_reset_goto_base")
                    await self.step1_select_pais_y_unidad()
                    if not await self.step2_login():
                        raise Exception("Login fallido en reset de sesión para inventario")
                    await self._screenshot("inv_inicio_pantalla_limpia")

                    await self.step_inv1_navegar_inventario()
                    await self.step_inv2_generar_informe()

                    # step_inv3 + step_inv4 con reintento por pérdida de conexión
                    MAX_REINTENTOS_INV = 3
                    popup_inv_ok = False
                    for reintento in range(MAX_REINTENTOS_INV):
                        if reintento > 0:
                            log.warning(f"  🔁 Reintento inventario [{reintento}/{MAX_REINTENTOS_INV-1}] — rehaciendo inv3")
                            await self._screenshot(f"inv_paso3_reintento_{reintento}")
                        await self.step_inv3_dobleclick_1974206()
                        popup_inv_ok = await self.step_inv4_click_boton_descarga()
                        if popup_inv_ok:
                            break
                        log.warning(f"  inv4 retornó False (reintento {reintento+1}/{MAX_REINTENTOS_INV})")

                    if not popup_inv_ok:
                        log.error("  ❌ No se pudo abrir popup de descarga en inventario tras reintentos")
                        result["error"] = "Popup descarga inventario no abierto tras reintentos"
                    else:
                        await self.step_inv5_seleccionar_dato_fuente()
                        archivo_inv = await self.step_inv6_descargar_archivo()
                        if archivo_inv:
                            log.info(f"✅ Inventario descargado | {archivo_inv}")
                            result["success"] = True
                            result["archivo_inventario"] = archivo_inv

                            # ── Subida a Supabase ──────────────────────────
                            log.info("=" * 50)
                            log.info("Iniciando subida a Supabase...")
                            log.info("=" * 50)
                            try:
                                subir_ventas(result["archivo_ventas"])
                            except Exception as e:
                                log.error(f"  Error subiendo ventas a Supabase: {e}")
                                result["error_supabase_ventas"] = str(e)
                            try:
                                subir_inventario(archivo_inv)
                            except Exception as e:
                                log.error(f"  Error subiendo inventario a Supabase: {e}")
                                result["error_supabase_inventario"] = str(e)
                        else:
                            result["error"] = "Descarga inventario fallida"
                else:
                    result["error"] = "Descarga ventas fallida"

            except Exception as e:
                if "FECHA_NO_DISPONIBLE" in str(e):
                    log.warning(f"RPA detenido: {e}")
                    result["error"] = str(e)
                else:
                    log.error(f"Error crítico: {e}")
                    result["error"] = str(e)
                    try:
                        await self._screenshot("error_critico")
                    except Exception:
                        pass
            finally:
                await browser.close()

        return result


if __name__ == "__main__":
    rpa = VentaDiariaRPA(
        username=os.getenv("CENC_USER"),
        password=os.getenv("CENC_PASS"),
        headless=os.getenv("HEADLESS", "true").lower() == "true",
    )
    print(asyncio.run(rpa.run()))
