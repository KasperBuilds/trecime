"""
Auronzo / Tre Cime di Lavaredo pass availability monitor.

Polls https://pass.auronzo.info every N minutes and sends a Telegram message when:
  1. the calendar's max bookable date moves forward (new dates released), or
  2. a slot that was previously sold-out becomes available again, or
  3. a brand-new date appears with availability.

Uses camofox-browser (Camoufox) to bypass Cloudflare protection.
Requires camofox-browser running at CAMOFOX_URL (default http://localhost:9377).

This is a monitor, not an auto-booker. Be polite with poll intervals.
"""

import os
import re
import json
import time
import logging
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlencode

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

CAMOFOX_URL = os.getenv("CAMOFOX_URL", "http://localhost:9377")
CAMOFOX_USER_ID = "auronzo-monitor"

# How long to wait for Cloudflare challenge + page render (ms)
CAMOFOX_PAGE_LOAD_WAIT_MS = 8000
# How long to wait for scheduler AJAX response (ms)
CAMOFOX_FETCH_TIMEOUT_MS = 15000

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
    for match in _KENDO_DATE_RE.finditer(html):
        kind, y, mo, d = match.group(1).lower(), int(match.group(2)), int(match.group(3)), int(match.group(4))
        bounds.setdefault(kind, date(y, mo + 1, d))  # JS month is 0-based

    if "min" in bounds and "max" in bounds:
        return bounds["min"], bounds["max"]

    # Fallback to hidden inputs: id="Tipo_ValiditaScheduler_DataMinima" value="6/1/2026 12:00:00 AM"
    min_re = re.search(r'id="Tipo_ValiditaScheduler_DataMinima"[^>]*value="(\d{1,2})/(\d{1,2})/(\d{4})', html)
    max_re = re.search(r'id="Tipo_ValiditaScheduler_DataMassima"[^>]*value="(\d{1,2})/(\d{1,2})/(\d{4})', html)
    
    if min_re and max_re:
        d, m, y = map(int, min_re.groups())
        min_date = date(y, m, d)
        d, m, y = map(int, max_re.groups())
        max_date = date(y, m, d)
        
        # The user wants to start scanning from July 1 to avoid wasting time on empty days
        floor_date = date(y, 7, 1)
        if min_date < floor_date:
            min_date = floor_date

        return min_date, max_date

    # Log a snippet of the HTML for debugging if regex fails
    snippet = html[:2000] if len(html) > 2000 else html
    log.error("Calendar range extraction failed. HTML snippet (first 2000 chars):\n%s", snippet)
    raise RuntimeError("Could not find calendar min/max in page HTML")


# ----------------------------------------------------------------------------
# Scheduler parsing
# ----------------------------------------------------------------------------

_SLOT_PLACES_RE = re.compile(r"(\d+)\s*(?:POST[IO]\s*DISPONIBIL[EI]|PLACES?\s*AVAILABLE)", re.IGNORECASE)


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
# Camofox browser session (REST API client)
# ----------------------------------------------------------------------------

