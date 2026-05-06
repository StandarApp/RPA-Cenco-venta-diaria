"""
RPA - Cencosud Venta Diaria
============================
Flujo:
  1. Consultar fechas máximas en Supabase (ventas_cencosud, inventarios_cencosud)
  2. Login (Chile + Supermercados + Keycloak SSO)
  3. Menú Comercial → Ventas
  4. Leer fecha HASTA de la plataforma
     - Si fecha_plataforma > fecha_max_ventas_supabase → correr ventas
     - Si no → saltar ventas
  5. Doble click en celda adyacente a 1974206 → modal Detalle de Producto
  6. Click en primera fila del modal (activa el botón ↓)
     Click botón ↓ → popup descarga
  7. Click SELECCIONAR → modal Formato de Descarga
  8. Click link Ventas(detalleProducto)*.zip → descarga
  Reset sesión Vaadin
  9. Menú Abastecimiento → Detalle de Inventario
  10. Leer fecha de completitud de la plataforma
      - Si fecha_plataforma > fecha_max_inventario_supabase → correr inventario
      - Si no → saltar inventario
  11-14. Generar informe, doble click, descargar inventario
  15. Subir ambos a Supabase
"""

import asyncio
import os
import logging
import zipfile
from pathlib import Path
from datetime import datetime, date
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import pandas as pd
from supabase import create_client

load_dotenv()

# ─────────────────────────────────────────────
# SUPABASE
# ─────────────────────────────────────────────

def _supabase_client():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise ValueError("SUPABASE_URL y SUPABASE_KEY deben estar definidas")
    return create_client(url, key)


def _fecha_max_supabase(tabla, columna="fecha"):
    try:
        sb = _supabase_client()
        resp = sb.table(tabla).select(columna).order(columna, desc=True).limit(1).execute()
        if resp.data and len(resp.data) > 0:
            fecha_str = resp.data[0][columna]
            return datetime.strptime(fecha_str, "%Y-%m-%d").date()
        return None
    except Exception as e:
        log.error(f"  Error consultando fecha max de {tabla}: {e}")
        return None


def _parsear_fecha_plataforma(fecha_str):
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(fecha_str.strip(), fmt).date()
        except ValueError:
            pass
    return None


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
    import io
    with zipfile.ZipFile(zip_path) as z:
        nombres = z.namelist()
        log.info(f"  Archivos dentro del ZIP: {nombres}")
        for nombre in nombres:
            ext = Path(nombre).suffix.lower()
            if ext not in (".xlsx", ".xls", ".csv"):
                continue
            raw = z.read(nombre)
            if ext == ".csv":
                for encoding in ("utf-16-le", "utf-8-sig", "latin-1", "utf-8"):
                    for sep in (",", ";", "\t"):
                        try:
                            df = pd.read_csv(io.BytesIO(raw), sep=sep, encoding=encoding)
                            if len(df.columns) > 1 and not df.columns[0].startswith("Unnamed"):
                                log.info(f"  CSV leido con encoding='{encoding}' sep='{sep}': {len(df)} filas")
                                log.info(f"  Columnas: {list(df.columns)}")
                                return df
                        except Exception:
                            pass
                raise ValueError(f"No se pudo parsear el CSV {nombre} dentro de {zip_path}")
            elif ext == ".xlsx":
                df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")
                log.info(f"  XLSX leido: {len(df)} filas, columnas: {list(df.columns)}")
                return df
            elif ext == ".xls":
                df = pd.read_excel(io.BytesIO(raw), engine="xlrd")
                log.info(f"  XLS leido: {len(df)} filas, columnas: {list(df.columns)}")
                return df
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


