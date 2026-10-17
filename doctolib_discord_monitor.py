#!/usr/bin/env python3
"""Surveille les premiers rendez-vous Doctolib sans jamais sélectionner de créneau."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import signal
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.async_api import Browser, Locator, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
STATE_FILE = APP_DIR / "state.json"
DOCTOR_URL = "https://www.doctolib.fr/allergologue/laval/maria-daniela-aguilar-pardo"
DOCTOR_NAME = "Dr Maria Daniela Aguilar Pardo — Laval"

POSITIVE_PATTERNS = [
    re.compile(r"premi(?:er|ère)\s+(?:rendez[- ]?vous|consultation)", re.I),
    re.compile(r"nouve(?:au|lle)\s+patient(?:e)?", re.I),
    re.compile(r"premi(?:ère|ere)\s+fois", re.I),
    re.compile(r"jamais\s+consult", re.I),
]
EXCLUDED_RE = re.compile(r"suivi|contrôle|controle|renouvellement|déjà\s+suivi|deja\s+suivi", re.I)
QUESTION_RE = re.compile(r"(?:avez[- ]vous|as[- ]tu).*déjà\s+consult|déjà\s+consulté", re.I)
NO_RE = re.compile(r"^(?:non|non[, ]|jamais|premi(?:ère|ere)\s+fois)", re.I)
BOOK_RE = re.compile(r"prendre\s+(?:un\s+)?rendez[- ]?vous|réserver", re.I)
TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3])\s*(?::|h)\s*[0-5]\d$")
DATE_RE = re.compile(
    r"(?:lun(?:di)?|mar(?:di)?|mer(?:credi)?|jeu(?:di)?|ven(?:dredi)?|sam(?:edi)?|dim(?:anche)?)?\s*"
    r"\d{1,2}\s+(?:janv(?:ier)?|févr(?:ier)?|fevr(?:ier)?|mars|avr(?:il)?|mai|juin|juil(?:let)?|août|aout|sept(?:embre)?|oct(?:obre)?|nov(?:embre)?|déc(?:embre)?|dec(?:embre)?)"
    r"(?:\s+\d{4})?",
    re.I,
)
NO_AVAILABILITY_RE = re.compile(
    r"aucun(?:e)?\s+(?:créneau|creneau|disponibilité|disponibilite)|"
    r"pas\s+de\s+(?:créneau|creneau|disponibilité|disponibilite)|"
    r"n['’]a\s+pas\s+de\s+disponibilit|"
    r"prise\s+de\s+rendez[- ]?vous\s+en\s+ligne\s+n['’]est.{0,80}pas\s+disponible",
    re.I,
)
CHALLENGE_RE = re.compile(
    r"captcha|vérifions que vous êtes humain|verify you are human|access denied|"
    r"unusual traffic|activité inhabituelle|cloudflare|datadome|incident id|"
    r"veuillez patienter.*sécurité",
    re.I | re.S,
)


def log(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    print(f"[{stamp}] {message}", flush=True)


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return {**default, **loaded}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default.copy()


def save_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(STATE_FILE)


def config() -> dict[str, Any]:
    values = load_json(CONFIG_FILE, {})
    values.setdefault("discord_webhook_url", "")
    if os.environ.get("DISCORD_WEBHOOK_URL", "").strip():
        values["discord_webhook_url"] = os.environ["DISCORD_WEBHOOK_URL"].strip()
    values.setdefault("check_interval_seconds", 1200)
    values.setdefault("jitter_seconds", 300)
    values.setdefault("headless", True)
    return values


def save_webhook_from_stdin() -> int:
    webhook = sys.stdin.readline().strip()
    if not webhook.startswith("https://discord.com/api/webhooks/") and not webhook.startswith(
        "https://discordapp.com/api/webhooks/"
    ):
        raise RuntimeError("Le presse-papiers ne contient pas une URL de webhook Discord valide.")
    values = config()
    values["discord_webhook_url"] = webhook
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(CONFIG_FILE)
    log("Webhook Discord enregistré dans config.json (valeur masquée).")
    return 0


def send_discord(message: str, webhook: str) -> None:
    if not webhook.startswith("https://discord.com/api/webhooks/") and not webhook.startswith(
        "https://discordapp.com/api/webhooks/"
    ):
        raise RuntimeError("Webhook Discord absent ou invalide dans config.json.")
    payload = json.dumps({"content": message, "allowed_mentions": {"parse": []}}).encode("utf-8")
    request = Request(
        webhook,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "DoctolibMonitorMac/1.0"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"Discord a répondu HTTP {response.status}.")
    except HTTPError as exc:
        raise RuntimeError(f"Discord a répondu HTTP {exc.code}.") from None
    except URLError:
        raise RuntimeError("Discord est inaccessible (erreur réseau).") from None


async def dismiss_cookies(page: Page) -> None:
    for label in ("Tout refuser", "Refuser", "Continuer sans accepter", "Accepter et fermer", "Tout accepter"):
        try:
            button = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
            if await button.count() and await button.first.is_visible():
                await button.first.click(timeout=2500)
                await page.wait_for_timeout(400)
                return
        except Exception:
            continue


async def page_text(page: Page) -> str:
    return compact(await page.locator("body").inner_text(timeout=10000))


async def challenge_detected(page: Page) -> bool:
    text = await page_text(page)
    title = await page.title()
    return bool(CHALLENGE_RE.search(f"{title} {text}"))


async def click_first_visible(locator: Locator, timeout: int = 5000) -> bool:
    for index in range(min(await locator.count(), 12)):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible() and await candidate.is_enabled():
                await candidate.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


async def open_booking(page: Page) -> None:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        for role in ("button", "link"):
            if await click_first_visible(page.get_by_role(role, name=BOOK_RE)):
                await page.wait_for_timeout(1200)
                return
        # Sur certaines largeurs, le module de réservation est déjà ouvert.
        text = await page_text(page)
        if re.search(r"motif\s+de\s+consultation|choisissez.*motif|prenez\s+votre\s+rendez-vous\s+en\s+ligne", text, re.I):
            return
        if CHALLENGE_RE.search(text):
            raise RuntimeError("Challenge ou protection anti-robot détecté; aucun contournement tenté.")
        await page.wait_for_timeout(750)
    raise RuntimeError("Bouton « Prendre rendez-vous » introuvable.")


async def select_positive_option(page: Page) -> str | None:
    """Sélectionne uniquement un libellé explicitement premier rendez-vous/nouveau patient."""
    candidates: list[tuple[int, str, Locator]] = []
    locator = page.locator('button, [role="button"], [role="option"], [role="radio"], label, select')
    for index in range(min(await locator.count(), 450)):
        item = locator.nth(index)
        try:
            if not await item.is_visible():
                continue
            text = compact(await item.inner_text())
            aria = compact(await item.get_attribute("aria-label") or "")
            value = compact(await item.get_attribute("value") or "")
            combined = compact(" ".join((text, aria, value)))
            if not combined or len(combined) > 240 or EXCLUDED_RE.search(combined):
                continue
            if any(pattern.search(combined) for pattern in POSITIVE_PATTERNS):
                candidates.append((len(combined), combined, item))
        except Exception:
            continue
    if candidates:
        _, label, item = sorted(candidates, key=lambda row: row[0])[0]
        if (await item.evaluate("el => el.tagName")) == "SELECT":
            options = await item.locator("option").all()
            for option in options:
                option_text = compact(await option.inner_text())
                if not EXCLUDED_RE.search(option_text) and any(p.search(option_text) for p in POSITIVE_PATTERNS):
                    await item.select_option(value=await option.get_attribute("value"))
                    await page.wait_for_timeout(1200)
                    return option_text
        else:
            await item.click(timeout=5000)
            await page.wait_for_timeout(1200)
            return label
    return None


async def answer_new_patient_question(page: Page) -> bool:
    body = await page_text(page)
    if not QUESTION_RE.search(body):
        return False
    for role in ("button", "radio", "option"):
        if await click_first_visible(page.get_by_role(role, name=NO_RE)):
            await page.wait_for_timeout(1000)
            return True
    return False


async def choose_first_appointment(page: Page) -> str:
    # Attend aussi l'hydratation de l'interface; elle peut prendre plusieurs secondes.
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        if await challenge_detected(page):
            raise RuntimeError("Challenge ou protection anti-robot détecté; aucun contournement tenté.")
        selected = await select_positive_option(page)
        if selected:
            return selected
        if await answer_new_patient_question(page):
            selected = await select_positive_option(page)
            return selected or "Réponse NON à la question patient déjà suivi"

        # Ouvre prudemment les menus de motif sans choisir un choix ambigu.
        menu = page.get_by_role(
            "button", name=re.compile(r"motif\s+de\s+consultation|choisir.*motif|sélectionner.*motif", re.I)
        )
        if await click_first_visible(menu):
            await page.wait_for_timeout(500)
            continue
        await page.wait_for_timeout(750)
    raise RuntimeError("Aucun motif explicite de premier rendez-vous/nouveau patient n'a été identifié.")


async def slot_label(element: Locator) -> tuple[str, str] | None:
    raw = compact(await element.inner_text())
    aria = compact(await element.get_attribute("aria-label") or "")
    title = compact(await element.get_attribute("title") or "")
    attributes = []
    for name in ("data-date", "data-start-date", "data-test-id", "datetime", "href"):
        attributes.append(compact(await element.get_attribute(name) or ""))
    combined = compact(" ".join((raw, aria, title, *attributes)))
    match = TIME_RE.search(raw) or TIME_RE.search(aria) or TIME_RE.search(title)
    if not match:
        return None
    time_value = compact(match.group(0)).replace(":", "h").replace(" ", "")
    context = ""
    try:
        context = compact(
            await element.evaluate(
                """el => {
                  const datePattern = /\\b(?:lun|mar|mer|jeu|ven|sam|dim)[a-zéû]*\\.?\\s+\\d{1,2}\\s+(?:janv|févr|fevr|mars|avr|mai|juin|juil|août|aout|sept|oct|nov|déc|dec)[a-zéû]*/i;
                  let n = el;
                  for (let i = 0; i < 5 && n; i++, n = n.parentElement) {
                    const t = (n.innerText || '').replace(/\\s+/g, ' ').trim();
                    const attrs = [...(n.attributes || [])].map(a => `${a.name}=${a.value}`).join(' ');
                    if (datePattern.test(`${t} ${attrs}`)) return `${t} ${attrs}`.slice(0, 800);
                  }
                  const target = el.getBoundingClientRect();
                  const candidates = [...document.querySelectorAll('h1,h2,h3,h4,time,[aria-label],[data-test-id],[data-testid]')]
                    .map(node => ({
                      text: `${node.innerText || ''} ${node.getAttribute('aria-label') || ''} ${node.getAttribute('datetime') || ''}`.replace(/\\s+/g, ' ').trim(),
                      rect: node.getBoundingClientRect(),
                    }))
                    .filter(item => item.text.length < 160 && datePattern.test(item.text) && item.rect.top <= target.bottom)
                    .sort((a, b) => {
                      const score = item => Math.abs((item.rect.left + item.rect.right) / 2 - (target.left + target.right) / 2) * 3 + Math.abs(target.top - item.rect.bottom);
                      return score(a) - score(b);
                    });
                  if (candidates.length) return candidates[0].text;
                  return '';
                }"""
            )
        )
    except Exception:
        pass
    date_match = DATE_RE.search(f"{combined} {context}")
    date_value = compact(date_match.group(0)) if date_match else "date non affichée"
    return date_value, time_value


async def detect_slots(page: Page) -> list[dict[str, str]]:
    """Ne clique jamais les créneaux; accepte seulement des contrôles activés au libellé horaire."""
    controls = page.locator('button:not([disabled]), a[href], [role="button"]:not([aria-disabled="true"])')
    slots: list[dict[str, str]] = []
    for index in range(min(await controls.count(), 800)):
        element = controls.nth(index)
        try:
            if not await element.is_visible() or not await element.is_enabled():
                continue
            found = await slot_label(element)
            if found:
                date_value, time_value = found
                slots.append({"date": date_value, "time": time_value})
        except Exception:
            continue
    unique = {(slot["date"].casefold(), slot["time"]): slot for slot in slots}
    return sorted(unique.values(), key=lambda slot: (slot["date"].casefold(), slot["time"]))


async def wait_for_availability_state(
    page: Page, timeout_seconds: int = 20
) -> tuple[str, list[dict[str, str]]]:
    """Attend que le module affiche des créneaux ou un message d'indisponibilité.

    L'interface Doctolib est hydratée de façon asynchrone. Sur les runners
    GitHub, le message final peut apparaître plusieurs secondes après le clic
    sur le motif; une lecture immédiate produisait alors un faux état ambigu.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if await challenge_detected(page):
            raise RuntimeError("Challenge ou protection anti-robot détecté; aucun contournement tenté.")
        slots = await detect_slots(page)
        if slots:
            return "available", slots
        if NO_AVAILABILITY_RE.search(await page_text(page)):
            return "none", []
        await page.wait_for_timeout(750)
    return "unknown", []


