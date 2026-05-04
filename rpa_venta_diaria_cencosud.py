"""
RPA - Cencosud Venta Diaria
============================
Flujo:
  1. Login (Chile + Supermercados + Keycloak SSO)
  2. Menú Comercial → Ventas
  3. Setear fecha AYER en ambos campos (Desde / Hasta)
  4. Click Generar Informe
  5. Doble click en celda 1974206
  6. Click botón ↓ azul del modal → modal Formato de Descarga
  7. Click SELECCIONAR (CSV ya viene seleccionado por defecto)
  8. Click link Ventas(detalleProducto)*.zip → descarga
"""

import asyncio
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

BASE_URL     = "https://www.cenconlineb2b.com/"
DOWNLOAD_DIR = Path("downloads")
LOG_DIR      = Path("logs")
DOWNLOAD_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

log_file = LOG_DIR / f"venta_diaria_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("VentaDiaria")


def _es_dashboard(url: str) -> bool:
    return (
        "cenconlineb2b.com" in url
        and "ssocencosud" not in url
        and "BBRe-commerce/main" in url
    )


def _fecha_ayer() -> str:
    """Retorna la fecha de ayer en formato dd-mm-yyyy (el que usa la plataforma)."""
    ayer = datetime.now() - timedelta(days=1)
    return ayer.strftime("%d-%m-%Y")


