import asyncio
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, Page, BrowserContext

from config import (
    LOGIN_URL, TICKET_URL,
    WEB_USER, WEB_PASS,
    SEDE_IDX, SERVICIO_IDX,
    MAX_CONCURRENT, HEADLESS,
)

logger = logging.getLogger(__name__)

_sem = asyncio.Semaphore(MAX_CONCURRENT)


# ── Text parsers ──────────────────────────────────────────────────────────────

def parse_message(text: str) -> tuple[str | None, int | None, int | None]:
    """
    Accepted formats:
        "385"             → (codigo, None, None)
        "385 nivel1 turno2" / "385 n2 t1" → (codigo, nivel, turno)
    """
    text = text.strip()
    m = re.match(
        r"^(\d+)\s+(?:nivel\s*|n\s*)(\d+)\s+(?:turno\s*|t\s*)(\d+)$",
        text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    if re.match(r"^\d+$", text):
        return text, None, None
    return None, None, None


def parse_button(text: str) -> dict:
    """Extracts nivel, turno, and disponibles from a ticket-slot button's label."""

    def find_int(patterns: list[str]) -> int | None:
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None

    nivel = find_int([r"nivel\D{0,5}(\d+)", r"\bN[°º]?\s*(\d+)\b"])
    turno = find_int([r"turno\D{0,5}(\d+)", r"\bT[°º]?\s*(\d+)\b"])
    disponibles = find_int([
        r"disp(?:onibles?)?\s*[:\-]?\s*(\d+)",
        r"cant(?:idad)?\s*[:\-]?\s*(\d+)",
        r"cupos?\s*[:\-]?\s*(\d+)",
        r"\(\s*(\d+)\s*\)",
        r"(\d+)\s*disp",
        r"(\d+)\s*cupo",
    ])
    if disponibles is None:
        nums = re.findall(r"\d+", text)
        disponibles = int(nums[-1]) if nums else 0

    return {"nivel": nivel, "turno": turno, "disponibles": disponibles}


# ── Playwright helpers ────────────────────────────────────────────────────────

async def _fill_first_visible(page: Page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            if await page.is_visible(sel, timeout=1_500):
                await page.fill(sel, value)
                return True
        except Exception:
            continue
    return False


async def _click_first_visible(page: Page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            if await page.is_visible(sel, timeout=1_500):
                await page.click(sel)
                return True
        except Exception:
            continue
    return False


async def _select_nth_option(page: Page, selectors: list[str], idx: int) -> bool:
    """
    Selects option at position `idx` in the first visible <select> and fires
    the 'change' event so the page's dynamic form logic responds.
    """
    for sel in selectors:
        try:
            if not await page.is_visible(sel, timeout=1_500):
                continue
            options = await page.query_selector_all(f"{sel} > option")
            if idx >= len(options):
                continue
            val = await options[idx].get_attribute("value")
            if val is not None:
                await page.select_option(sel, value=val)
            else:
                await page.evaluate(
                    f"var s = document.querySelector('{sel}');"
                    f"s.selectedIndex = {idx};"
                    f"s.dispatchEvent(new Event('change', {{bubbles: true}}));"
                )
            return True
        except Exception:
            continue
    return False


# ── Login ─────────────────────────────────────────────────────────────────────

async def _do_login(page: Page) -> bool:
    logger.info("[LOGIN] Navegando a: %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

    ok_user = await _fill_first_visible(page, ['input[name="login"]'], WEB_USER)
    ok_pass = await _fill_first_visible(page, ['input[name="clave"]'], WEB_PASS)

    if not ok_user or not ok_pass:
        logger.error("[LOGIN] Campos de credenciales no encontrados")
        return False

    if not await _click_first_visible(page, ['button.login']):
        await page.keyboard.press("Enter")

    await page.wait_for_load_state("networkidle", timeout=20_000)

    # The portal shows a "Si" button when there's already an active session
    if "SesionIniciada" in page.url or await page.is_visible('button:has-text("Si")', timeout=2_000):
        logger.info("[LOGIN] Sesión activa previa detectada – cerrando")
        await _click_first_visible(page, [
            'button:has-text("Si")',
            'input[type="button"][value="Si"]',
            'a:has-text("Si")',
        ])
        await page.wait_for_load_state("networkidle", timeout=15_000)

    logged_in = "login" not in page.url.lower()
    logger.info("[LOGIN] Resultado: %s | URL: %s", logged_in, page.url)
    return logged_in


# ── PDF download ──────────────────────────────────────────────────────────────

async def _fetch_pdf(url: str, cookies: dict, dest: str) -> bool:
    """Downloads `url` using the browser's session cookies and writes to `dest`."""
    try:
        async with httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            verify=False,  # university server uses an expired/self-signed cert
            timeout=20,
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.content) > 500:
                Path(dest).write_bytes(resp.content)
                return True
            logger.warning("HTTP %s descargando PDF: %s", resp.status_code, url)
    except httpx.TimeoutException:
        logger.warning("Timeout descargando PDF: %s", url)
    except httpx.RequestError as e:
        logger.warning("Error de red descargando PDF: %s", e)
    return False


# ── Ticket generation ─────────────────────────────────────────────────────────

async def generate_ticket(
    codigo: str,
    nivel_req: int | None = None,
    turno_req: int | None = None,
) -> dict:
    """
    Drives the university web portal to generate a meal ticket PDF.

    Returns:
        {"success": True,  "pdf_path": str, "ticket_info": str}
        {"success": False, "message": str}

    The caller is responsible for deleting `pdf_path` after use.
    """
    async with _sem:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=HEADLESS)
            ctx: BrowserContext = await browser.new_context(accept_downloads=True)
            page: Page = await ctx.new_page()
            try:
                if not await _do_login(page):
                    return {"success": False, "message": "Error al iniciar sesión en el sistema."}

                await page.goto(TICKET_URL, wait_until="networkidle", timeout=30_000)

                await _select_nth_option(page, [
                    'select[name="sede"]', 'select[name="Sede"]',
                    'select[id="sede"]',   'select[id="Sede"]',
                ], SEDE_IDX)
                await page.wait_for_timeout(700)

                await _select_nth_option(page, [
                    'select[name="servicio"]', 'select[name="Servicio"]',
                    'select[id="servicio"]',   'select[id="Servicio"]',
                ], SERVICIO_IDX)
                await page.wait_for_timeout(700)

                ok_cod = await _fill_first_visible(page, [
                    'input[name="Codigo"]', 'input[name="codigo"]',
                    'input[id="Codigo"]',   'input[id="codigo"]',
                    'input[placeholder*="odigo" i]',
                ], codigo)
                if not ok_cod:
                    return {"success": False, "message": "No se encontró el campo de código en la página."}

                if not await _click_first_visible(page, [
                    'button:has-text("Buscar")',     'button:has-text("Consultar")',
                    'button:has-text("Verificar")',  'button:has-text("Buscar Alumno")',
                    'input[type="button"][value*="uscar" i]',
                ]):
                    await page.keyboard.press("Enter")

                await page.wait_for_load_state("networkidle", timeout=15_000)
                await page.wait_for_timeout(1_200)

                # Collect ticket buttons; deduplicate by approximate screen position
                raw_handles = []
                for sel in [
                    '.btn-primary', '.btn-info', '.btn-success',
                    'a.btn-primary', 'a.btn-info', 'a.btn-success',
                    'button.btn-primary', 'button.btn-info',
                ]:
                    raw_handles.extend(await page.query_selector_all(sel))

                seen_pos, unique_handles = set(), []
                for h in raw_handles:
                    box = await h.bounding_box()
                    if box:
                        key = (round(box["x"] / 5), round(box["y"] / 5))
                        if key not in seen_pos:
                            seen_pos.add(key)
                            unique_handles.append(h)

                if not unique_handles:
                    return {
                        "success": False,
                        "message": (
                            f"No se encontraron tickets disponibles para el código *{codigo}*.\n"
                            "Verifica que el código sea correcto y que el alumno esté habilitado."
                        ),
                    }

                options = []
                for h in unique_handles:
                    raw_text = (await h.inner_text()).strip()
                    if raw_text:
                        info = parse_button(raw_text)
                        info["handle"] = h
                        info["text"]   = raw_text
                        options.append(info)

                logger.info(
                    "Código %s – opciones: %s",
                    codigo,
                    [f"N{o['nivel']}T{o['turno']}(disp={o['disponibles']})" for o in options],
                )

                if not options:
                    return {"success": False, "message": "No hay tickets disponibles."}

                # Prefer explicit slot request; fall back to highest availability
                selected = None
                if nivel_req is not None and turno_req is not None:
                    selected = next(
                        (o for o in options if o["nivel"] == nivel_req and o["turno"] == turno_req),
                        None,
                    )
                    if not selected:
                        lista = "\n".join(
                            f"  • Nivel {o['nivel']} – Turno {o['turno']} ({o['disponibles']} disponibles)"
                            for o in options
                        )
                        return {
                            "success": False,
                            "message": (
                                f"No hay disponibilidad para Nivel {nivel_req} / Turno {turno_req}.\n\n"
                                f"Opciones disponibles:\n{lista}"
                            ),
                        }
                else:
                    selected = max(options, key=lambda o: o["disponibles"])
                    if selected["disponibles"] == 0:
                        return {"success": False, "message": "Todos los cupos están agotados."}

                # --- PDF capture: three fallback strategies ---
                # Use a stable path in tempfile.gettempdir() so the caller can
                # read the file after this function returns (no tmpdir to race against).
                pdf_dest = os.path.join(
                    tempfile.gettempdir(),
                    f"ticket_{codigo}_{uuid.uuid4().hex[:8]}.pdf",
                )
                pdf_path: str | None = None

                download_ready  = asyncio.Event()
                download_holder = [None]
                new_page_holder = [None]
                new_page_ready  = asyncio.Event()

                async def _save_download(dl):
                    await dl.save_as(pdf_dest)
                    download_holder[0] = pdf_dest
                    download_ready.set()

                def _on_new_page(np):
                    new_page_holder[0] = np
                    new_page_ready.set()

                page.on("download", lambda dl: asyncio.create_task(_save_download(dl)))
                ctx.on("page", _on_new_page)

                await selected["handle"].click()

                # Strategy A: direct file download triggered by the click
                try:
                    await asyncio.wait_for(download_ready.wait(), timeout=10)
                    pdf_path = download_holder[0]
                    logger.info("PDF vía descarga directa: %s", pdf_path)
                except asyncio.TimeoutError:
                    pass

                # Strategy B: click opened PDF in a new tab
                if not pdf_path:
                    try:
                        await asyncio.wait_for(new_page_ready.wait(), timeout=5)
                        np = new_page_holder[0]
                        if np:
                            await np.wait_for_load_state("load", timeout=10_000)
                            pdf_url = np.url
                            logger.info("Nueva pestaña detectada: %s", pdf_url)
                            if pdf_url and pdf_url not in (TICKET_URL, LOGIN_URL, "about:blank"):
                                cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
                                if await _fetch_pdf(pdf_url, cookies, pdf_dest):
                                    pdf_path = pdf_dest
                            await np.close()
                    except asyncio.TimeoutError:
                        pass

                # Strategy C: PDF embedded in an <iframe> on the same page
                if not pdf_path:
                    try:
                        iframe_src = await page.eval_on_selector(
                            "iframe[src]", "el => el.src", timeout=3_000
                        )
                        if iframe_src and iframe_src not in (TICKET_URL, LOGIN_URL, ""):
                            logger.info("iframe con PDF detectado: %s", iframe_src)
                            cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
                            if await _fetch_pdf(iframe_src, cookies, pdf_dest):
                                pdf_path = pdf_dest
                    except Exception:
                        pass

                if pdf_path and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 500:
                    return {"success": True, "pdf_path": pdf_path, "ticket_info": selected["text"]}

                return {
                    "success": False,
                    "message": "El ticket fue seleccionado pero no se pudo obtener el PDF.\nIntenta de nuevo.",
                }

            except Exception as exc:
                logger.exception("Error inesperado generando ticket para código %s", codigo)
                return {"success": False, "message": f"Error inesperado: {exc}"}

            finally:
                await ctx.close()
                await browser.close()