async def save_diagnostic(page: Page, stem: str, events: list[str] | None = None) -> None:
    """Écrase un seul diagnostic par type afin de ne pas accumuler de fichiers."""
    output = APP_DIR / "output" / "playwright"
    output.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=output / f"{stem}.png", full_page=True)
    (output / f"{stem}.txt").write_text((await page_text(page))[:40000], encoding="utf-8")
    if events is not None:
        (output / f"{stem}-events.txt").write_text("\n".join(events[-300:]), encoding="utf-8")


@dataclass
class CheckResult:
    status: str
    slots: list[dict[str, str]]
    detail: str


async def check_once(settings: dict[str, Any], diagnostic: bool = False) -> CheckResult:
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(headless=bool(settings.get("headless", True)))
        context = await browser.new_context(
            locale="fr-FR", timezone_id="Europe/Paris", viewport={"width": 1440, "height": 1000}
        )
        page = await context.new_page()
        events: list[str] = []
        page.on("console", lambda message: events.append(f"console {message.type}: {message.text}"))
        page.on("pageerror", lambda error: events.append(f"pageerror: {error}"))
        page.on("requestfailed", lambda request: events.append(f"requestfailed: {request.method} {request.url} {request.failure}"))
        try:
            response = await page.goto(DOCTOR_URL, wait_until="domcontentloaded", timeout=45000)
            if response is None or response.status >= 400:
                raise RuntimeError(f"Chargement Doctolib impossible (HTTP {response.status if response else 'inconnu'}).")
            await page.wait_for_timeout(1800)
            await dismiss_cookies(page)
            if await challenge_detected(page):
                raise RuntimeError("Challenge ou protection anti-robot détecté; aucun contournement tenté.")
            await open_booking(page)
            selected = await choose_first_appointment(page)
            status, slots = await wait_for_availability_state(page)
            if diagnostic:
                await save_diagnostic(page, "diagnostic", events)
            if status == "available":
                return CheckResult("available", slots, selected)
            if status == "none":
                return CheckResult("none", [], selected)
            await save_diagnostic(page, "last-unknown", events)
            slot_api_blocked = any(
                "HTTP error 403" in event and "['slots'" in event for event in events
            )
            if slot_api_blocked:
                return CheckResult(
                    "blocked",
                    [],
                    "Doctolib refuse l'accès à l'API des créneaux depuis l'hébergement cloud (HTTP 403).",
                )
            return CheckResult("unknown", [], "Motif sélectionné, mais l'état des disponibilités reste ambigu.")
        except Exception:
            try:
                await save_diagnostic(page, "last-error", events)
            except Exception:
                pass
            raise
        finally:
            await context.close()
            await browser.close()