class VentaDiariaRPA:
    def __init__(self, username, password, headless=True):
        self.username = username
        self.password = password
        self.headless = headless
        self.page     = None

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
        """Click en item del menubar Vaadin usando coordenadas reales del mouse."""
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
        log.info(f"  Mouse click Vaadin '{texto}' @ ({x:.0f}, {y:.0f})")
        await self.page.mouse.move(x, y)
        await asyncio.sleep(0.2)
        await self.page.mouse.click(x, y)
        return True

    # ─────────────────────────────────────────────────────────────────────────

    async def step1_select_pais_y_unidad(self):
        log.info("Paso 1: Seleccionando Chile y Supermercados")
        await self.page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        await self._screenshot("paso1_inicio")
        if _es_dashboard(self.page.url):
            log.info("Sesión activa — saltando paso 1")
            return
        pais_select = await self.page.wait_for_selector("select", timeout=10000)
        await pais_select.select_option(label="Chile")
        await self._wait(800, 1200)
        await self.page.wait_for_timeout(1500)
        selects = await self.page.query_selector_all("select")
        if len(selects) >= 2:
            await selects[1].select_option(label="Supermercados")
            await self._wait(800, 1200)
        ingresar_btn = await self.page.wait_for_selector("#btnIngresar", timeout=8000)
        await ingresar_btn.click()
        await self.page.wait_for_load_state("networkidle", timeout=20000)
        await self._screenshot("paso1_post_ingresar")
        log.info(f"Paso 1 OK | URL: {self.page.url}")

    async def step2_login(self):
        log.info("Paso 2: Login")
        await self._screenshot("paso2_inicio")
        if _es_dashboard(self.page.url):
            log.info("✅ Sesión activa")
            return True
        try:
            await self.page.wait_for_selector("#kc-login", timeout=10000)
        except Exception:
            if _es_dashboard(self.page.url):
                return True
            log.error(f"Estado inesperado | URL: {self.page.url}")
            return False
        resultado = await self.page.evaluate("""
            ([username, password]) => {
                try {
                    const u = document.getElementById('username');
                    if (!u) return {ok: false};
                    u.value = username;
                    u.dispatchEvent(new Event('input', {bubbles: true}));
                    u.dispatchEvent(new Event('change', {bubbles: true}));
                    const p = document.getElementById('password');
                    if (!p) return {ok: false};
                    p.value = password;
                    p.dispatchEvent(new Event('input', {bubbles: true}));
                    p.dispatchEvent(new Event('change', {bubbles: true}));
                    const btn = document.getElementById('kc-login');
                    if (!btn) return {ok: false};
                    btn.click();
                    return {ok: true};
                } catch(e) { return {ok: false}; }
            }
        """, [self.username, self.password])
        if not resultado or not resultado.get("ok"):
            log.error("JS login falló")
            return False
        await self._screenshot("paso2_submit_disparado")
        for intento in range(60):
            await asyncio.sleep(2)
            try:
                url_actual = self.page.url
            except Exception:
                url_actual = ""
            log.info(f"  [{intento+1:02d}/60] {url_actual}")
            if _es_dashboard(url_actual):
                break
        else:
            log.error("Timeout login")
            return False
        try:
            await self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        await self._screenshot("paso2_login_ok")
        log.info(f"✅ Login OK | URL: {self.page.url}")
        return True

    async def step3_navegar_ventas(self):
        """Menú Comercial → Ventas."""
        log.info("Paso 3: Comercial → Ventas")
        try:
            await self.page.wait_for_selector(".v-menubar-menuitem-caption", timeout=20000)
        except Exception:
            pass
        await self._wait(2000, 3000)
        await self._screenshot("paso3_vaadin_cargado")

        for ciclo in range(5):
            log.info(f"  Ciclo {ciclo+1}/5")
            ok_com = await self._click_vaadin_real("Comercial")
            log.info(f"  Click Comercial: {ok_com}")
            if not ok_com:
                await asyncio.sleep(3)
                continue
            await asyncio.sleep(1.5)
            await self._screenshot(f"paso3_menu_comercial_{ciclo+1}")

            ok_ven = await self._click_vaadin_real("Ventas")
            log.info(f"  Click Ventas: {ok_ven}")
            await self._screenshot(f"paso3_click_ventas_{ciclo+1}")
            if not ok_ven:
                await asyncio.sleep(3)
                continue

            # Esperar que aparezca el panel de filtros con "Generar Informe"
            for espera in range(20):
                await asyncio.sleep(2)
                tiene_generar = await self.page.evaluate("""
                    () => {
                        for (const el of document.querySelectorAll('*')) {
                            if (el.children.length === 0 &&
                                el.textContent.trim() === 'Generar Informe') {
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                log.info(f"    Esperando vista [{espera+1}/20]...")
                if tiene_generar:
                    log.info(f"  ✅ 'Generar Informe' detectado [{espera+1}]")
                    break
            else:
                continue
            break

        await self._screenshot("paso3_ventas_cargado")
        log.info("Paso 3 completado")

    async def step4_setear_fecha_y_generar(self):
        """
        Setear fecha de ayer en ambos campos Desde/Hasta y click Generar Informe.
        Los campos de fecha son inputs de texto — se limpian y se escribe la fecha.
        """
        log.info("Paso 4: Setear fecha ayer y Generar Informe")
        await self._screenshot("paso4_antes_fecha")

        fecha_ayer = _fecha_ayer()
        log.info(f"  Fecha a setear: {fecha_ayer}")

        # Limpiar y escribir en ambos campos de fecha
        # Setear fecha usando Playwright directamente — más confiable que JS
        resultado = 0
        log.info(f"  Seteando fechas con Playwright triple_click+type...")

        # Si JS no encontró los campos, buscarlos con Playwright y escribir con teclado
        if not resultado or resultado < 2:
            log.info("  Intentando con Playwright fill...")
            date_inputs = await self.page.query_selector_all(
                'input[type="text"], .v-datefield-textfield'
            )
            seteados = 0
            import re
            for inp in date_inputs:
                try:
                    val = await inp.input_value()
                    if re.search(r'\d{2}-\d{2}-\d{4}', val or ''):
                        await inp.click(click_count=3)  # seleccionar todo
                        await asyncio.sleep(0.1)
                        await inp.fill(fecha_ayer)
                        await inp.press("Tab")
                        seteados += 1
                        log.info(f"  Campo fecha seteado: '{val}' → '{fecha_ayer}'")
                except Exception as ex:
                    log.warning(f"  Error seteando campo: {ex}")
            log.info(f"  Campos seteados con Playwright: {seteados}")

        await self._screenshot("paso4_fecha_seteada")
        await asyncio.sleep(1)

        # Click en Generar Informe
        for intento in range(10):
            await asyncio.sleep(1)
            coords = await self.page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        if (el.children.length === 0 &&
                            el.textContent.trim() === 'Generar Informe') {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                return {x: Math.round(r.left + r.width/2),
                                        y: Math.round(r.top + r.height/2)};
                            }
                        }
                    }
                    return null;
                }
            """)
            if coords:
                log.info(f"  'Generar Informe' [{intento+1}] @ ({coords['x']}, {coords['y']})")
                await self.page.mouse.move(coords["x"], coords["y"])
                await asyncio.sleep(0.3)
                await self.page.mouse.click(coords["x"], coords["y"])
                break

        await asyncio.sleep(5)
        await self._screenshot("paso4_informe_generado")
        log.info("Paso 4 completado")

    async def step5_dobleclick_1974206(self):
        """
        Doble click en celda adyacente a 1974206 para abrir modal Detalle de Producto.
        Esperar 12s para que cargue la tabla de locales (>20 celdas).
        """
        log.info("Paso 5: Doble click en 1974206")
        await self._wait(1000, 2000)

        JS_CELDA = """
            () => {
                for (const sel of ['td', 'span', 'div', 'a']) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (el.textContent.trim() === '1974206') {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0 &&
                                rect.left > 0 && rect.top > 0) {
                                const sig = el.nextElementSibling;
                                const sigRect = sig ? sig.getBoundingClientRect() : null;
                                return {
                                    x: Math.round(rect.left + rect.width / 2),
                                    y: Math.round(rect.top + rect.height / 2),
                                    x_der: sigRect ? Math.round(sigRect.left + sigRect.width / 2) : Math.round(rect.right + 80),
                                    y_der: Math.round(rect.top + rect.height / 2)
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
                // Tabla de ventas base tiene ~9 celdas. El modal de detalle tiene muchas más.
                // También detectar por el titulo del modal 'Detalle de Producto'
                let tieneModal = false;
                for (const el of document.querySelectorAll('*')) {
                    const t = el.textContent.trim();
                    if (t === 'Detalle de Producto' && el.offsetParent !== null) {
                        tieneModal = true;
                        break;
                    }
                }
                return {ok: n > 50 || tieneModal, n: n, tieneModal: tieneModal};
            }
        """

        coords = await self.page.evaluate(JS_CELDA)
        if not coords:
            raise Exception("Celda 1974206 no encontrada")
        log.info(f"  Celda adyacente @ ({coords['x_der']}, {coords['y_der']})")

        modal_listo = False
        for intento in range(4):
            log.info(f"  Intento dblclick [{intento+1}/4]")
            await self.page.mouse.move(coords["x_der"], coords["y_der"])
            await asyncio.sleep(0.2)
            await self.page.mouse.dblclick(coords["x_der"], coords["y_der"], delay=100)
            await self._screenshot(f"paso5_dblclick_{intento+1}")

            log.info("  Esperando 12s para carga del modal...")
            await asyncio.sleep(12)

            estado = await self.page.evaluate(JS_MODAL_OK)
            log.info(f"  Celdas: {estado['n']} — modal={'✅' if estado['ok'] else '❌'}")
            if estado["ok"]:
                log.info("  ✅ Modal cargado")
                modal_listo = True
                break

            # Re-obtener coords por si la página cambió
            coords = await self.page.evaluate(JS_CELDA) or coords

        if not modal_listo:
            log.warning("  ⚠️ Modal no detectado — continuando igual")

        await self._screenshot("paso5_modal_abierto")
        log.info("Paso 5 completado")

    async def step6_click_boton_descarga(self):
        """
        Click en el botón ↓ azul del modal Detalle de Producto.
        Misma estrategia confirmada del RPA Inventario:
        buscar input con contenido largo → calcular x = input.right + 229, y = input.y
        """
        log.info("Paso 6: Click en botón ↓ del modal")
        await self._screenshot("paso6_antes")

        # Buscar el boton azul ↓ del modal de ventas.
        # El modal ocupa casi todo el viewport. El boton esta en la fila del
        # campo "Detalle para:" alineado a la derecha, antes del "..."
        # Confirmado por screenshot: esta en ~(1471, 139) en imagen 1536px
        # escalado a viewport 1280px: x≈1227, y≈130. Pero el modal puede
        # variar. Buscamos el bbr-popupbutton o v-button con x mas alto
        # dentro del modal (excluyendo botones de ayuda y cierre).
        coords = await self.page.evaluate("""
            () => {
                const vW = window.innerWidth;
                // Buscar todos los botones visibles en zona y=100-300
                const candidatos = [];
                for (const el of document.querySelectorAll(
                    '.bbr-popupbutton, .v-button, button'
                )) {
                    const cls = el.className || '';
                    if (cls.includes('help') || cls.includes('close')) continue;
                    const r = el.getBoundingClientRect();
                    const cx = Math.round(r.left + r.width / 2);
                    const cy = Math.round(r.top + r.height / 2);
                    if (r.width > 5 && r.height > 5 &&
                        cx > vW * 0.7 && cy > 80 && cy < 300) {
                        candidatos.push({x: cx, y: cy,
                            cls: cls.substring(0, 50)});
                    }
                }
                // Ordenar por y asc (el boton del modal esta mas arriba)
                // luego por x desc (mas a la derecha)
                candidatos.sort((a, b) => a.y - b.y || b.x - a.x);
                // Loguear todos
                return {
                    elegido: candidatos[0] || null,
                    todos: candidatos.map(b =>
                        '(' + b.x + ',' + b.y + ' ' + b.cls.split(' ')[0] + ')'
                    ).join(' | ')
                };
            }
        """)

        log.info(f"  Botones zona modal: {coords.get('todos', 'ninguno')}")
        btn = coords.get("elegido") if coords else None

        if btn and btn["x"] <= 1280:
            x, y = btn["x"], btn["y"]
            log.info(f"  → Botón ↓ @ ({x}, {y}) cls={btn['cls'][:40]}")
        else:
            # Fallback: esquina superior derecha del modal
            # Del screenshot: boton en x≈1471/1536*1280≈1227, y≈139/816*720≈123
            x, y = 1227, 140
            log.warning(f"  Usando coord fallback ({x}, {y})")

        await self.page.mouse.move(x, y)
        await asyncio.sleep(0.3)
        await self.page.mouse.click(x, y)

        await asyncio.sleep(2)
        await self._screenshot("paso6_post_click")
        log.info("Paso 6 completado")

    async def step7_seleccionar_formato(self):
        """
        Modal Formato de Descarga: CSV ya viene seleccionado por defecto.
        Solo hacer click en SELECCIONAR.
        """
        log.info("Paso 7: Click SELECCIONAR en modal Formato de Descarga")

        # Esperar modal hasta 8s
        for i in range(16):
            await asyncio.sleep(0.5)
            visible = await self.page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        const t = el.textContent.trim();
                        if (t.includes('Formato de Descarga') && el.offsetParent !== null)
                            return true;
                    }
                    return false;
                }
            """)
            if visible:
                log.info(f"  Modal visible [{i+1}]")
                break
        await self._screenshot("paso7_modal_formato")

        # Click SELECCIONAR
        coords = await self.page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('button, .v-button, .gwt-Button, span, div')) {
                    if (el.textContent.trim().toUpperCase() === 'SELECCIONAR') {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0 && r.left > 0) {
                            return {x: Math.round(r.left + r.width/2),
                                    y: Math.round(r.top + r.height/2)};
                        }
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
            log.warning("  SELECCIONAR no encontrado — coord fija (230, 511)")
            await self.page.mouse.move(230, 511)
            await asyncio.sleep(0.2)
            await self.page.mouse.click(230, 511)

        log.info("Paso 7 completado")

    async def step8_descargar_archivo(self):
        """
        Modal Descargar Archivo: click en link Ventas(detalleProducto)*.zip
        """
        log.info("Paso 8: Click en link de descarga")

        link_el = None
        for espera in range(20):
            await asyncio.sleep(0.5)
            link_el = await self.page.query_selector(
                "a[href*='Ventas'], a[href*='ventas'], a[href*='.zip'], a[href*='.xls']"
            )
            if link_el and await link_el.is_visible():
                texto = (await link_el.inner_text()).strip()
                href  = await link_el.get_attribute("href") or ""
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

    # ─────────────────────────────────────────────────────────────────────────

    async def run(self):
        result = {"success": False, "archivo_descargado": None, "error": None}
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=[
                    "--ignore-certificate-errors",
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ]
            )
            context = await browser.new_context(
                ignore_https_errors=True,
                viewport={"width": 1280, "height": 720},
                accept_downloads=True,
            )
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            self.page = await context.new_page()
            try:
                await self.step1_select_pais_y_unidad()
                ok_login = await self.step2_login()
                if not ok_login:
                    result["error"] = "Login fallido"
                    return result
                await self.step3_navegar_ventas()
                await self.step4_setear_fecha_y_generar()
                await self.step5_dobleclick_1974206()
                await self.step6_click_boton_descarga()
                await self.step7_seleccionar_formato()
                archivo = await self.step8_descargar_archivo()
                if archivo:
                    result["success"] = True
                    result["archivo_descargado"] = archivo
                    log.info(f"✅ RPA completado | Archivo: {archivo}")
                else:
                    result["error"] = "No se pudo descargar el archivo"
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
