"""
Script de diagnóstico para encontrar el botón ↓ del modal Detalle de Producto.

Estrategia:
1. Navega hasta el modal (mismo flujo que RPA Venta Diaria)
2. Prueba sistemáticamente coordenadas en la zona superior derecha del modal
3. Después de cada click verifica si apareció el modal "Formato de Descarga"
4. Guarda screenshot de cada intento
5. Al encontrar la coordenada correcta, la reporta en el log
"""

import asyncio
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

LOG_DIR = Path("logs")
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
log = logging.getLogger("Diagnostico")

BASE_URL = "https://www.cenconlineb2b.com/"


def _es_dashboard(url):
    return "cenconlineb2b.com" in url and "ssocencosud" not in url and "BBRe-commerce/main" in url


def _fecha_ayer():
    return (datetime.now() - timedelta(days=1)).strftime("%d-%m-%Y")


async def login_y_navegar(page, username, password):
    """Pasos 1-5: login + Comercial/Ventas + Generar Informe + doble click 1974206."""
    # Paso 1
    await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
    if not _es_dashboard(page.url):
        pais = await page.wait_for_selector("select", timeout=10000)
        await pais.select_option(label="Chile")
        await asyncio.sleep(1.5)
        selects = await page.query_selector_all("select")
        if len(selects) >= 2:
            await selects[1].select_option(label="Supermercados")
        btn = await page.wait_for_selector("#btnIngresar", timeout=8000)
        await btn.click()
        await page.wait_for_load_state("networkidle", timeout=20000)

    # Paso 2: login
    if not _es_dashboard(page.url):
        await page.wait_for_selector("#kc-login", timeout=10000)
        await page.evaluate("""
            ([u, p]) => {
                document.getElementById('username').value = u;
                document.getElementById('username').dispatchEvent(new Event('input', {bubbles:true}));
                document.getElementById('password').value = p;
                document.getElementById('password').dispatchEvent(new Event('input', {bubbles:true}));
                document.getElementById('kc-login').click();
            }
        """, [username, password])
        for _ in range(60):
            await asyncio.sleep(2)
            if _es_dashboard(page.url):
                break
        await page.wait_for_load_state("networkidle", timeout=30000)
    log.info(f"✅ Login OK")

    # Paso 3: Comercial → Ventas
    await asyncio.sleep(3)
    for ciclo in range(5):
        menus = await page.query_selector_all('.v-menubar-menuitem-caption')
        for m in menus:
            if await m.inner_text() == "Comercial":
                r = await m.bounding_box()
                await page.mouse.move(r["x"] + r["width"]/2, r["y"] + r["height"]/2)
                await asyncio.sleep(0.2)
                await page.mouse.click(r["x"] + r["width"]/2, r["y"] + r["height"]/2)
                break
        await asyncio.sleep(1.5)
        menus = await page.query_selector_all('.v-menubar-menuitem-caption')
        for m in menus:
            if await m.inner_text() == "Ventas":
                r = await m.bounding_box()
                await page.mouse.move(r["x"] + r["width"]/2, r["y"] + r["height"]/2)
                await asyncio.sleep(0.2)
                await page.mouse.click(r["x"] + r["width"]/2, r["y"] + r["height"]/2)
                break

        # Esperar "Generar Informe"
        for _ in range(20):
            await asyncio.sleep(2)
            ok = await page.evaluate("""
                () => [...document.querySelectorAll('*')]
                    .some(e => e.children.length===0 && e.textContent.trim()==='Generar Informe')
            """)
            if ok:
                log.info("✅ Ventas cargado")
                break
        else:
            continue
        break

    # Paso 4: Generar Informe (sin cambiar fecha para ahorrar tiempo)
    await asyncio.sleep(1)
    for _ in range(10):
        await asyncio.sleep(1)
        coords = await page.evaluate("""
            () => {
                for (const el of document.querySelectorAll('*')) {
                    if (el.children.length===0 && el.textContent.trim()==='Generar Informe') {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0) return {x: Math.round(r.left+r.width/2), y: Math.round(r.top+r.height/2)};
                    }
                }
                return null;
            }
        """)
        if coords:
            await page.mouse.move(coords["x"], coords["y"])
            await asyncio.sleep(0.3)
            await page.mouse.click(coords["x"], coords["y"])
            log.info(f"  Generar Informe @ {coords}")
            break
    await asyncio.sleep(5)

    # Paso 5: doble click en celda adyacente a 1974206
    for intento in range(4):
        coords = await page.evaluate("""
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
        """)
        if not coords:
            log.warning("Celda 1974206 no encontrada")
            break
        await page.mouse.move(coords["x"], coords["y"])
        await asyncio.sleep(0.2)
        await page.mouse.dblclick(coords["x"], coords["y"], delay=100)
        log.info(f"  dblclick [{intento+1}] @ ({coords['x']}, {coords['y']})")
        await asyncio.sleep(12)
        n = await page.evaluate("""
            () => document.querySelectorAll('.v-grid-body .v-grid-cell').length
        """)
        log.info(f"  Celdas: {n}")
        if n > 50:
            log.info("✅ Modal Detalle de Producto abierto")
            return True
        coords = await page.evaluate("""
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
                return None;
            }
        """) or coords

    log.warning("⚠️ Modal no abierto")
    return False