class CamofoxSession:
    """Wraps camofox-browser REST API calls for browser automation."""

    def __init__(self, base_url: str = CAMOFOX_URL, user_id: str = CAMOFOX_USER_ID):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.tab_id: Optional[str] = None
        self._http = requests.Session()
        self._http.headers.update({"Content-Type": "application/json"})

    def health_check(self) -> bool:
        """Check if camofox-browser is running and healthy."""
        try:
            r = self._http.get(f"{self.base_url}/health", timeout=5)
            data = r.json()
            return data.get("ok", False)
        except Exception as exc:
            log.error("Camofox health check failed: %r", exc)
            return False

    def open_tab(self, url: str) -> str:
        """Open a new browser tab at the given URL. Returns the tab ID."""
        r = self._http.post(
            f"{self.base_url}/tabs",
            json={"userId": self.user_id, "sessionKey": "monitor", "url": url},
            timeout=60,  # First load may need to pass Cloudflare challenge
        )
        if not r.ok:
            log.error("Failed to open tab: %s", r.text)
        r.raise_for_status()
        data = r.json()
        self.tab_id = data["tabId"]
        log.info("Opened tab %s at %s", self.tab_id, url)
        return self.tab_id

    def execute_js(self, script: str, tab_id: Optional[str] = None) -> str:
        """Execute JavaScript in the browser tab and return the result."""
        tid = tab_id or self.tab_id
        if not tid:
            raise RuntimeError("No tab open — call open_tab() first")
        r = self._http.post(
            f"{self.base_url}/tabs/{tid}/evaluate",
            json={"userId": self.user_id, "sessionKey": "monitor", "expression": script},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("result", "")

    def navigate(self, url: str, tab_id: Optional[str] = None) -> None:
        """Navigate an existing tab to a new URL."""
        tid = tab_id or self.tab_id
        if not tid:
            raise RuntimeError("No tab open — call open_tab() first")
        r = self._http.post(
            f"{self.base_url}/tabs/{tid}/navigate",
            json={"url": url},
            timeout=60,
        )
        r.raise_for_status()
        log.info("Navigated tab %s to %s", tid, url)

    def get_snapshot(self, tab_id: Optional[str] = None) -> dict:
        """Get an accessibility snapshot of the current page."""
        tid = tab_id or self.tab_id
        if not tid:
            raise RuntimeError("No tab open — call open_tab() first")
        r = self._http.get(
            f"{self.base_url}/tabs/{tid}/snapshot",
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def close_tab(self, tab_id: Optional[str] = None) -> None:
        """Close a browser tab."""
        tid = tab_id or self.tab_id
        if not tid:
            return
        try:
            self._http.delete(
                f"{self.base_url}/tabs/{tid}",
                timeout=10,
            )
            log.info("Closed tab %s", tid)
        except Exception as exc:
            log.warning("Failed to close tab %s: %r", tid, exc)
        if tid == self.tab_id:
            self.tab_id = None

    def close_session(self) -> None:
        """Close the entire browser session (all tabs for this user)."""
        try:
            self._http.delete(
                f"{self.base_url}/sessions/{self.user_id}",
                timeout=10,
            )
            log.info("Closed session for user %s", self.user_id)
        except Exception as exc:
            log.warning("Failed to close session: %r", exc)
        self.tab_id = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close_session()


# ----------------------------------------------------------------------------
# Browser-based page fetching
# ----------------------------------------------------------------------------

def fetch_page(session: CamofoxSession) -> str:
    """Load the booking page in a real browser and return its HTML.

    This handles Cloudflare challenges automatically — the Camoufox browser
    passes TLS fingerprinting, solves Turnstile, and follows redirects.
    """
    # Open the page — camofox will wait for initial load
    session.open_tab(PAGE_URL)

    # Give Cloudflare challenge + Kendo widget time to initialize
    # The browser handles challenges internally; we just need to wait for
    # the page to fully render including JS-generated content
    time.sleep(CAMOFOX_PAGE_LOAD_WAIT_MS / 1000)

    # Extract the full page HTML via JS execution
    html = session.execute_js("document.documentElement.outerHTML")

    if not html or len(html) < 500:
        log.warning("Page HTML seems too short (%d chars), might be a challenge page", len(html))

    return html


def _build_scheduler_params(day: date) -> str:
    """Build the query string for GetDurateScheduler, matching what the Kendo widget sends."""
    params = {
        "customerId": "",
        "permitTypeId": PERMIT_TYPE_ID,
        "sectorId": SECTOR_ID,
        "selectedDate": day.isoformat(),
        "ctrlDurataId": "Validita_Durata_Id",
        "ctrlDataSelectionId": "schedulerDataSelection",
        "_": str(int(time.time() * 1000)),
    }
    return urlencode(params)


def _build_fetch_js(day: date, csrf_token: Optional[str] = None) -> str:
    """Build a JS fetch() call to get scheduler data from within the browser.

    Running fetch() inside the browser means:
    - Cloudflare clearance cookies are automatically included
    - Same-origin policy is satisfied
    - Any CSRF tokens in cookies are sent
    - TLS fingerprint matches (it's the same browser)
    """
    query_string = _build_scheduler_params(day)
    url = f"{SCHEDULER_URL}?{query_string}"

    # Build headers matching what the real page's jQuery.ajax sends
    headers_obj = {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "clientpath": "/Frontoffice/Abbonamenti/Nuovo?taId=IIV7dhOwZ1Q6MHDwHH4Fyw%3d%3d",
        "contextid": "1",
    }

    # If we found a CSRF / anti-forgery token, include it
    if csrf_token:
        headers_obj["__RequestVerificationToken"] = csrf_token

    headers_json = json.dumps(headers_obj)

    return f"""
    (async () => {{
        try {{
            const resp = await fetch("{url}", {{
                method: "GET",
                headers: {headers_json},
                credentials: "same-origin"
            }});
            if (!resp.ok) {{
                return JSON.stringify({{error: true, status: resp.status, statusText: resp.statusText}});
            }}
            const text = await resp.text();
            return text;
        }} catch (e) {{
            return JSON.stringify({{error: true, message: e.message}});
        }}
    }})()
    """


def _extract_csrf_token(html: str) -> Optional[str]:
    """Try to extract an ASP.NET anti-forgery token from the page HTML.

    The Auronzo site likely uses ASP.NET MVC which embeds __RequestVerificationToken
    as a hidden input in forms. If present, we'll include it in scheduler requests.
    """
    soup = BeautifulSoup(html, "html.parser")
    token_input = soup.find("input", {"name": "__RequestVerificationToken"})
    if token_input and token_input.get("value"):
        log.info("Found CSRF token in page HTML")
        return token_input["value"]

    # Also check meta tags (some ASP.NET configs put it there)
    meta = soup.find("meta", {"name": "csrf-token"})
    if meta and meta.get("content"):
        log.info("Found CSRF token in meta tag")
        return meta["content"]

    log.info("No CSRF token found in page (probably not needed)")
    return None


def fetch_day(session: CamofoxSession, day: date, csrf_token: Optional[str] = None) -> list[dict]:
    """Fetch scheduler slots for a specific day using in-browser fetch().

    This runs a JS fetch() call inside the already-open browser tab,
    which inherits all cookies, Cloudflare clearance, and session state.
    """
    js_code = _build_fetch_js(day, csrf_token)
    result = session.execute_js(js_code)

    # Check if the result is an error JSON
    if result and result.startswith("{"):
        try:
            err_data = json.loads(result)
            if err_data.get("error"):
                raise RuntimeError(
                    f"Scheduler fetch failed for {day}: "
                    f"status={err_data.get('status')} {err_data.get('statusText', err_data.get('message', ''))}"
                )
        except json.JSONDecodeError:
            pass  # Not JSON, treat as HTML response

    return parse_day_slots(result)


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
    with CamofoxSession() as session:
        # Health check — fail fast if camofox isn't running
        if not session.health_check():
            raise RuntimeError(
                f"Camofox-browser is not running at {CAMOFOX_URL}. "
                f"Start it with: cd camofox-browser && npm start"
            )

        # Load the booking page in the browser
        html = fetch_page(session)
        start_d, max_d = extract_calendar_range(html)
        log.info("Calendar range: %s -> %s", start_d, max_d)

        # Extract CSRF token if present (defensive — likely not needed)
        csrf_token = _extract_csrf_token(html)

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
                slots = fetch_day(session, d, csrf_token)
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
