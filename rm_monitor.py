#!/usr/bin/env python3
"""
Real Madrid Ticket Availability Monitor
========================================
Watches https://www.realmadrid.com/es-ES/entradas for availability changes
and sends Telegram alerts when matches flip from unavailable to available.

Uses the Camofox browser server (same instance as auronzo_monitor) to load
the page in a real browser, bypassing Akamai Bot Manager legitimately.
The matches API data is captured by executing a fetch() inside the browser
tab after the page has loaded (so Akamai cookies are already set).

Designed to run alongside auronzo_monitor.py on Railway.
"""

import os
import json
import time
import signal
import random
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# ============================================================================
# Config
# ============================================================================

# Matches to watch — filter by opponent name substring and/or date substring.
# Empty list = watch ALL matches.
# Examples:
#   {"opponent": "Barcelona"}
#   {"date": "2026-09"}
#   {"opponent": "Atlético", "date": "2026-10"}
WATCH_LIST: list[dict] = []

# Polling — slow and jittered to be polite
POLL_INTERVAL_SECONDS = 120       # base interval
JITTER_MIN_SECONDS = 15
JITTER_MAX_SECONDS = 45

# Telegram (reuses the same env vars as auronzo_monitor)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Camofox browser server (shared with auronzo_monitor)
CAMOFOX_URL = os.getenv("CAMOFOX_URL", "http://localhost:9377")
CAMOFOX_USER_ID = "rm-monitor"

# Real Madrid
TICKETS_URL = "https://www.realmadrid.com/es-ES/entradas"
MATCHES_API = "https://api-narm.realmadrid.com/rm-ms-match-prd/api/v1/matches"
API_KEY = "d48dd6e08e6c4ba086ba161047afb976"

# State
STATE_FILE = Path(__file__).parent / "rm_monitor_state.json"

# Robustness
BACKOFF_BASE = 30
BACKOFF_MAX = 300
BACKOFF_MULTIPLIER = 2
FAILURE_ALERT_THRESHOLD = 5

# Heartbeat — send a status summary to Telegram every N hours
HEARTBEAT_INTERVAL_HOURS = 6

# ============================================================================
# Logging
# ============================================================================

logging.basicConfig(
    format="%(asctime)s %(levelname)s [RM] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rm_monitor")

# ============================================================================
# Telegram
# ============================================================================

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
    except Exception as exc:
        log.warning("Telegram send failed: %r", exc)

# ============================================================================
# State
# ============================================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"matches": {}, "last_check": None}


def save_state(state: dict) -> None:
    state["last_check"] = datetime.utcnow().isoformat()
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )

# ============================================================================
# Camofox browser session (reuses the same pattern as auronzo_monitor)
# ============================================================================