async def probar_coordenadas(page):
    """
    Prueba sistemáticamente coordenadas en la zona superior del modal.
    El modal ocupa todo el ancho. El botón ↓ está en la esquina superior derecha.
    Probamos una grilla de puntos en x=[900,1280] y=[80,200] cada 20px.
    """
    log.info("=" * 60)
    log.info("INICIO BÚSQUEDA DE BOTÓN ↓")
    log.info("=" * 60)

    JS_FORMATO_ABIERTO = """
        () => {
            for (const el of document.querySelectorAll('*')) {
                const t = el.textContent.trim();
                if ((t === 'Formato de Descarga' || t.includes('Seleccione un formato')) &&
                    el.offsetParent !== null) return true;
            }
            return false;
        }
    """

    # Tomar screenshot del estado inicial del modal
    await page.screenshot(
        path=str(LOG_DIR / "estado_inicial_modal.png"),
        full_page=True, timeout=8000
    )

    # Primero: volcar TODOS los elementos en zona y=80-220 con sus coords
    elementos = await page.evaluate("""
        () => {
            const res = [];
            for (const el of document.querySelectorAll('*')) {
                const r = el.getBoundingClientRect();
                const cy = r.top + r.height/2;
                const cx = r.left + r.width/2;
                if (cy > 80 && cy < 220 && cx > 800 &&
                    r.width > 5 && r.width < 300 && r.height > 5) {
                    res.push({
                        tag: el.tagName,
                        cls: el.className.substring(0, 60),
                        x: Math.round(cx),
                        y: Math.round(cy),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        title: el.title || '',
                        texto: el.textContent.trim().substring(0, 20)
                    });
                }
            }
            return res.sort((a,b) => b.x - a.x);
        }
    """)

    log.info(f"Elementos en zona x>800, y=80-220 ({len(elementos)} total):")
    for el in elementos[:30]:
        log.info(f"  {el['tag']:6} ({el['x']:4},{el['y']:4}) {el['w']}x{el['h']} "
                 f"cls={el['cls'][:40]:40} title={el['title'][:20]:20} txt={el['texto'][:15]}")

    # Grilla de coordenadas a probar
    xs = list(range(950, 1281, 25))   # cada 25px en x
    ys = list(range(85, 200, 15))     # cada 15px en y

    encontrado = None
    intento = 0

    for y in ys:
        for x in xs:
            intento += 1
            log.info(f"  [{intento:03d}] Probando ({x}, {y})...")

            # Guardar estado del DOM antes del click
            n_antes = await page.evaluate("""
                () => document.querySelectorAll('.v-grid-body .v-grid-cell').length
            """)

            # Click
            await page.mouse.move(x, y)
            await asyncio.sleep(0.1)
            await page.mouse.click(x, y)
            await asyncio.sleep(1.5)

            # Screenshot
            await page.screenshot(
                path=str(LOG_DIR / f"intento_{intento:03d}_x{x}_y{y}.png"),
                full_page=True, timeout=5000
            )

            # Verificar si abrió modal de formato
            formato_abierto = await page.evaluate(JS_FORMATO_ABIERTO)
            n_despues = await page.evaluate("""
                () => document.querySelectorAll('.v-grid-body .v-grid-cell').length
            """)

            if formato_abierto:
                log.info(f"  ✅✅✅ BOTÓN ENCONTRADO @ ({x}, {y}) ✅✅✅")
                encontrado = (x, y)
                await page.screenshot(
                    path=str(LOG_DIR / f"EXITO_x{x}_y{y}.png"),
                    full_page=True, timeout=8000
                )
                return encontrado

            # Si se cerró el modal (menos celdas), reabrirlo
            if n_despues < 50 and n_antes >= 50:
                log.warning(f"  ⚠️ Modal cerrado con click en ({x},{y}) — n={n_antes}→{n_despues}")
                # El modal se cerró — necesitamos volver a abrirlo
                # Esto es útil info: coords que cierran el modal
                log.info("  Reintentando doble click para reabrir modal...")
                # Hacer doble click en 1974206 de nuevo
                c = await page.evaluate("""
                    () => {
                        for (const el of document.querySelectorAll('td,span,div,a')) {
                            if (el.textContent.trim()==='1974206') {
                                const r = el.getBoundingClientRect();
                                if (r.width>0 && r.left>0) {
                                    const sig = el.nextElementSibling;
                                    const sr = sig ? sig.getBoundingClientRect() : null;
                                    return {
                                        x: sr ? Math.round(sr.left+sr.width/2) : Math.round(r.right+80),
                                        y: Math.round(r.top+r.height/2)
                                    };
                                }
                            }
                        }
                        return null;
                    }
                """)
                if c:
                    await page.mouse.move(c["x"], c["y"])
                    await asyncio.sleep(0.2)
                    await page.mouse.dblclick(c["x"], c["y"], delay=100)
                    log.info(f"  Esperando 12s para recargar modal...")
                    await asyncio.sleep(12)
                    n_check = await page.evaluate("""
                        () => document.querySelectorAll('.v-grid-body .v-grid-cell').length
                    """)
                    log.info(f"  Modal recargado: {n_check} celdas")
                    if n_check < 50:
                        log.error("  No se pudo reabrir el modal — abortando búsqueda")
                        return None
                # Saltar al siguiente y para no perder más tiempo en esta zona
                break

    if not encontrado:
        log.warning("⚠️ No se encontró el botón en la grilla probada")
    return encontrado


async def main():
    username = os.getenv("CENC_USER")
    password = os.getenv("CENC_PASS")
    headless = os.getenv("HEADLESS", "true").lower() == "true"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
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
        page = await context.new_page()

        try:
            modal_ok = await login_y_navegar(page, username, password)
            if not modal_ok:
                log.error("No se pudo abrir el modal — abortando")
                return

            resultado = await probar_coordenadas(page)
            if resultado:
                log.info(f"\n{'='*60}")
                log.info(f"RESULTADO: el botón ↓ está en ({resultado[0]}, {resultado[1]})")
                log.info(f"{'='*60}")
            else:
                log.info("No se encontró — revisar screenshots en logs_diag/")

        except Exception as e:
            log.error(f"Error: {e}")
            try:
                await page.screenshot(
                    path=str(LOG_DIR / "error.png"), timeout=5000
                )
            except Exception:
                pass
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