def default_state() -> dict[str, Any]:
    return {
        "available": False,
        "signature": "",
        "slots": [],
        "last_status": "unknown",
        "consecutive_errors": 0,
        "technical_alert_sent": False,
        "seen_slots": {},
    }


def slot_key(slot: dict[str, str]) -> str:
    return f"{compact(slot.get('date', '')).casefold()}|{compact(slot.get('time', '')).casefold()}"


def seen_slots_from_state(state: dict[str, Any]) -> dict[str, str]:
    raw = state.get("seen_slots", {})
    seen = raw.copy() if isinstance(raw, dict) else {}
    # Migration de l'ancien format : les créneaux présents dans l'état ont
    # déjà fait l'objet d'une alerte et ne doivent pas être renvoyés.
    for slot in state.get("slots", []):
        if isinstance(slot, dict):
            seen.setdefault(slot_key(slot), "historique")
    return seen


async def run_check(settings: dict[str, Any], diagnostic: bool = False) -> str:
    state = load_json(STATE_FILE, default_state())
    webhook = str(settings.get("discord_webhook_url", "")).strip()
    try:
        result = await check_once(settings, diagnostic=diagnostic)
    except PlaywrightTimeoutError:
        result = CheckResult("error", [], "Délai d'attente Doctolib dépassé.")
    except Exception as exc:
        # Le message ne contient jamais la configuration ni le webhook.
        result = CheckResult("error", [], compact(str(exc))[:300])

    if result.status == "error" and "HTTP 429" in result.detail:
        result.status = "rate_limited"

    if result.status == "available":
        canonical = json.dumps(result.slots, ensure_ascii=False, sort_keys=True)
        signature = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        seen_slots = seen_slots_from_state(state)
        new_slots = [slot for slot in result.slots if slot_key(slot) not in seen_slots]
        preview = ", ".join(f"{slot['date']} à {slot['time']}" for slot in result.slots[:12])
        new_preview = ", ".join(f"{slot['date']} à {slot['time']}" for slot in new_slots[:12])
        if new_slots:
            try:
                send_discord(
                    "🚨 **NOUVEAU CRÉNEAU DOCTOLIB — PREMIER RENDEZ-VOUS**\n\n"
                    f"**{DOCTOR_NAME}**\n\nNouveaux horaires détectés :\n**{new_preview}**\n\n"
                    f"Lien :\n{DOCTOR_URL}\n\n"
                    "Réserve rapidement : le programme ne réserve rien automatiquement.",
                    webhook,
                )
                detected_at = datetime.now().astimezone().isoformat()
                for slot in new_slots:
                    seen_slots[slot_key(slot)] = detected_at
                log(f"NOUVEAU — notification envoyée — {new_preview}")
            except Exception as exc:
                log(f"NOUVEAU, mais notification impossible — {compact(str(exc))}")
        else:
            log(f"Aucun nouveau créneau — créneaux déjà vus : {preview}")
        state.update(
            available=True, signature=signature, slots=result.slots, last_status="available",
            consecutive_errors=0, technical_alert_sent=False, retry_not_before_epoch=0,
            seen_slots=seen_slots, last_success_epoch=int(time.time()),
        )
    elif result.status == "none":
        log("Aucun créneau de premier rendez-vous.")
        state.update(
            available=False, signature="", slots=[], last_status="none",
            consecutive_errors=0, technical_alert_sent=False, retry_not_before_epoch=0,
            seen_slots=seen_slots_from_state(state), last_success_epoch=int(time.time()),
        )
    else:
        errors = min(3, int(state.get("consecutive_errors", 0)) + 1)
        log(f"Lecture non fiable ({result.status}, erreur {errors}) — {result.detail}")
        state.update(last_status=result.status, consecutive_errors=errors)
        if result.status == "rate_limited":
            state["retry_not_before_epoch"] = int(time.time()) + 7200
            log("Protection HTTP 429 : prochain essai différé de 2 heures pour laisser Doctolib lever la limitation.")
        elif result.status == "blocked":
            state["retry_not_before_epoch"] = int(time.time()) + 21600
            log("Blocage cloud HTTP 403 : prochain essai différé d'au moins 6 heures.")
        if (
            errors >= 3
            and result.status not in {"rate_limited", "blocked"}
            and not state.get("technical_alert_sent")
            and webhook
        ):
            try:
                if result.status == "blocked":
                    alert_detail = (
                        "Doctolib bloque l'accès aux créneaux depuis l'hébergement cloud. "
                        "La surveillance cloud ne peut donc pas confirmer les disponibilités."
                    )
                else:
                    alert_detail = (
                        "Le programme n'arrive plus à lire correctement la page Doctolib "
                        "depuis 3 vérifications consécutives."
                    )
                send_discord(
                    "⚠️ **Surveillance Doctolib à vérifier**\n\n"
                    f"{alert_detail}\n\n"
                    f"{DOCTOR_URL}",
                    webhook,
                )
                state["technical_alert_sent"] = True
                log("Alerte technique Discord envoyée une seule fois.")
            except Exception as exc:
                log(f"Alerte technique impossible — {compact(str(exc))}")
    save_state(state)
    return result.status


