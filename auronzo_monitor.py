"""
Auronzo / Tre Cime di Lavaredo pass availability monitor.

Polls https://pass.auronzo.info every N minutes and sends a Telegram message when:
  1. the calendar's max bookable date moves forward (new dates released), or
  2. a slot that was previously sold-out becomes available again, or
  3. a brand-new date appears with availability.

This is a monitor, not an auto-booker. Be polite with poll intervals.
"""

import os
import re
import json
import time
import logging
from datetime import date, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

PAGE_URL = "https://pass.auronzo.info/Frontoffice/Abbonamenti/Nuovo?taId=IIV7dhOwZ1Q6MHDwHH4Fyw%3d%3d"
SCHEDULER_URL = "https://pass.auronzo.info/Frontoffice/Abbonamenti/GetDurateScheduler"

PERMIT_TYPE_ID = 1
SECTOR_ID = 10  # PARCHEGGIO AUTO/MOTO

OUTER_INTERVAL_SECONDS = 15 * 60   # full sweep every 15 min
PER_DATE_SLEEP_SECONDS = 2.5       # gap between per-date requests; be polite
MAX_FAILURES_BEFORE_ALERT = 3      # consecutive sweep failures before alerting

STATE_FILE = "auronzo_state.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

USER_AGENT = "auronzo-monitor/1.0 (personal availability watcher)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("[TELEGRAM NOT CONFIGURED] %s", message)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram send failed: %r", exc)


# ----------------------------------------------------------------------------
# State persistence
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"last_max_date": None, "day_slots": {}, "failures": 0}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("day_slots", {})
    state.setdefault("failures", 0)
    return state


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ----------------------------------------------------------------------------
# Calendar range from the page HTML
# ----------------------------------------------------------------------------

# Kendo emits min/max as `new Date(y, m, d)` where month is 0-based.
_KENDO_DATE_RE = re.compile(
    r'"(min|max)"\s*:\s*new\s+Date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)',
    re.IGNORECASE,
)


def extract_calendar_range(html: str) -> tuple[date, date]:
    """Parse the bookable date range out of the page HTML."""
    bounds: dict[str, date] = {}
    for m in _KENDO_DATE_RE.finditer(html):
        kind, y, mo, d = m.group(1).lower(), int(m.group(2)), int(m.group(3)), int(m.group(4))
        bounds.setdefault(kind, date(y, mo + 1, d))  # JS month is 0-based

    if "min" in bounds and "max" in bounds:
        return bounds["min"], bounds["max"]

    raise RuntimeError("Could not find calendar min/max in page HTML")


# ----------------------------------------------------------------------------
# Scheduler parsing
# ----------------------------------------------------------------------------

_SLOT_PLACES_RE = re.compile(r"(\d+)\s*PLACES?\s*AVAILABLE", re.IGNORECASE)