def subir_ventas(zip_path, fecha_plataforma):
    log.info("=" * 50)
    log.info("Subiendo VENTAS a Supabase...")
    log.info("=" * 50)
    fecha_db = fecha_plataforma.strftime("%Y-%m-%d")
    sb = _supabase_client()
    df = _leer_excel_de_zip(zip_path)
    log.info(f"  Filas leidas del Excel: {len(df)}")
    log.info(f"  Columnas: {list(df.columns)}")
    col_local   = _buscar_columna(df, ["Cód. Local", "Local", "ID Local", "cod_local", "sucursal", "Cod. Local", "Codigo Local", "Cod Local"])
    col_desc    = _buscar_columna(df, ["Descripción Local", "Nombre Local", "descripcion", "nombre", "Descripcion Local"])
    col_vta_un  = _buscar_columna(df, ["Venta Período(Un)", "Venta Periodo(Un)", "venta periodo(un)", "unidades", "Vta. Periodo (Un)", "Vta. Periodo(Un)"])
    col_vta_clp = _buscar_columna(df, ["Venta Período Público ($)", "Venta Periodo Publico ($)", "Ventas", "Monto", "venta periodo publico", "Vta. Periodo Publico ($)"])
    registros = []
    for _, fila in df.iterrows():
        vta_un  = _limpiar_numero(fila[col_vta_un])
        vta_clp = _limpiar_numero(fila[col_vta_clp])
        if vta_un is None and vta_clp is None:
            continue
        registros.append({
            "fecha":                     fecha_db,
            "cod_local":                 str(fila[col_local]).strip(),
            "descripcion_local":         str(fila[col_desc]).strip(),
            "venta_periodo_un":          vta_un,
            "venta_periodo_publico_clp": vta_clp,
        })
    if not registros:
        log.warning("  No hay registros validos para subir a ventas_cencosud")
        return
    log.info(f"  Eliminando registros de fecha {fecha_db} en ventas_cencosud...")
    sb.table("ventas_cencosud").delete().eq("fecha", fecha_db).execute()
    log.info(f"  Subiendo {len(registros)} registros a ventas_cencosud...")
    chunk = 500
    for i in range(0, len(registros), chunk):
        sb.table("ventas_cencosud").upsert(registros[i:i+chunk]).execute()
        log.info(f"    Batch {i//chunk + 1}: {min(i+chunk, len(registros))}/{len(registros)}")
    log.info(f"Ventas subidas: {len(registros)} filas para fecha {fecha_db}")


def subir_inventario(zip_path, fecha_plataforma):
    log.info("=" * 50)
    log.info("Subiendo INVENTARIO a Supabase...")
    log.info("=" * 50)
    fecha_db = fecha_plataforma.strftime("%Y-%m-%d")
    sb = _supabase_client()
    df = _leer_excel_de_zip(zip_path)
    log.info(f"  Filas leidas del Excel: {len(df)}")
    log.info(f"  Columnas: {list(df.columns)}")
    col_local  = _buscar_columna(df, ["Cód. Local", "Local", "ID Local", "cod_local", "sucursal", "Cod. Local", "Codigo Local", "Cod Local"])
    col_desc   = _buscar_columna(df, ["Descripción Local", "Nombre Local", "descripcion", "nombre", "Descripcion Local"])
    col_stock  = _buscar_columna(df, ["Stock(Un)", "stock_un", "stock", "Inv. Actual(Un)", "Inv. Actual (Un)", "Stock (Un)"])
    registros = []
    for _, fila in df.iterrows():
        stock = _limpiar_numero(fila[col_stock])
        if stock is None:
            continue
        registros.append({
            "fecha":             fecha_db,
            "cod_local":         str(fila[col_local]).strip(),
            "descripcion_local": str(fila[col_desc]).strip(),
            "stock_un":          stock,
        })
    if not registros:
        log.warning("  No hay registros validos para subir a inventarios_cencosud")
        return
    log.info(f"  Eliminando registros de fecha {fecha_db} en inventarios_cencosud...")
    sb.table("inventarios_cencosud").delete().eq("fecha", fecha_db).execute()
    log.info(f"  Subiendo {len(registros)} registros a inventarios_cencosud...")
    chunk = 500
    for i in range(0, len(registros), chunk):
        sb.table("inventarios_cencosud").upsert(registros[i:i+chunk]).execute()
        log.info(f"    Batch {i//chunk + 1}: {min(i+chunk, len(registros))}/{len(registros)}")
    log.info(f"Inventario subido: {len(registros)} filas para fecha {fecha_db}")


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