async def daemon(settings: dict[str, Any]) -> int:
    interval = max(60, int(settings.get("check_interval_seconds", 300)))
    jitter = max(0, min(300, int(settings.get("jitter_seconds", 60))))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log(f"Surveillance démarrée (intervalle {interval}s + jitter 0–{jitter}s).")
    while not stop.is_set():
        state = load_json(STATE_FILE, default_state())
        deferred_for = max(0, int(state.get("retry_not_before_epoch", 0)) - int(time.time()))
        if deferred_for:
            log(f"Recul de sécurité actif; reprise dans environ {deferred_for} secondes.")
            try:
                await asyncio.wait_for(stop.wait(), timeout=deferred_for)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
        await run_check(settings)
        latest = load_json(STATE_FILE, default_state())
        backoff = max(0, int(latest.get("retry_not_before_epoch", 0)) - int(time.time()))
        delay = max(interval + random.uniform(0, jitter), backoff)
        log(f"Prochaine vérification dans environ {round(delay)} secondes.")
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
    log("Surveillance arrêtée proprement.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true", help="Boucle persistante pour launchd")
    mode.add_argument("--test-discord", action="store_true", help="Envoie le message Discord de test")
    mode.add_argument("--configure-webhook-stdin", action="store_true", help="Lit le webhook sur l'entrée standard")
    parser.add_argument("--diagnostic", action="store_true", help="Conserve une capture et le texte de diagnostic")
    parser.add_argument("--headed", action="store_true", help="Affiche temporairement Chromium")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = config()
    if args.headed:
        settings["headless"] = False
    if args.test_discord:
        send_discord(
            "🟢 **Surveillance Doctolib activée**\n\n"
            f"{DOCTOR_NAME}\nSurveillance : premier rendez-vous uniquement.",
            str(settings.get("discord_webhook_url", "")).strip(),
        )
        log("Notification Discord de test envoyée.")
        return 0
    if args.configure_webhook_stdin:
        return save_webhook_from_stdin()
    if args.daemon:
        return asyncio.run(daemon(settings))
    state = load_json(STATE_FILE, default_state())
    deferred_for = max(0, int(state.get("retry_not_before_epoch", 0)) - int(time.time()))
    if deferred_for:
        log(f"Recul de sécurité actif; contrôle différé d'environ {deferred_for} secondes.")
        return 0
    status = asyncio.run(run_check(settings, diagnostic=args.diagnostic))
    return 0 if status in {"available", "none"} else 2


if __name__ == "__main__":
    sys.exit(main())