class CamofoxSession:
    def __init__(self):
        self.base_url = CAMOFOX_URL.rstrip("/")
        self.user_id = CAMOFOX_USER_ID
        self.tab_id: Optional[str] = None
        self._http = requests.Session()
        self._http.headers.update({"Content-Type": "application/json"})

    def health_check(self) -> bool:
        try:
            r = self._http.get(f"{self.base_url}/health", timeout=5)
            return r.json().get("ok", False)
        except Exception:
            return False

    def open_tab(self, url: str) -> str:
        r = self._http.post(
            f"{self.base_url}/tabs",
            json={"userId": self.user_id, "sessionKey": "rm-monitor", "url": url},
            timeout=60,
        )
        r.raise_for_status()
        self.tab_id = r.json()["tabId"]
        log.info("Opened tab %s at %s", self.tab_id, url)
        return self.tab_id

    def execute_js(self, script: str) -> str:
        if not self.tab_id:
            raise RuntimeError("No tab open")
        r = self._http.post(
            f"{self.base_url}/tabs/{self.tab_id}/evaluate",
            json={
                "userId": self.user_id,
                "sessionKey": "rm-monitor",
                "expression": script,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("result", "")

    def navigate(self, url: str) -> None:
        if not self.tab_id:
            raise RuntimeError("No tab open")
        r = self._http.post(
            f"{self.base_url}/tabs/{self.tab_id}/navigate",
            json={"url": url},
            timeout=60,
        )
        r.raise_for_status()

    def close_session(self) -> None:
        try:
            self._http.delete(
                f"{self.base_url}/sessions/{self.user_id}", timeout=10
            )
            log.info("Closed session")
        except Exception as exc:
            log.warning("Failed to close session: %r", exc)
        self.tab_id = None

# ============================================================================
# Match filtering
# ============================================================================

def match_passes_filter(match_info: dict) -> bool:
    if not WATCH_LIST:
        return True
    for crit in WATCH_LIST:
        opp_f = crit.get("opponent", "").lower()
        date_f = crit.get("date", "").lower()
        opp_ok = not opp_f or opp_f in (match_info.get("opponent") or "").lower()
        date_ok = not date_f or date_f in (match_info.get("date") or "").lower()
        if opp_ok and date_ok:
            return True
    return False

# ============================================================================
# Parse matches from API JSON
# ============================================================================

def parse_matches(api_data) -> list[dict]:
    matches = []
    items = api_data if isinstance(api_data, list) else (
        api_data.get("data") or api_data.get("matches") or api_data.get("items") or []
    )
    if isinstance(items, dict):
        items = [items]

    for item in items:
        if not isinstance(item, dict):
            continue

        mid = str(item.get("id") or item.get("matchId") or item.get("externalId") or "")
        if not mid:
            continue

        # Opponent
        opponent = ""
        for key in ("awayTeam", "visitorTeam"):
            if key in item and isinstance(item[key], dict):
                opponent = item[key].get("name") or item[key].get("shortName") or ""
                break
        opponent = opponent or item.get("title") or ""

        # Date
        match_date = item.get("date") or item.get("matchDate") or item.get("startDate") or ""

        # Competition
        competition = ""
        if isinstance(item.get("competition"), dict):
            competition = item["competition"].get("name", "")
        elif item.get("competition"):
            competition = str(item["competition"])

        # Availability
        available = False
        detail = "unknown"

        if item.get("soldOut"):
            detail = "SOLD OUT"
        elif "aforo" in item:
            af = item["aforo"]
            if isinstance(af, dict):
                available = bool(af.get("available") or af.get("isAvailable"))
                detail = "available" if available else "sold out"
            else:
                available = bool(af)
                detail = str(af)
        elif "ticketAvailability" in item:
            ta = str(item["ticketAvailability"]).upper()
            available = ta not in ("SOLD_OUT", "NOT_AVAILABLE", "UNAVAILABLE")
            detail = item["ticketAvailability"]
        elif "isAvailable" in item:
            available = bool(item["isAvailable"])
            detail = "available" if available else "not available"

        # Price
        price_parts = []
        for pk in ("fromPrice", "fromPriceVIP", "fromPriceGeneral"):
            if item.get(pk):
                price_parts.append(f"€{item[pk]}")
                if not available:
                    available = True
                    detail = f"from €{item[pk]}"
        price_info = " / ".join(price_parts)

        info = {
            "id": mid,
            "opponent": opponent,
            "date": match_date,
            "competition": competition,
            "available": available,
            "detail": detail,
            "price": price_info,
        }
        if match_passes_filter(info):
            matches.append(info)

    return matches

# ============================================================================
# Fetch matches via browser (bypasses Akamai)
# ============================================================================

JS_FETCH_MATCHES = """
(async () => {
    try {
        const resp = await fetch("%s", {
            headers: {
                "Ocp-Apim-Subscription-Key": "%s",
                "Accept": "application/json"
            }
        });
        if (!resp.ok) return JSON.stringify({error: resp.status});
        const data = await resp.json();
        return JSON.stringify(data);
    } catch (e) {
        return JSON.stringify({error: e.message});
    }
})()
""" % (MATCHES_API, API_KEY)


def fetch_matches(session: CamofoxSession) -> list[dict]:
    """
    Load the RM tickets page in the browser (warms Akamai cookies),
    then fetch the matches API from inside the browser context.
    """
    # Navigate to the tickets page first to get Akamai cookies
    if session.tab_id is None:
        session.open_tab(TICKETS_URL)
        time.sleep(5)  # let Akamai sensor JS run
    else:
        session.navigate(TICKETS_URL)
        time.sleep(5)

    # Now fetch the API from inside the browser
    raw = session.execute_js(JS_FETCH_MATCHES)
    if not raw:
        log.warning("Empty JS result from fetch")
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Failed to parse fetch result: %.200s", raw)
        return []

    if "error" in data:
        log.warning("Fetch returned error: %s", data["error"])
        return []

    return parse_matches(data)

# ============================================================================
# Transition detection
# ============================================================================

def check_transitions(state: dict, matches: list[dict]) -> list[str]:
    alerts = []
    stored = state.setdefault("matches", {})

    for m in matches:
        mid = m["id"]
        was_available = stored.get(mid, {}).get("available", False)
        now_available = m["available"]

        if now_available and not was_available:
            msg = (
                f"⚽ TICKETS AVAILABLE!\n"
                f"🏟️ Real Madrid vs {m['opponent']}\n"
                f"📅 {m['date']}\n"
                f"🏆 {m['competition']}\n"
                f"🎫 {m['detail']}"
            )
            if m["price"]:
                msg += f"\n💰 {m['price']}"
            msg += f"\n\n🔗 {TICKETS_URL}"
            alerts.append(msg)

        stored[mid] = {
            "opponent": m["opponent"],
            "date": m["date"],
            "competition": m["competition"],
            "available": now_available,
            "detail": m["detail"],
            "last_seen": datetime.utcnow().isoformat(),
        }

    return alerts

# ============================================================================
# Main loop
# ============================================================================

def main():
    state = load_state()
    consecutive_failures = 0
    running = True
    last_heartbeat = time.time()
    total_polls = 0

    def shutdown(sig, frame):
        nonlocal running
        log.info("Shutdown signal received")
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    session = CamofoxSession()

    # Wait for the shared Camofox server to be ready
    for attempt in range(30):
        if session.health_check():
            break
        log.info("Waiting for Camofox server... (%d/30)", attempt + 1)
        time.sleep(2)
    else:
        log.error("Camofox server not available after 60s")
        return

    try:
        while running:
            try:
                matches = fetch_matches(session)
            except Exception as exc:
                matches = []
                log.error("fetch_matches failed: %r", exc, exc_info=True)

            if not matches:
                consecutive_failures += 1
                backoff = min(
                    BACKOFF_BASE * (BACKOFF_MULTIPLIER ** (consecutive_failures - 1)),
                    BACKOFF_MAX,
                )
                log.warning(
                    "No matches found (failure #%d). Backing off %.0fs.",
                    consecutive_failures, backoff,
                )
                if consecutive_failures >= FAILURE_ALERT_THRESHOLD:
                    send_telegram(
                        f"⚠️ RM Monitor: {consecutive_failures} consecutive failures.\n"
                        f"Possibly blocked by Akamai or site changed."
                    )
                # Close session to reset cookies
                session.close_session()
                time.sleep(backoff)
                continue

            consecutive_failures = 0
            avail = sum(1 for m in matches if m["available"])
            log.info("Found %d matches (%d available)", len(matches), avail)
            for m in matches:
                icon = "✅" if m["available"] else "❌"
                d = m["date"][:10] if len(m["date"]) >= 10 else m["date"]
                log.info("  %s %s vs %s (%s) %s %s", icon, d, m["opponent"],
                         m["competition"], m["detail"], m["price"])

            alerts = check_transitions(state, matches)
            for a in alerts:
                log.info("[ALERT] %s", a)
                send_telegram(a)

            save_state(state)
            total_polls += 1

            # Periodic heartbeat
            hours_since = (time.time() - last_heartbeat) / 3600
            if hours_since >= HEARTBEAT_INTERVAL_HOURS:
                avail_list = [m for m in matches if m["available"]]
                hb = (
                    f"💓 RM Monitor heartbeat\n"
                    f"⏱️ Uptime: {hours_since:.1f}h | Polls: {total_polls}\n"
                    f"📊 {len(matches)} matches tracked, {len(avail_list)} available"
                )
                if avail_list:
                    hb += "\n" + "\n".join(
                        f"  ✅ {m['opponent']} ({m['date'][:10]})" for m in avail_list[:5]
                    )
                send_telegram(hb)
                last_heartbeat = time.time()

            # Jittered sleep
            jitter = random.uniform(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS)
            sleep_time = POLL_INTERVAL_SECONDS + jitter
            log.info("Sleeping %.0fs", sleep_time)

            deadline = time.time() + sleep_time
            while running and time.time() < deadline:
                time.sleep(1)

    finally:
        save_state(state)
        session.close_session()
        log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
