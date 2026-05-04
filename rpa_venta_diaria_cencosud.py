"""
RPA - Cencosud Venta Diaria
============================
Flujo:
  1. Login (Chile + Supermercados + Keycloak SSO)
  2. Menú Comercial → Ventas
  3. Setear fecha AYER en ambos campos (Desde / Hasta)
  4. Click Generar Informe
  5. Doble click en celda adyacente a 1974206 → modal Detalle de Producto
  6. Click botón ↓ @ (1075, 190) CONFIRMADO por diagnóstico
  7. Click SELECCIONAR en modal Formato de Descarga
  8. Click link Ventas(detalleProducto)*.zip → descarga

COORDENADA CONFIRMADA: el botón ↓ azul del modal está en (1075, 190).
Identificado por script de diagnóstico que probó grilla sistemática de coordenadas.
El elemento es: DIV (1075, 192) 30x30 cls=v-button v-widget toolbar-button
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
    return (datetime.now() - timedelta(days=1)).strftime("%d-%m-%Y")


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

    async def step1_select_pais_y_unidad(self):
        log.info("Paso 1: Chile + Supermercados")
        await self.page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
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
        await btn.click()
        await self.page.wait_for_load_state("networkidle", timeout=20000)
        log.info(f"Paso 1 OK | URL: {self.page.url}")

    async def step2_login(self):
        log.info("Paso 2: Login")
        if _es_dashboard(self.page.url):
            log.info("✅ Sesión activa")
            return True
        try:
            await self.page.wait_for_selector("#kc-login", timeout=10000)
        except Exception:
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
        log.info(f"✅ Login OK")
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
        """
        1. Leer el campo HASTA — si no es ayer, crear fecha_no_disponible.txt y abortar.
        2. Si es ayer, setear DESDE también a ayer usando el input de texto.
        3. Click Generar Informe.
        """
        log.info("Paso 4: Verificar fecha HASTA y setear DESDE")
        import re
        fecha_ayer = _fecha_ayer()
        log.info(f"  Fecha esperada (ayer): {fecha_ayer}")
        await self._screenshot("paso4_antes")

        # Los campos de fecha en Vaadin son v-datefield — no son inputs HTML
        # estándar. El valor visible está en el textContent del campo de texto
        # interno (.v-datefield-textfield). Para escribir hay que hacer click
        # y usar el teclado (Ctrl+A + type).
        fechas = await self.page.evaluate("""
            () => {
                const campos = [];
                // v-datefield contiene un input interno con la fecha
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

        # Si no encontró con ese selector, buscar por texto con formato fecha
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

        # El panel muestra Desde (primero) y Hasta (segundo)
        fecha_hasta = fechas[1]["val"] if len(fechas) >= 2 else (fechas[0]["val"] if fechas else "")
        campo_desde = fechas[0] if fechas else None

        log.info(f"  Fecha HASTA leída: '{fecha_hasta}'")
        log.info(f"  Fecha ayer esperada: '{fecha_ayer}'")

        # Verificar HASTA
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

        # Setear DESDE a ayer.
        # Vaadin v-datefield bloquea edición directa via teclado.
        # Estrategia: usar JS para setear el value del input interno
        # y disparar los eventos que Vaadin necesita para actualizar su estado.
        if campo_desde:
            x, y = campo_desde["x"], campo_desde["y"]
            log.info(f"  Seteando DESDE '{campo_desde['val']}' → '{fecha_ayer}' @ ({x},{y})")

            # Método 1: JS directo en el input del v-datefield
            seteado = await self.page.evaluate(f"""
                () => {{
                    const inputs = document.querySelectorAll('.v-datefield input');
                    if (inputs.length === 0) return false;
                    const inp = inputs[0];  // primer campo = DESDE
                    inp.removeAttribute('readonly');
                    inp.removeAttribute('disabled');
                    inp.value = '{fecha_ayer}';
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    inp.dispatchEvent(new KeyboardEvent('keyup', {{bubbles: true, key: 'Enter'}}));
                    inp.blur();
                    return inp.value;
                }}
            """)
            log.info(f"  JS seteo DESDE resultado: {seteado}")
            await asyncio.sleep(0.5)

            # Método 2: click + seleccionar todo + escribir (backup)
            if seteado != fecha_ayer:
                log.info("  JS no funcionó — intentando click + teclado")
                await self.page.mouse.click(x, y)
                await asyncio.sleep(0.3)
                await self.page.mouse.click(x, y, click_count=3)
                await asyncio.sleep(0.2)
                await self.page.keyboard.type(fecha_ayer, delay=80)
                await self.page.keyboard.press("Tab")
                await asyncio.sleep(0.5)

            # Verificar
            fechas_post = await self.page.evaluate("""
                () => {
                    const vals = [];
                    for (const el of document.querySelectorAll('.v-datefield input, input')) {
                        const v = el.value || '';
                        if (v.match(/[0-9]{2}-[0-9]{2}-[0-9]{4}/)) vals.push(v);
                    }
                    return vals;
                }
            """)
            log.info(f"  Fechas tras setear DESDE: {fechas_post}")
            if fechas_post and fechas_post[0] == fecha_ayer:
                log.info(f"  ✅ DESDE seteado correctamente")
            else:
                log.warning(f"  ⚠️ DESDE puede no haberse seteado: {fechas_post}")

        await self._screenshot("paso4_fecha_seteada")
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

        coords = await self.page.evaluate(JS_CELDA)
        if not coords:
            raise Exception("Celda 1974206 no encontrada")
        log.info(f"  Celda adyacente @ ({coords['x']}, {coords['y']})")

        modal_listo = False
        for intento in range(4):
            log.info(f"  dblclick [{intento+1}/4]")
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

    async def step6_click_boton_descarga(self):
        """
        Click en botón ↓ (1075, 190) del modal → popup con opciones de descarga
        → click en primera opción del popup (Descargar).
        El botón ↓ abre un popup Vaadin en overlay — igual que el RPA Inventario.
        """
        log.info("Paso 6: Click en botón ↓ @ (1075, 190)")
        await self._screenshot("paso6_antes")

        x, y = 1075, 190
        await self.page.mouse.move(x, y)
        await asyncio.sleep(0.3)
        await self.page.mouse.click(x, y)
        # Esperar hasta 8s que aparezca el modal "Formato de Descarga"
        # El botón ↓ lo abre directamente sin popup intermedio
        formato_ok = False
        for i in range(16):
            await asyncio.sleep(0.5)
            await self._screenshot(f"paso6_espera_{i+1}")
            formato_ok = await self.page.evaluate("""
                () => {
                    for (const el of document.querySelectorAll('*')) {
                        if (el.textContent.trim().includes('Formato de Descarga') &&
                            el.offsetParent !== null) return true;
                    }
                    return false;
                }
            """)
            if formato_ok:
                log.info(f"  ✅ Formato de Descarga visible [{i+1}]")
                break
            log.info(f"  [{i+1}/16] Esperando Formato de Descarga...")

        if not formato_ok:
            log.warning("  Formato de Descarga no apareció tras 8s")

        log.info("Paso 6 completado")

    async def step7_seleccionar_formato(self):
        log.info("Paso 7: Click SELECCIONAR")

        # Esperar modal Formato de Descarga hasta 8s
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
                log.info(f"  Modal Formato de Descarga visible [{i+1}]")
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
            log.info(f"  SELECCIONAR ({coords['tag']}) @ ({coords['x']}, {coords['y']})")
            await self.page.mouse.move(coords["x"], coords["y"])
            await asyncio.sleep(0.2)
            await self.page.mouse.click(coords["x"], coords["y"])
        else:
            # Coordenada confirmada en run exitoso anterior: (575, 449)
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

    async def run(self):
        result = {"success": False, "archivo_descargado": None, "error": None}
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
                await self.step5_dobleclick_1974206()
                await self.step6_click_boton_descarga()
                await self.step7_seleccionar_formato()
                archivo = await self.step8_descargar_archivo()
                if archivo:
                    result["success"] = True
                    result["archivo_descargado"] = archivo
                    log.info(f"✅ RPA completado | {archivo}")
                else:
                    result["error"] = "Descarga fallida"
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
