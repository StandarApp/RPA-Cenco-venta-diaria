"""
RPA - Cencosud Descarga Histórica Ventas Febrero 2026
======================================================
Descarga ventas día por día para todo febrero 2026.

Flujo por cada día:
  1. Verificar si ya hay datos en Supabase para ese día → saltar si existen
  2. Navegar a Comercial → Ventas
  3. Setear DESDE = HASTA = día a descargar via calendario
  4. Generar Informe
  5. Doble click en 1974206 → modal Detalle de Producto
  6. Click fila + botón ↓ → popup descarga
  7. Click SELECCIONAR → modal Formato de Descarga
  8. Descargar zip
  9. Subir a Supabase ventas_cencosud
  Reset sesión Vaadin antes del siguiente día
"""

import asyncio
import os
import logging
import zipfile
from pathlib import Path
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import pandas as pd
from supabase import create_client

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────

MES_INICIO = date(2026, 2, 1)
MES_FIN    = date(2026, 2, 28)

# ─────────────────────────────────────────────
# SUPABASE
# ─────────────────────────────────────────────

def _supabase_client():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        raise ValueError("SUPABASE_URL y SUPABASE_KEY deben estar definidas")
    return create_client(url, key)


def _fecha_existe_en_supabase(fecha):
    """Retorna True si ya hay registros para esa fecha en ventas_cencosud."""
    try:
        sb = _supabase_client()
        fecha_str = fecha.strftime("%Y-%m-%d")
        resp = sb.table("ventas_cencosud").select("id").eq("fecha", fecha_str).limit(1).execute()
        return len(resp.data) > 0
    except Exception as e:
        log.error(f"  Error consultando Supabase para {fecha}: {e}")
        return False


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
                                return df
                        except Exception:
                            pass
                raise ValueError(f"No se pudo parsear el CSV {nombre}")
            elif ext == ".xlsx":
                df = pd.read_excel(io.BytesIO(raw), engine="openpyxl")
                log.info(f"  XLSX leido: {len(df)} filas")
                return df
            elif ext == ".xls":
                df = pd.read_excel(io.BytesIO(raw), engine="xlrd")
                log.info(f"  XLS leido: {len(df)} filas")
                return df
        raise ValueError(f"No se encontro ningun archivo Excel/CSV dentro de {zip_path}")


