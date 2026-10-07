"""
Revisa si hay citas de DIAN (Persona natural > Videoatención > Devoluciones)
y envía una alerta por Telegram cuando las hay.

Variables de entorno:
  TELEGRAM_TOKEN    token del bot (de @BotFather)
  TELEGRAM_CHAT_ID  tu chat id
  AVISAR_SIEMPRE=1  (opcional) avisa también cuando NO hay citas (modo prueba)
  VER=1             (opcional) abre el navegador visible, para probar en tu PC
"""
import os
import re
import sys
import time

import requests
from playwright.sync_api import sync_playwright

URL = "https://agendamiento.dian.gov.co"
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SIN_CITAS = re.compile(r"no se encontraron especialidades", re.I)


def avisar(texto):
    print(texto)
    if TOKEN and CHAT_ID:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": texto},
            timeout=20,
        )


def hay_visible(loc):
    """True si alguno de los elementos encontrados está visible en pantalla."""
    for i in range(loc.count()):
        try:
            if loc.nth(i).is_visible():
                return True
        except Exception:
            pass
    return False


def clic(page, patron, paso, espera=45):
    """Hace clic en el primer elemento VISIBLE cuyo texto coincide con el patrón.
    La página de la DIAN guarda copias ocultas de sus pantallas con los mismos
    textos, por eso se ignoran los elementos invisibles."""
    regex = re.compile(patron, re.I)
    fin = time.time() + espera
    while time.time() < fin:
        candidatos = page.get_by_text(regex)
        for i in range(candidatos.count()):
            c = candidatos.nth(i)
            try:
                if c.is_visible():
                    c.click(timeout=5_000)
                    page.wait_for_timeout(2000)
                    return
            except Exception:
                pass
        page.wait_for_timeout(1000)
    raise RuntimeError(f"no encontré el botón '{paso}' en la página")


def selectores_de_ciudad(page):
    """Cuenta elementos visibles que parecen el selector de ciudad."""
    n = 0
    for loc in (
        page.get_by_text(re.compile(r"ciudad", re.I)),
        page.locator("select, [role=combobox], [role=listbox]"),
    ):
        for i in range(loc.count()):
            if loc.nth(i).is_visible():
                n += 1
    return n


def revisar():
    with sync_playwright() as p:
        nav = p.chromium.launch(headless=not os.getenv("VER"))
        page = nav.new_page(locale="es-CO", viewport={"width": 1280, "height": 900})
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            clic(page, r"agendar\s*(una\s*)?cita", "Agendar cita")
            clic(page, r"persona\s*natural", "Persona natural")
            clic(page, r"video\s*-?\s*atenci[oó]n", "Videoatención")

            antes = selectores_de_ciudad(page)
            clic(page, r"^\s*devoluciones\.?\s*$", "Devoluciones")

            # Esperar hasta 20 s a que aparezca el modal (sin citas) o el selector de ciudad (hay citas)
            fin = time.time() + 20
            while time.time() < fin:
                if hay_visible(page.get_by_text(SIN_CITAS)):
                    return "sin_citas"
                if selectores_de_ciudad(page) > antes:
                    return "hay_citas"
                page.wait_for_timeout(1000)

            page.screenshot(path="desconocido.png", full_page=True)
            return "desconocido"
        except Exception:
            page.screenshot(path="error.png", full_page=True)
            raise
        finally:
            nav.close()


if __name__ == "__main__":
    try:
        estado = revisar()
    except Exception as e:
        avisar(f"⚠️ No pude revisar las citas de la DIAN: {e}")
        sys.exit(1)

    if estado == "hay_citas":
        avisar(f"✅ ¡Hay citas de Devoluciones en la DIAN! Entra ya: {URL}")
    elif estado == "desconocido":
        avisar(f"❓ La página de la DIAN mostró algo inesperado. Revisa a mano: {URL}")
    else:
        if os.getenv("AVISAR_SIEMPRE") == "1":
            avisar("❌ Revisé la DIAN: por ahora NO hay citas de Devoluciones.")
        else:
            print("Sin citas por ahora.")