def parse_day_slots(html: str) -> list[dict]:
    """Parse a GetDurateScheduler response into a list of slot dicts.

    Each slot is {'time': 'From 07:00', 'value': '-15', 'places': int, 'available': bool}.
    A slot is available iff its panel does NOT carry the
    `scheduler-disabled-heading` class. Place count is read from the label.
    """
    soup = BeautifulSoup(html, "html.parser")
    slots: list[dict] = []
    for panel in soup.select("div.panel.panel-heading"):
        classes = panel.get("class", [])
        disabled = "scheduler-disabled-heading" in classes
        label_el = panel.find("label")
        if label_el is None:
            continue
        text = label_el.get_text(" ", strip=True)
        time_part = text.split(" - ", 1)[0].strip()
        places = 0
        if not disabled:
            m = _SLOT_PLACES_RE.search(text)
            places = int(m.group(1)) if m else 0
        radio = panel.find("input", {"type": "radio"})
        value = radio["value"] if radio and radio.has_attr("value") else None
        slots.append({
            "time": time_part,
            "value": value,
            "places": places,
            "available": (not disabled) and places > 0,
        })
    return slots


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def build_session() -> requests.Session:
    """Create a session that persists cookies across requests within one sweep.

    Note on auth: the site currently requires NO login and sets NO cookies
    for the read-only browse endpoints we use (GetDurateScheduler and the
    page load). The taId in PAGE_URL identifies the tenant (Tre Cime di
    Lavaredo) and is a public slug, not a session token. If the site ever
    starts issuing a session cookie, the requests.Session() will pick it up
    from fetch_page() and carry it into fetch_day() automatically — we just
    need to keep calling fetch_page() before fetch_day() each sweep.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch_page(session: requests.Session) -> str:
    r = session.get(PAGE_URL, timeout=20)
    r.raise_for_status()
    return r.text


def fetch_day(session: requests.Session, day: date) -> list[dict]:
    params = {
        "customerId": "",
        "permitTypeId": PERMIT_TYPE_ID,
        "sectorId": SECTOR_ID,
        "selectedDate": day.isoformat(),
        "ctrlDurataId": "Validita_Durata_Id",
        "ctrlDataSelectionId": "schedulerDataSelection",
        "_": int(time.time() * 1000),  # cache buster, matches what jQuery sends
    }
    headers = {
        "Accept": "*/*",
        "Referer": PAGE_URL,
        "X-Requested-With": "XMLHttpRequest",
        # App-specific headers the real site sends. The endpoint currently
        # answers 200 without them, but sending them keeps us closer to a
        # real browser in case validation tightens. clientpath = referer path,
        # contextid = numeric tenant id (1 = Tre Cime di Lavaredo).
        "clientpath": "/Frontoffice/Abbonamenti/Nuovo?taId=IIV7dhOwZ1Q6MHDwHH4Fyw%3d%3d",
        "contextid": "1",
    }
    r = session.get(SCHEDULER_URL, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return parse_day_slots(r.text)


# ----------------------------------------------------------------------------
# Diff & alert
# ----------------------------------------------------------------------------

def slot_summary(slots: list[dict]) -> str:
    """Compact text summary of available slots, e.g. '07:00(3) 07:30(25)'."""
    avail = [s for s in slots if s["available"]]
    return " ".join(f"{s['time'].replace('From ', '')}({s['places']})" for s in avail)


def diff_and_alert(state: dict, day: date, new_slots: list[dict]) -> Optional[str]:
    """Compare today's slots to the snapshot in state. Returns alert text or None."""
    key = day.isoformat()
    old = state["day_slots"].get(key, {})  # {time: places}
    new = {s["time"]: s["places"] for s in new_slots if s["available"]}

    newly_available = []
    for t, places in new.items():
        if old.get(t, 0) == 0:
            newly_available.append(f"{t.replace('From ', '')} ({places} places)")

    # Persist new snapshot regardless of alert
    state["day_slots"][key] = new

    if not newly_available:
        return None

    return (
        f"🚨 New availability on {day.strftime('%a %d %b %Y')}:\n"
        + "\n".join(newly_available)
    )


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------

def daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def run_sweep(state: dict) -> None:
    session = build_session()
    html = fetch_page(session)
    start_d, max_d = extract_calendar_range(html)
    log.info("Calendar range: %s -> %s", start_d, max_d)

    prev_max = state.get("last_max_date")
    if prev_max and prev_max != max_d.isoformat():
        send_telegram(
            f"🚨 Auronzo calendar moved!\n"
            f"Previous max: {prev_max}\n"
            f"New max:      {max_d.isoformat()}\n\n"
            f"{PAGE_URL}"
        )
    state["last_max_date"] = max_d.isoformat()

    # Drop snapshots for dates that are no longer in range, to keep state tidy
    in_range = {d.isoformat() for d in daterange(start_d, max_d)}
    state["day_slots"] = {k: v for k, v in state["day_slots"].items() if k in in_range}

    alerts: list[str] = []
    for d in daterange(start_d, max_d):
        try:
            slots = fetch_day(session, d)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_day(%s) failed: %r", d, exc)
            time.sleep(PER_DATE_SLEEP_SECONDS)
            continue

        summary = slot_summary(slots) or "(none)"
        log.info("%s -> %s", d, summary)

        alert = diff_and_alert(state, d, slots)
        if alert:
            alerts.append(alert)

        time.sleep(PER_DATE_SLEEP_SECONDS)

    if alerts:
        send_telegram("\n\n".join(alerts) + f"\n\nBook: {PAGE_URL}")

    save_state(state)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Auronzo pass availability monitor")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sweep and exit (for cron / GitHub Actions). "
             "Without this flag, the script loops forever.",
    )
    args = parser.parse_args()

    state = load_state()

    def one_iteration() -> None:
        try:
            run_sweep(state)
            state["failures"] = 0
        except Exception as exc:  # noqa: BLE001
            log.exception("Sweep failed: %r", exc)
            state["failures"] = state.get("failures", 0) + 1
            if state["failures"] == MAX_FAILURES_BEFORE_ALERT:
                send_telegram(
                    f"⚠️ Auronzo monitor: {state['failures']} consecutive failures.\n"
                    f"Last error: {exc!r}"
                )
            save_state(state)

    if args.once:
        one_iteration()
        return

    while True:
        one_iteration()
        log.info("Sleeping %ds", OUTER_INTERVAL_SECONDS)
        time.sleep(OUTER_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