def _limpiar_numero(valor):
    if pd.isna(valor):
        return None
    s = str(valor).strip().replace("$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def subir_ventas(zip_path, fecha):
    log.info(f"  Subiendo ventas de {fecha} a Supabase...")
    fecha_db = fecha.strftime("%Y-%m-%d")
    sb = _supabase_client()
    df = _leer_excel_de_zip(zip_path)
    col_local   = _buscar_columna(df, ["Cód. Local", "Local", "ID Local", "cod_local", "Cod. Local", "Codigo Local", "Cod Local"])
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
        log.warning(f"  No hay registros validos para {fecha_db}")
        return 0
    sb.table("ventas_cencosud").delete().eq("fecha", fecha_db).execute()
    chunk = 500
    for i in range(0, len(registros), chunk):
        sb.table("ventas_cencosud").upsert(registros[i:i+chunk]).execute()
    log.info(f"  ✅ {len(registros)} registros subidos para {fecha_db}")
    return len(registros)


BASE_URL     = "https://www.cenconlineb2b.com/"
DOWNLOAD_DIR = Path("downloads")
LOG_DIR      = Path("logs")
DOWNLOAD_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / f"rpa_historico_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("HistoricoVentas")


def _es_dashboard(url):
    return (
        "cenconlineb2b.com" in url
        and "ssocencosud" not in url
        and "BBRe-commerce/main" in url
    )


class HistoricoVentasRPA:
    def __init__(self, username, password, headless=True):
        self.username = username
        self.password = password
        self.headless  = headless
        self.page      = None

    async def _screenshot(self, name):
        path = LOG_DIR / f"{name}_{datetime.now().strftime('%H%M%S')}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True, timeout=8000)
            log.info(f"  Screenshot: {path}")
        except Exception as e:
            log.warning(f"  Screenshot omitido ({name}): {type(e).__name__}")

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
                        if ((t.includes('perdió conexión') ||
                             t.includes('Reconectando') ||
                             t.includes('Lost connection')) &&
                            el.offsetParent !== null) return true;
                    }
                    return false;
                }
            """)
            if not desconectado:
                return True
            log.warning(f"  Conexión perdida — esperando [{i+1}/{timeout_s*2}]...")
            await asyncio.sleep(0.5)
        return False

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

    async def _click_fila_modal(self):
        coords = await self.page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('*')) {
                    if (el.textContent.trim() === 'Detalle de Producto' &&
                        el.offsetParent !== null) {
                        let contenedor = el;
                        for (let i = 0; i < 20; i++) {
                            contenedor = contenedor.parentElement;
                            if (!contenedor) break;
                            const celdas = contenedor.querySelectorAll(
                                '.v-grid-body td, .v-grid-body .v-grid-cell'
                            );
                            for (const celda of celdas) {
                                const r = celda.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0 && r.top > 100)
                                    return {x: Math.round(r.left + r.width/2),
                                            y: Math.round(r.top + r.height/2)};
                            }
                        }
                    }
                }
                return null;
            }
        """)
        if coords:
            await self.page.mouse.move(coords["x"], coords["y"])
            await asyncio.sleep(0.3)
            await self.page.mouse.click(coords["x"], coords["y"])
            await asyncio.sleep(0.5)

    async def _clickear_dia_calendario(self, dia):
        resultado = await self.page.evaluate(f"""
            () => {{
                const dia = {dia};
                const selectores = [
                    '.v-datefield-calendarpanel-day',
                    '.v-datefield-calendarpanel td',
                    '.v-overlay-container .v-datefield-calendarpanel td',
                ];
                for (const sel of selectores) {{
                    for (const el of document.querySelectorAll(sel)) {{
                        if (el.className && el.className.includes('offmonth')) continue;
                        if (el.className && el.className.includes('weekday')) continue;
                        if (el.textContent.trim() !== String(dia)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && r.top > 0)
                            return {{x: Math.round(r.left + r.width/2),
                                    y: Math.round(r.top + r.height/2)}};
                    }}
                }}
                return null;
            }}
        """)
        if resultado:
            log.info(f"  Día {dia} @ ({resultado['x']}, {resultado['y']})")
            await self.page.mouse.move(resultado["x"], resultado["y"])
            await asyncio.sleep(0.2)
            await self.page.mouse.click(resultado["x"], resultado["y"])
            await asyncio.sleep(0.5)
            return True
        log.warning(f"  Día {dia} no encontrado en calendario")
        return False

    # ─────────────────────────────────────────────
    # LOGIN
    # ─────────────────────────────────────────────

    async def login(self):
        log.info("Login: Chile + Supermercados + Keycloak")
        if not _es_dashboard(self.page.url):
            try:
                await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        if _es_dashboard(self.page.url):
            log.info("  Sesión activa")
            return True
        pais = await self.page.wait_for_selector("select", timeout=10000)
        await pais.select_option(label="Chile")
        await asyncio.sleep(1.5)
        selects = await self.page.query_selector_all("select")
        if len(selects) >= 2:
            await selects[1].select_option(label="Supermercados")
            await asyncio.sleep(1.0)
        btn = await self.page.wait_for_selector("#btnIngresar", timeout=8000)
        try:
            await btn.click(no_wait_after=True, timeout=5000)
        except Exception:
            await self.page.evaluate("document.getElementById('btnIngresar').click()")

        kc_listo = False
        for i in range(60):
            await asyncio.sleep(1)
            if _es_dashboard(self.page.url):
                log.info("  ✅ Sesión activa")
                return True
            try:
                el = await self.page.query_selector("#kc-login")
                if el:
                    kc_listo = True
                    break
            except Exception:
                pass

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
            if _es_dashboard(self.page.url):
                break
        else:
            return False

        try:
            await self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        log.info("  ✅ Login OK")
        return True

    async def reset_sesion(self):
        """Reset completo de sesión Vaadin antes de cada día."""
        log.info("  Reset sesión Vaadin...")
        try:
            await self.page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        except Exception:
            pass
        await self.login()

    # ─────────────────────────────────────────────
    # DESCARGA DE UN DÍA
    # ─────────────────────────────────────────────

    async def descargar_dia(self, fecha):
        """
        Descarga ventas para un día específico.
        Retorna path del zip descargado o None si falló.
        """
        fecha_str = fecha.strftime("%d-%m-%Y")
        log.info(f"\n{'='*50}")
        log.info(f"Descargando ventas para {fecha_str}...")
        log.info(f"{'='*50}")

        # Esperar conexión estable
        await self._esperar_conexion()

        # Navegar a Comercial → Ventas
        try:
            await self.page.wait_for_selector(".v-menubar-menuitem-caption", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(2)

        panel_ok = False
        for ciclo in range(5):
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
                if ok2:
                    panel_ok = True
                    break
            if panel_ok:
                break

        if not panel_ok:
            log.error(f"  Panel Ventas no cargó para {fecha_str}")
            return None

        # Setear DESDE via calendario
        icono_cal = await self.page.evaluate("""
            () => {
                const campos = document.querySelectorAll('.v-datefield');
                if (campos.length === 0) return null;
                const btn = campos[0].querySelector(
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

        await self.page.mouse.move(icono_cal["x"], icono_cal["y"])
        await asyncio.sleep(0.3)
        await self.page.mouse.click(icono_cal["x"], icono_cal["y"])
        await asyncio.sleep(1.0)
        await self._screenshot(f"cal_desde_{fecha_str}")

        ok_cal = await self._clickear_dia_calendario(fecha.day)
        if not ok_cal:
            log.error(f"  No se pudo setear DESDE para {fecha_str}")
            return None

        await self._screenshot(f"fecha_seteada_{fecha_str}")
        await asyncio.sleep(1)

        # Click Generar Informe
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
                await self.page.mouse.move(coords["x"], coords["y"])
                await asyncio.sleep(0.3)
                await self.page.mouse.click(coords["x"], coords["y"])
                break

        await asyncio.sleep(5)
        await self._esperar_conexion()

        # Esperar celda 1974206
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
            log.info(f"  Esperando 1974206 [{espera+1}/15]...")
            await asyncio.sleep(2)

        if not coords:
            log.error(f"  Celda 1974206 no encontrada para {fecha_str}")
            return None

        # Doble click en celda
        modal_ok = False
        for intento in range(6):
            await self.page.mouse.move(coords["x"], coords["y"])
            await asyncio.sleep(0.2)
            await self.page.mouse.dblclick(coords["x"], coords["y"], delay=100)
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
            if estado["ok"]:
                modal_ok = True
                break
            coords = await self.page.evaluate(JS_CELDA) or coords

        if not modal_ok:
            log.warning(f"  Modal no detectado para {fecha_str}")

        # Click fila + botón ↓
        await self._click_fila_modal()

        candidatos = [
            (1075, 192), (1064, 180), (1070, 185), (1070, 192),
            (1080, 192), (1060, 192), (1075, 185), (1075, 180),
        ]
        popup_ok = False
        for x, y in candidatos:
            await self.page.mouse.move(x, y)
            await asyncio.sleep(0.2)
            await self.page.mouse.click(x, y)
            await asyncio.sleep(1.2)
            if await self._popup_descarga_visible():
                popup_ok = True
                break
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
                popup_ok = True
                break

        if not popup_ok:
            log.error(f"  Popup no abierto para {fecha_str}")
            return None

        # Click SELECCIONAR
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

        coords_sel = await self.page.evaluate("""
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
        if coords_sel:
            await self.page.mouse.move(coords_sel["x"], coords_sel["y"])
            await asyncio.sleep(0.2)
            await self.page.mouse.click(coords_sel["x"], coords_sel["y"])
        else:
            await self.page.mouse.move(575, 449)
            await asyncio.sleep(0.2)
            await self.page.mouse.click(575, 449)

        # Descargar archivo
        link_el = None
        for espera in range(20):
            await asyncio.sleep(0.5)
            link_el = await self.page.query_selector(
                "a[href*='Ventas'], a[href*='ventas'], a[href*='.zip'], a[href*='.xls']"
            )
            if link_el and await link_el.is_visible():
                break

        if not link_el:
            log.error(f"  Link de descarga no encontrado para {fecha_str}")
            return None

        try:
            async with self.page.expect_download(timeout=60000) as dl_info:
                await link_el.click()
            download = await dl_info.value
            dest = DOWNLOAD_DIR / f"{fecha.strftime('%Y%m%d')}_{download.suggested_filename}"
            await download.save_as(str(dest))
            log.info(f"  ✅ Descargado: {dest}")
            return str(dest)
        except Exception as e:
            log.error(f"  Error descargando {fecha_str}: {e}")
            return None

    # ─────────────────────────────────────────────
    # RUNNER PRINCIPAL
    # ─────────────────────────────────────────────

    async def run(self):
        # Generar lista de días de febrero 2026
        dias = []
        d = MES_INICIO
        while d <= MES_FIN:
            dias.append(d)
            d += timedelta(days=1)

        log.info(f"Días a procesar: {len(dias)} ({MES_INICIO} → {MES_FIN})")

        # Verificar cuáles ya existen en Supabase
        dias_pendientes = []
        for d in dias:
            if _fecha_existe_en_supabase(d):
                log.info(f"  ⏭️  {d.strftime('%d-%m-%Y')} ya existe en Supabase — saltando")
            else:
                dias_pendientes.append(d)

        log.info(f"Días pendientes: {len(dias_pendientes)}")

        if not dias_pendientes:
            log.info("✅ Todos los días ya están en Supabase")
            return

        resumen = {"ok": [], "error": []}

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
                # Login inicial
                await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                if not await self.login():
                    log.error("Login inicial fallido")
                    return

                for i, fecha in enumerate(dias_pendientes):
                    fecha_str = fecha.strftime("%d-%m-%Y")
                    log.info(f"\n[{i+1}/{len(dias_pendientes)}] Procesando {fecha_str}...")

                    # Reset sesión antes de cada día (excepto el primero)
                    if i > 0:
                        await self.reset_sesion()

                    # Descargar
                    zip_path = await self.descargar_dia(fecha)

                    if zip_path:
                        try:
                            subir_ventas(zip_path, fecha)
                            resumen["ok"].append(fecha_str)
                        except Exception as e:
                            log.error(f"  Error subiendo {fecha_str} a Supabase: {e}")
                            resumen["error"].append(fecha_str)
                    else:
                        resumen["error"].append(fecha_str)

            except Exception as e:
                log.error(f"Error crítico: {e}")
            finally:
                await browser.close()

        log.info("\n" + "="*50)
        log.info("RESUMEN FINAL")
        log.info("="*50)
        log.info(f"✅ Exitosos ({len(resumen['ok'])}): {resumen['ok']}")
        log.info(f"❌ Errores  ({len(resumen['error'])}): {resumen['error']}")


if __name__ == "__main__":
    rpa = HistoricoVentasRPA(
        username=os.getenv("CENC_USER"),
        password=os.getenv("CENC_PASS"),
        headless=os.getenv("HEADLESS", "true").lower() == "true",
    )
    asyncio.run(rpa.run())
