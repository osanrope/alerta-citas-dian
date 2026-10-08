"""
Revisa si hay citas de DIAN (Persona natural > Videoatención > Devoluciones)
y avisa por Telegram (mensaje + foto + llamada) cuando las hay.

Variables de entorno (se configuran en citas.yml y en los Secrets de GitHub):
  TELEGRAM_TOKEN     token del bot (de @BotFather)
  TELEGRAM_CHAT_ID   tu número de chat
  TELEGRAM_USER      tu usuario de Telegram (@...) para la llamada de CallMeBot
  AVISAR_SIEMPRE=1   avisa también cuando NO hay citas (modo prueba)
  PROBAR_LLAMADA=true  solo hace una llamada de prueba y termina
  RECORDATORIO_MIN   minutos entre recordatorios mientras sigan las citas (5)
  LLAMADA_CADA_MIN   minutos entre llamadas mientras sigan las citas (15)
  MAX_LLAMADAS       llamadas máximas por cada aparición de citas (3)
  LLAMADAS=1         activa las llamadas (0 = solo mensajes)
  LATIDO_MIN         cada cuántos minutos enviar "sigo revisando, sin citas" (5; 0 = nunca)
  VER=1              abre el navegador visible, para probar en tu PC

Memoria entre ejecuciones: estado.json (GitHub lo guarda con actions/cache).
"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

URL = "https://agendamiento.dian.gov.co"
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TG_USER = (os.getenv("TELEGRAM_USER") or "").strip()
SIN_CITAS = re.compile(r"no se encontraron especialidades", re.I)
ESTADO = "estado.json"
BOGOTA = ZoneInfo("America/Bogota")

RECORDATORIO_MIN = float(os.getenv("RECORDATORIO_MIN") or 1)
LLAMADA_CADA_MIN = float(os.getenv("LLAMADA_CADA_MIN") or 15)
MAX_LLAMADAS = int(os.getenv("MAX_LLAMADAS") or 3)
LLAMADAS = os.getenv("LLAMADAS", "1") == "1"
LATIDO_MIN = float(os.getenv("LATIDO_MIN") or 5)
HOLGURA = 20  # segundos de tolerancia: las revisiones no caen exactas cada minuto
ERROR_AVISO_MIN = 30  # como máximo un aviso de error cada 30 min


# ---------------------------------------------------------------- utilidades
def hora(ts=None):
    return datetime.fromtimestamp(ts or time.time(), BOGOTA).strftime("%I:%M %p").lstrip("0")


def cargar_estado():
    try:
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_estado(e):
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(e, f, ensure_ascii=False, indent=2)


def avisar(texto):
    print(texto)
    if TOKEN and CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": texto},
                timeout=20,
            )
        except Exception as ex:
            print("No pude enviar el mensaje:", ex)


def enviar_foto(ruta, texto):
    """Envía una foto con texto; si falla, envía solo el texto."""
    print(texto)
    if not (TOKEN and CHAT_ID):
        return
    try:
        with open(ruta, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                data={"chat_id": CHAT_ID, "caption": texto[:1000]},
                files={"photo": f},
                timeout=30,
            )
        if r.ok:
            return
    except Exception as ex:
        print("No pude enviar la foto:", ex)
    avisar(texto)


def llamar(texto):
    """Llamada de voz por Telegram usando CallMeBot. Devuelve la respuesta del servicio."""
    if not TG_USER:
        return "Falta el secreto TELEGRAM_USER"
    usuario = TG_USER if TG_USER.startswith("@") else "@" + TG_USER
    url = (
        "https://api.callmebot.com/start.php?user=" + quote(usuario)
        + "&text=" + quote(texto) + "&lang=es-ES-Standard-A&rpt=3"
    )
    try:
        r = requests.get(url, timeout=90)
        resp = re.sub(r"<[^>]+>", " ", r.text)
        resp = re.sub(r"\s+", " ", resp).strip()[:300]
        print("CallMeBot:", r.status_code, resp)
        return resp
    except Exception as ex:
        print("CallMeBot falló:", ex)
        return f"error: {ex}"


def llamar_en_segundo_plano(texto):
    if not LLAMADAS:
        return None
    h = threading.Thread(target=llamar, args=(texto,), daemon=True)
    h.start()
    return h


def usuario_dijo_listo(desde_ts):
    """True si respondiste 'listo' u 'ok' al bot después de desde_ts."""
    if not (TOKEN and CHAT_ID):
        return False
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=20)
        for u in r.json().get("result", []):
            m = u.get("message") or {}
            if str(m.get("chat", {}).get("id")) != str(CHAT_ID):
                continue
            if m.get("date", 0) < desde_ts:
                continue
            if re.search(r"\b(listo|ok|ya|silencio)\b", m.get("text", ""), re.I):
                return True
    except Exception as ex:
        print("No pude leer respuestas:", ex)
    return False


# ---------------------------------------------------------------- navegador
def hay_visible(loc):
    for i in range(loc.count()):
        try:
            if loc.nth(i).is_visible():
                return True
        except Exception:
            pass
    return False


def clic(page, patron, paso, espera=45):
    """Clic en el primer elemento VISIBLE cuyo texto coincide (la página guarda copias ocultas)."""
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


def senales_de_citas(page):
    """Cuenta elementos visibles que indican que hay citas (selector de ciudad / trámite)."""
    n = 0
    for loc in (
        page.get_by_text(re.compile(r"ciudad", re.I)),
        page.get_by_text(re.compile(r"^\s*tr[aá]mite\s*:?\s*\*?\s*$", re.I)),
        page.locator("select, [role=combobox], [role=listbox]"),
    ):
        for i in range(loc.count()):
            try:
                if loc.nth(i).is_visible():
                    n += 1
            except Exception:
                pass
    return n


JS_TEXTOS_VISIBLES = r"""() => {
  const out = [];
  const vistos = new Set();
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const t = n.textContent.replace(/\s+/g, ' ').trim();
    if (!t || t.length > 150 || vistos.has(t)) continue;
    const el = n.parentElement;
    if (!el) continue;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none') {
      vistos.add(t); out.push(t);
    }
  }
  return out;
}"""

RELLENO = re.compile(r"^(seleccione|selecciona|--|tr[aá]mite\s*:?\s*\*?$)", re.I)


def leer_tramites(page):
    """Intenta leer las opciones de la lista 'Trámite'. Devuelve una lista de textos."""
    # 1) Si es un <select> normal, leer sus opciones directamente.
    try:
        sels = page.locator("select")
        opciones = []
        for i in range(sels.count()):
            s = sels.nth(i)
            if s.is_visible():
                for t in s.locator("option").all_inner_texts():
                    t = re.sub(r"\s+", " ", t).strip()
                    if t and not RELLENO.match(t) and t not in opciones:
                        opciones.append(t)
        if opciones:
            return opciones[:25]
    except Exception as ex:
        print("Lectura de <select> falló:", ex)

    # 2) Lista hecha a mano: abrirla y ver qué textos nuevos aparecen.
    try:
        antes = set(page.evaluate(JS_TEXTOS_VISIBLES))
        etiquetas = page.get_by_text(re.compile(r"^\s*tr[aá]mite\s*:?\s*\*?\s*$", re.I))
        for i in range(etiquetas.count()):
            et = etiquetas.nth(i)
            if not et.is_visible():
                continue
            destino = et.locator(
                "xpath=following::*[self::input or self::select or @role='combobox'"
                " or contains(@class,'select') or contains(@class,'combo')"
                " or contains(@class,'desplegable') or contains(@class,'lista')][1]"
            )
            try:
                if destino.count() and destino.first.is_visible():
                    destino.first.click(timeout=3_000)
                else:
                    et.click(timeout=3_000)
            except Exception:
                et.click(timeout=3_000)
            page.wait_for_timeout(1500)
            despues = page.evaluate(JS_TEXTOS_VISIBLES)
            nuevos = [t for t in despues if t not in antes and not RELLENO.match(t)]
            if nuevos:
                return nuevos[:25]
    except Exception as ex:
        print("Lectura de la lista falló:", ex)
    return []


def revisar():
    """Devuelve (estado, tramites, ruta_foto). estado: sin_citas | hay_citas | desconocido."""
    with sync_playwright() as p:
        nav = p.chromium.launch(headless=not os.getenv("VER"))
        page = nav.new_page(locale="es-CO", viewport={"width": 1280, "height": 900})
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
            clic(page, r"agendar\s*(una\s*)?cita", "Agendar cita")
            clic(page, r"persona\s*natural", "Persona natural")
            clic(page, r"video\s*-?\s*atenci[oó]n", "Videoatención")

            antes = senales_de_citas(page)
            clic(page, r"^\s*devoluciones\.?\s*$", "Devoluciones")

            fin = time.time() + 20
            while time.time() < fin:
                if hay_visible(page.get_by_text(SIN_CITAS)):
                    return "sin_citas", [], None
                if senales_de_citas(page) > antes:
                    page.wait_for_timeout(1000)
                    tramites = leer_tramites(page)
                    page.screenshot(path="citas.png", full_page=True)
                    return "hay_citas", tramites, "citas.png"
                page.wait_for_timeout(1000)

            page.screenshot(path="desconocido.png", full_page=True)
            return "desconocido", [], "desconocido.png"
        except Exception:
            page.screenshot(path="error.png", full_page=True)
            raise
        finally:
            nav.close()


# ---------------------------------------------------------------- decisión
def latido(est, ahora, ok):
    """Cuenta revisiones y cada LATIDO_MIN avisa que sigue funcionando sin citas."""
    if LATIDO_MIN <= 0:
        return
    if not est.get("latido_inicio"):
        est.update(latido_inicio=ahora, revisiones_ok=0, revisiones_fallidas=0)
    est["revisiones_ok" if ok else "revisiones_fallidas"] = \
        est.get("revisiones_ok" if ok else "revisiones_fallidas", 0) + 1
    if ahora - est["latido_inicio"] >= LATIDO_MIN * 60 - HOLGURA:
        total = est["revisiones_ok"] + est["revisiones_fallidas"]
        texto = (f"🟢 Sigo revisando. Verifiqué {total} veces entre las "
                 f"{hora(est['latido_inicio'])} y las {hora(ahora)}: no hubo citas.")
        if est["revisiones_fallidas"]:
            texto += f" ({est['revisiones_fallidas']} de esas revisiones no se pudieron completar.)"
        avisar(texto)
        est.update(latido_inicio=ahora, revisiones_ok=0, revisiones_fallidas=0)


def reiniciar_latido(est):
    for k in ("latido_inicio", "revisiones_ok", "revisiones_fallidas"):
        est.pop(k, None)


def manejar_problema(est, texto, ahora):
    """Errores: avisa en el 2.º fallo seguido y luego máximo cada 30 min."""
    latido(est, ahora, ok=False)
    est["errores_seguidos"] = est.get("errores_seguidos", 0) + 1
    ultimo = est.get("ultimo_aviso_error", 0)
    if est["errores_seguidos"] >= 2 and ahora - ultimo >= ERROR_AVISO_MIN * 60:
        avisar(f"{texto}\n(Fallos seguidos: {est['errores_seguidos']}. "
               f"No volveré a avisar de esto en {ERROR_AVISO_MIN} min.)")
        est["ultimo_aviso_error"] = ahora
    else:
        print(texto, "(aviso omitido para no repetir)")


def main():
    ahora = time.time()
    est = cargar_estado()

    if os.getenv("PROBAR_LLAMADA") == "true":
        resp = llamar("Esto es una prueba de la alerta de citas de la DIAN.")
        avisar(f"📞 Llamada de prueba enviada. Respuesta de CallMeBot:\n{resp}")
        return

    try:
        estado, tramites, foto = revisar()
    except Exception as ex:
        manejar_problema(est, f"⚠️ No pude revisar las citas de la DIAN: {ex}", ahora)
        guardar_estado(est)
        return

    if estado == "desconocido":
        manejar_problema(est, f"❓ La página de la DIAN mostró algo inesperado. Revisa a mano: {URL}", ahora)
        guardar_estado(est)
        return

    # La revisión funcionó: si venía fallando, avisar que se recuperó.
    if est.get("errores_seguidos", 0) >= 2 and est.get("ultimo_aviso_error"):
        avisar("👍 La revisión de la DIAN volvió a funcionar.")
    est["errores_seguidos"] = 0
    est.pop("ultimo_aviso_error", None)

    if estado == "sin_citas":
        if est.get("episodio_inicio"):
            minutos = round((ahora - est["episodio_inicio"]) / 60)
            avisar(f"🔚 Las citas de Devoluciones ya no aparecen. "
                   f"Estuvieron visibles unos {max(minutos, 1)} min "
                   f"(desde las {hora(est['episodio_inicio'])}).")
            for k in ("episodio_inicio", "ultimo_mensaje", "ultima_llamada", "llamadas", "silenciado"):
                est.pop(k, None)
        elif os.getenv("AVISAR_SIEMPRE") == "1":
            avisar("❌ Revisé la DIAN: por ahora NO hay citas de Devoluciones.")
        else:
            print("Sin citas por ahora.")
        latido(est, ahora, ok=True)
        guardar_estado(est)
        return

    # ---- Hay citas
    lista = "\n".join(f"• {t}" for t in tramites) if tramites else "(no pude leer la lista de trámites; mira la foto)"
    nuevo = not est.get("episodio_inicio")
    reiniciar_latido(est)

    if nuevo:
        est.update(episodio_inicio=ahora, ultimo_mensaje=ahora, ultima_llamada=ahora,
                   llamadas=1, silenciado=False)
        hilo = llamar_en_segundo_plano("Atención. Hay citas de devoluciones en la DIAN. Entra ya.")
        enviar_foto(foto, f"✅ ¡Hay citas de Devoluciones en la DIAN! Entra ya: {URL}\n\n"
                          f"Trámites:\n{lista}\n\n"
                          f"Responde «listo» para dejar de recibir recordatorios de estas citas.")
        guardar_estado(est)
        if hilo:
            hilo.join(timeout=90)
        return

    # Las citas siguen disponibles desde una revisión anterior.
    if not est.get("silenciado") and usuario_dijo_listo(est["episodio_inicio"]):
        est["silenciado"] = True
        avisar("🔕 Entendido. No te enviaré más recordatorios de estas citas. "
               "Te avisaré cuando se agoten o cuando aparezcan citas nuevas.")
    if est.get("silenciado"):
        print("Hay citas, pero el usuario silenció los recordatorios.")
        guardar_estado(est)
        return

    minutos = round((ahora - est["episodio_inicio"]) / 60)
    hilo = None
    if (LLAMADAS and est.get("llamadas", 0) < MAX_LLAMADAS
            and ahora - est.get("ultima_llamada", 0) >= LLAMADA_CADA_MIN * 60 - HOLGURA):
        est["llamadas"] = est.get("llamadas", 0) + 1
        est["ultima_llamada"] = ahora
        hilo = llamar_en_segundo_plano("Recordatorio. Siguen habiendo citas de devoluciones en la DIAN.")
    if ahora - est.get("ultimo_mensaje", 0) >= RECORDATORIO_MIN * 60 - HOLGURA:
        est["ultimo_mensaje"] = ahora
        avisar(f"⏰ Siguen las citas de Devoluciones (llevan unos {minutos} min). {URL}\n\n"
               f"Trámites:\n{lista}\n\nResponde «listo» para silenciar.")
    guardar_estado(est)
    if hilo:
        hilo.join(timeout=90)


if __name__ == "__main__":
    main()
    sys.exit(0)  # los problemas se avisan por Telegram; así GitHub no envía correos de fallo