class VentaDiariaRPA:
    def __init__(self, username, password, headless=True):
        self.username = username
        self.password = password
        self.headless  = headless
        self.page      = None
        self.fecha_max_ventas     = None
        self.fecha_max_inventario = None

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

    async def _esperar_conexion(self, timeout_s=30):
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
        return await self.page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('*')) {
                    const t = el.textContent.trim();
                    if (el.offsetParent === null) continue;
                    if (t === 'Formato de Descarga') return true;
                    if (t.includes('Dato Fuente') && t.length < 80) return true;
                    if (t === 'Descargar Archivo') return true;
                }
                for (const sel of ['td.gwt-MenuItem', '.v-menubar-popup td', '.v-contextmenu td']) {
                    for (const el of document.querySelectorAll(sel)) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.top > 0 && el.textContent.trim().length > 2) return true;
                    }
                }
                return false;
            }
        """)

    async def _modal_detalle_visible(self, titulo="Detalle de Producto"):
        return await self.page.evaluate(f"""
            () => {{
                for (const el of document.querySelectorAll('*')) {{
                    if (el.textContent.trim() === '{titulo}' &&
                        el.offsetParent !== null) return true;
                }}
                return false;
            }}
        """)

    async def _click_fila_modal(self, titulo_modal="Detalle de Producto"):
        """
        Click en la primera fila de la tabla del modal para activarla.
        Esto es necesario para que el botón ↓ quede habilitado.
        """
        coords = await self.page.evaluate(f"""
            () => {{
                // Buscar el modal por su título
                for (const el of document.querySelectorAll('*')) {{
                    if (el.textContent.trim() === '{titulo_modal}' &&
                        el.offsetParent !== null) {{
                        // Buscar la primera fila de la tabla dentro del modal
                        let contenedor = el;
                        for (let i = 0; i < 20; i++) {{
                            contenedor = contenedor.parentElement;
                            if (!contenedor) break;
                            // Buscar primera celda visible de la tabla
                            const celdas = contenedor.querySelectorAll(
                                '.v-grid-body td, .v-grid-body .v-grid-cell'
                            );
                            for (const celda of celdas) {{
                                const r = celda.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0 && r.top > 100) {{
                                    return {{
                                        x: Math.round(r.left + r.width / 2),
                                        y: Math.round(r.top + r.height / 2)
                                    }};
                                }}
                            }}
                        }}
                    }}
                }}
                return null;
            }}
        """)
        if coords:
            log.info(f"  Click en primera fila del modal @ ({coords['x']}, {coords['y']})")
            await self.page.mouse.move(coords["x"], coords["y"])
            await asyncio.sleep(0.3)
            await self.page.mouse.click(coords["x"], coords["y"])
            await asyncio.sleep(0.5)
            return True
        else:
            log.warning(f"  No se encontró fila del modal '{titulo_modal}' para click previo")
            return False

    # ─────────────────────────────────────────────
    # PASO 0: Consultar fechas máximas en Supabase
    # ─────────────────────────────────────────────

    def step0_consultar_fechas_supabase(self):
        log.info("=" * 50)
        log.info("Paso 0: Consultando fechas máximas en Supabase...")
        log.info("=" * 50)
        self.fecha_max_ventas = _fecha_max_supabase("ventas_cencosud")
        self.fecha_max_inventario = _fecha_max_supabase("inventarios_cencosud")
        log.info(f"  Fecha max ventas_cencosud:      {self.fecha_max_ventas}")
        log.info(f"  Fecha max inventarios_cencosud: {self.fecha_max_inventario}")

    # ─────────────────────────────────────────────
    # LOGIN
    # ─────────────────────────────────────────────

    async def step1_select_pais_y_unidad(self):
        log.info("Paso 1: Chile + Supermercados")
        if not _es_dashboard(self.page.url):
            try:
                await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        await self._screenshot("paso1_inicio")
        if _es_dashboard(self.page.url):
            log.info("  Sesión activa — saltando")
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
        try:
            await btn.click(no_wait_after=True, timeout=5000)
        except Exception:
            await self.page.evaluate("document.getElementById('btnIngresar').click()")
        log.info(f"  Paso 1 OK | URL: {self.page.url}")

    async def step2_login(self):
        log.info("Paso 2: Login")
        if _es_dashboard(self.page.url):
            log.info("  ✅ Sesión activa")
            return True
        kc_listo = False
        for i in range(60):
            await asyncio.sleep(1)
            if _es_dashboard(self.page.url):
                log.info("  ✅ Sesión activa (detectada durante espera)")
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
            return _es_dashboard(self.page.url)
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
            log.error("  Timeout login")
            return False
        try:
            await self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        log.info("  ✅ Login OK")
        return True

    # ─────────────────────────────────────────────
    # VENTAS
    # ─────────────────────────────────────────────

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

    async def step4_verificar_fecha_y_generar(self):
        """
        Lee la fecha HASTA de la plataforma.
        Compara con fecha_max_ventas de Supabase.
        Retorna (fecha_plataforma, debe_correr).
        """
        log.info("Paso 4: Leer fecha HASTA y comparar con Supabase")
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
                        campos.push({val: val.trim()});
                    }
                }
                return campos;
            }
        """)

        if not fechas:
            fechas = await self.page.evaluate("""
                () => {
                    const campos = [];
                    for (const el of document.querySelectorAll('input')) {
                        const val = el.value || '';
                        if (val.match(/[0-9]{2}-[0-9]{2}-[0-9]{4}/)) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0) campos.push({val: val.trim()});
                        }
                    }
                    return campos;
                }
            """)

        fecha_hasta_str = fechas[1]["val"] if len(fechas) >= 2 else (fechas[0]["val"] if fechas else "")
        log.info(f"  Fecha HASTA plataforma: '{fecha_hasta_str}'")

        fecha_hasta = _parsear_fecha_plataforma(fecha_hasta_str)
        if not fecha_hasta:
            raise Exception(f"No se pudo parsear la fecha HASTA: '{fecha_hasta_str}'")

        log.info(f"  Fecha max ventas Supabase: {self.fecha_max_ventas}")
        log.info(f"  Fecha HASTA plataforma:    {fecha_hasta}")

        if self.fecha_max_ventas is not None and fecha_hasta <= self.fecha_max_ventas:
            log.info(f"  ⏭️  Ventas ya al día — saltando")
            return fecha_hasta, False

        log.info(f"  ✅ Hay datos nuevos de ventas")

        # Setear fecha DESDE = misma fecha HASTA via calendario
        try:
            dia_ayer  = fecha_hasta.day
            mes_ayer  = fecha_hasta.month
            anio_ayer = fecha_hasta.year
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
                                        y: Math.round(r.top + r.height/2)
                                    }};
                                }}
                            }}
                        }}
                    }}
                    return null;
                }}
            """)

            if dia_clickeado:
                await self.page.mouse.move(dia_clickeado["x"], dia_clickeado["y"])
                await asyncio.sleep(0.2)
                await self.page.mouse.click(dia_clickeado["x"], dia_clickeado["y"])
                await asyncio.sleep(0.5)
                log.info(f"  ✅ DESDE seteado a día {dia_ayer}")
            else:
                log.warning(f"  ⚠️ Día {dia_ayer} no encontrado en calendario")
        except Exception as e:
            log.warning(f"  Error seteando calendario DESDE: {e}")

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
        return fecha_hasta, True

    async def step5_dobleclick_1974206(self):
        log.info("Paso 5: Doble click en 1974206")
        await self._wait(1000, 2000)
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
        log.info("Paso 6: Click en botón ↓ del modal")
        await self._esperar_conexion()
        await self._screenshot("paso6_antes")

        # ── FIX: Click en primera fila del modal para activar botón ↓ ──
        await self._click_fila_modal("Detalle de Producto")
        await asyncio.sleep(0.5)

        candidatos = [
            (1075, 192), (1064, 180), (1070, 185), (1070, 192),
            (1080, 192), (1060, 192), (1075, 185), (1075, 180),
        ]

        for x, y in candidatos:
            if await self._conexion_perdida_ahora():
                log.warning(f"  ⚠️ Conexión perdida antes de probar ({x}, {y}) — esperando...")
                recuperado = await self._esperar_conexion()
                if not recuperado:
                    return False
                await asyncio.sleep(1.5)
                if not await self._modal_detalle_visible("Detalle de Producto"):
                    log.warning("  Modal se cerró — reintentar step5")
                    return False

            log.info(f"  Click normal ({x}, {y})...")
            await self.page.mouse.move(x, y)
            await asyncio.sleep(0.2)
            await self.page.mouse.click(x, y)
            await asyncio.sleep(1.2)

            if await self._popup_descarga_visible():
                log.info(f"  ✅ Popup abierto @ ({x}, {y})")
                await self._screenshot("paso6_post_click")
                return True

            if await self._conexion_perdida_ahora():
                log.warning(f"  ⚠️ Conexión perdida tras click ({x}, {y}) — esperando...")
                recuperado = await self._esperar_conexion()
                await asyncio.sleep(2.0)
                if not recuperado:
                    return False
                if not await self._modal_detalle_visible("Detalle de Producto"):
                    return False

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
                return True

            if await self._conexion_perdida_ahora():
                recuperado = await self._esperar_conexion()
                await asyncio.sleep(2.0)
                if not recuperado:
                    return False
                if not await self._modal_detalle_visible("Detalle de Producto"):
                    return False

            log.info(f"  ({x},{y}) sin resultado")

        await self._screenshot("paso6_post_click")
        log.warning("  ⚠️ Ninguna coordenada abrió el popup")
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
            return None

    # ─────────────────────────────────────────────
    # INVENTARIO
    # ─────────────────────────────────────────────

    async def step_inv1_navegar_inventario(self):
        log.info("INV Paso 1: Abastecimiento → Detalle de Inventario")
        await self._wait(2000, 3000)

        for ciclo in range(8):
            log.info(f"  Ciclo {ciclo+1}/8")
            ok = await self._click_vaadin_real("Abastecimiento")
            if not ok:
                await asyncio.sleep(3)
                continue

            submenu_visible = False
            for t in range(6):
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
                await asyncio.sleep(2)
                continue

            ok = await self._click_vaadin_real("Detalle de Inventario")
            if not ok:
                await asyncio.sleep(3)
                continue

            for espera in range(20):
                await asyncio.sleep(2)
                ok2 = await self.page.evaluate("""
                    () => {
                        const textos = ['Detalle de Inventario', 'Inventario'];
                        for (const t of textos) {
                            for (const el of document.querySelectorAll(
                                '.v-panel-caption, .v-label, h2, h3, .v-slot > .v-label'
                            )) {
                                if (el.textContent.trim().includes(t) &&
                                    el.offsetParent !== null) {
                                    return [...document.querySelectorAll('*')]
                                        .some(e => e.children.length===0 &&
                                                   e.textContent.trim()==='Generar Informe' &&
                                                   e.offsetParent !== null);
                                }
                            }
                        }
                        return false;
                    }
                """)
                log.info(f"    [{espera+1}/20] Panel Inventario={ok2}")
                if ok2:
                    log.info("  ✅ Detalle de Inventario cargado")
                    break
            else:
                continue
            break

        await self._screenshot("inv_paso1_cargado")
        log.info("INV Paso 1 completado")

    async def step_inv2_verificar_fecha_completitud(self):
        """
        Lee la fecha de completitud del panel de inventario.
        Compara con fecha_max_inventario de Supabase.
        Retorna (fecha_completitud, debe_correr).
        """
        log.info("INV Paso 2: Leer fecha de completitud y comparar con Supabase")
        await self._screenshot("inv_paso2_antes")

        fecha_completitud_str = await self.page.evaluate("""
            () => {
                const regex = /\b(\d{2}-\d{2}-\d{4})\b/;
                for (const el of document.querySelectorAll('*')) {
                    if (el.children.length === 0 && el.offsetParent !== null) {
                        const t = el.textContent.trim();
                        const match = t.match(regex);
                        if (match) {
                            let padre = el.parentElement;
                            for (let i = 0; i < 5; i++) {
                                if (!padre) break;
                                if (padre.textContent.includes('Inventario') ||
                                    padre.textContent.includes('Completitud')) {
                                    return match[1];
                                }
                                padre = padre.parentElement;
                            }
                        }
                    }
                }
                // Fallback: cualquier fecha visible
                for (const el of document.querySelectorAll('*')) {
                    if (el.children.length === 0 && el.offsetParent !== null) {
                        const match = el.textContent.trim().match(/\b(\d{2}-\d{2}-\d{4})\b/);
                        if (match) return match[1];
                    }
                }
                return null;
            }
        """)

        log.info(f"  Fecha completitud plataforma: '{fecha_completitud_str}'")

        fecha_completitud = _parsear_fecha_plataforma(fecha_completitud_str) if fecha_completitud_str else None
        if not fecha_completitud:
            log.warning("  No se pudo leer la fecha de completitud — continuando igual")
            return None, True

        log.info(f"  Fecha max inventario Supabase: {self.fecha_max_inventario}")
        log.info(f"  Fecha completitud plataforma:  {fecha_completitud}")

        if self.fecha_max_inventario is not None and fecha_completitud <= self.fecha_max_inventario:
            log.info(f"  ⏭️  Inventario ya al día — saltando")
            return fecha_completitud, False

        log.info(f"  ✅ Hay datos nuevos de inventario")
        return fecha_completitud, True

    async def step_inv3_generar_informe(self):
        log.info("INV Paso 3: Generar Informe")
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
                log.info("  ✅ Tabla cargada")
                break

        await self._screenshot("inv_paso3_informe_generado")
        log.info("INV Paso 3 completado")

    async def step_inv4_dobleclick_1974206(self):
        log.info("INV Paso 4: Doble click en 1974206")
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

        coords = None
        for espera in range(15):
            coords = await self.page.evaluate(JS_CELDA)
            if coords:
                break
            log.info(f"  Esperando celda 1974206 [{espera+1}/15]...")
            await asyncio.sleep(2)

        if not coords:
            raise Exception("Celda 1974206 no encontrada en inventario")

        modal_listo = False
        for intento in range(6):
            log.info(f"  dblclick [{intento+1}/6]")
            await self.page.mouse.move(coords["x"], coords["y"])
            await asyncio.sleep(0.2)
            await self.page.mouse.dblclick(coords["x"], coords["y"], delay=100)
            await self._screenshot(f"inv_paso4_dblclick_{intento+1}")
            log.info("  Esperando 12s...")
            await asyncio.sleep(12)
            estado = await self.page.evaluate("""
                () => {
                    const n = document.querySelectorAll('.v-grid-body .v-grid-cell').length;
                    let modal = false;
                    for (const el of document.querySelectorAll('*')) {
                        if (el.textContent.trim()==='Detalle de Inventario' &&
                            el.offsetParent !== null) { modal = true; break; }
                    }
                    return {n: n, ok: n > 20 || modal};
                }
            """)
            log.info(f"  Celdas: {estado['n']} modal={estado['ok']}")
            if estado["ok"]:
                modal_listo = True
                break
            coords = await self.page.evaluate(JS_CELDA) or coords

        if not modal_listo:
            log.warning("  ⚠️ Modal no detectado")

        await self._screenshot("inv_paso4_modal_abierto")
        log.info("INV Paso 4 completado")
        return modal_listo

    async def step_inv5_click_boton_descarga(self):
        log.info("INV Paso 5: Click botón ↓ del modal inventario")
        await self._esperar_conexion()

        # ── FIX: Click en primera fila del modal para activar botón ↓ ──
        await self._click_fila_modal("Detalle de Inventario")
        await asyncio.sleep(0.5)

        candidatos_fijos = [
            (1117, 157), (1075, 192), (1064, 180), (1070, 185),
            (1070, 192), (1080, 192), (1060, 192),
        ]

        for x, y in candidatos_fijos:
            if await self._conexion_perdida_ahora():
                recuperado = await self._esperar_conexion()
                if not recuperado:
                    return False
                await asyncio.sleep(1.5)
                if not await self._modal_detalle_visible("Detalle de Inventario"):
                    return False

            log.info(f"  Probando ({x}, {y})...")
            await self.page.mouse.move(x, y)
            await asyncio.sleep(0.2)
            await self.page.mouse.click(x, y)
            await asyncio.sleep(1.5)

            if await self._popup_descarga_visible():
                log.info(f"  ✅ Popup abierto @ ({x}, {y})")
                await self._screenshot("inv_paso5_post_click")
                return True

            if await self._conexion_perdida_ahora():
                recuperado = await self._esperar_conexion()
                await asyncio.sleep(2.0)
                if not recuperado:
                    return False
                if not await self._modal_detalle_visible("Detalle de Inventario"):
                    return False

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
                await self._screenshot("inv_paso5_post_click")
                return True

            log.info(f"  ({x},{y}) sin resultado")

        log.warning("  ⚠️ Ninguna coordenada abrió el popup (inventario)")
        return False

    async def step_inv6_seleccionar_dato_fuente(self):
        log.info("INV Paso 6: Seleccionar Dato Fuente / Formato")
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
                await self.page.mouse.move(coords["x"], coords["y"])
                await asyncio.sleep(0.2)
                await self.page.mouse.click(coords["x"], coords["y"])
            else:
                await self.page.mouse.move(575, 449)
                await asyncio.sleep(0.2)
                await self.page.mouse.click(575, 449)
        else:
            log.warning("  Ni popup ni formato detectado")

        await asyncio.sleep(2)
        await self._screenshot("inv_paso6_post_seleccion")
        log.info("INV Paso 6 completado")

    async def step_inv7_descargar_archivo(self):
        log.info("INV Paso 7: Click en link de descarga inventario")
        link_el = None
        for espera in range(20):
            await asyncio.sleep(0.5)
            link_info = await self.page.evaluate("""
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
                                    if (r.width > 0 && r.height > 0)
                                        return {texto: link.textContent.trim(), found: true};
                                }
                            }
                        }
                    }
                    return {found: false};
                }
            """)
            if link_info and link_info.get("found"):
                log.info(f"  [{espera+1}] Link: '{link_info['texto']}'")
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
                is_null = await self.page.evaluate("el => el === null", link_el)
                if not is_null:
                    break
                link_el = None
            log.info(f"  [{espera+1}] Esperando link...")

        await self._screenshot("inv_paso7_modal_descarga")
        if not link_el:
            raise Exception("Link inventario no encontrado")

        try:
            async with self.page.expect_download(timeout=60000) as dl_info:
                await link_el.click()
            download = await dl_info.value
            dest = DOWNLOAD_DIR / download.suggested_filename
            await download.save_as(str(dest))
            log.info(f"  ✅ Inventario descargado: {dest}")
            await self._screenshot("inv_paso7_descarga_ok")
            return str(dest)
        except Exception as e:
            log.error(f"  Error descarga inventario: {e}")
            return None

    # ─────────────────────────────────────────────
    # RUNNER PRINCIPAL
    # ─────────────────────────────────────────────

    async def run(self):
        result = {
            "success": False,
            "ventas_corridas": False,
            "inventario_corrido": False,
            "archivo_ventas": None,
            "archivo_inventario": None,
            "error": None
        }

        # ── Paso 0: Consultar fechas máximas en Supabase ──
        self.step0_consultar_fechas_supabase()

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

                # ── VENTAS ────────────────────────────────
                await self.step3_navegar_ventas()
                fecha_plataforma_ventas, correr_ventas = await self.step4_verificar_fecha_y_generar()

                if correr_ventas:
                    MAX_REINTENTOS = 3
                    popup_ok = False
                    for reintento in range(MAX_REINTENTOS):
                        if reintento > 0:
                            log.warning(f"  🔁 Reintento ventas [{reintento}]")
                        await self.step5_dobleclick_1974206()
                        popup_ok = await self.step6_click_boton_descarga()
                        if popup_ok:
                            break

                    if not popup_ok:
                        raise Exception("Popup descarga ventas no abierto tras reintentos")

                    await self.step7_seleccionar_formato()
                    archivo_ventas = await self.step8_descargar_archivo()
                    if archivo_ventas:
                        result["archivo_ventas"] = archivo_ventas
                        result["ventas_corridas"] = True
                        log.info(f"✅ Ventas descargado | {archivo_ventas}")
                    else:
                        result["error"] = "Descarga ventas fallida"
                else:
                    log.info("⏭️  Ventas saltadas — datos ya al día en Supabase")

                # ── RESET SESIÓN VAADIN ───────────────────
                log.info("  Reset sesión: goto BASE_URL + step1 + login")
                await self.page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
                await self._screenshot("inv_reset_goto_base")
                await self.step1_select_pais_y_unidad()
                if not await self.step2_login():
                    raise Exception("Login fallido en reset de sesión para inventario")

                # ── INVENTARIO ────────────────────────────
                await self.step_inv1_navegar_inventario()
                fecha_plataforma_inv, correr_inventario = await self.step_inv2_verificar_fecha_completitud()

                if correr_inventario:
                    await self.step_inv3_generar_informe()

                    MAX_REINTENTOS_INV = 3
                    popup_inv_ok = False
                    for reintento in range(MAX_REINTENTOS_INV):
                        if reintento > 0:
                            log.warning(f"  🔁 Reintento inventario [{reintento}]")
                        await self.step_inv4_dobleclick_1974206()
                        popup_inv_ok = await self.step_inv5_click_boton_descarga()
                        if popup_inv_ok:
                            break

                    if not popup_inv_ok:
                        result["error"] = "Popup descarga inventario no abierto tras reintentos"
                    else:
                        await self.step_inv6_seleccionar_dato_fuente()
                        archivo_inv = await self.step_inv7_descargar_archivo()
                        if archivo_inv:
                            result["archivo_inventario"] = archivo_inv
                            result["inventario_corrido"] = True
                            log.info(f"✅ Inventario descargado | {archivo_inv}")
                        else:
                            result["error"] = "Descarga inventario fallida"
                else:
                    log.info("⏭️  Inventario saltado — datos ya al día en Supabase")

                # ── SUBIDA A SUPABASE ─────────────────────
                if result["ventas_corridas"] or result["inventario_corrido"]:
                    log.info("=" * 50)
                    log.info("Subiendo a Supabase...")
                    log.info("=" * 50)
                    if result["ventas_corridas"] and result["archivo_ventas"]:
                        try:
                            subir_ventas(result["archivo_ventas"], fecha_plataforma_ventas)
                        except Exception as e:
                            log.error(f"  Error subiendo ventas: {e}")
                            result["error_supabase_ventas"] = str(e)
                    if result["inventario_corrido"] and result["archivo_inventario"]:
                        try:
                            subir_inventario(result["archivo_inventario"], fecha_plataforma_inv)
                        except Exception as e:
                            log.error(f"  Error subiendo inventario: {e}")
                            result["error_supabase_inventario"] = str(e)

                if result["ventas_corridas"] and result["inventario_corrido"]:
                    result["success"] = True
                elif not correr_ventas and not correr_inventario:
                    log.info("✅ Ambos ya al día — nada que hacer")
                    result["success"] = True

            except Exception as e:
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
    resultado = asyncio.run(rpa.run())
    print(resultado)
