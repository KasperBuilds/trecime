#!/usr/bin/env python3
"""
Real Madrid Ticket Availability Monitor
========================================
Watches https://www.realmadrid.com/es-ES/entradas for availability changes
and sends Telegram alerts when matches flip from unavailable to available.

Uses Playwright with a headful Firefox browser (running via Xvfb on Railway)
to load the page normally and intercept the native matches API response.
This completely bypasses Akamai Bot Manager because the real site's JavaScript
generates all the correct telemetry and cookies.

To save memory on Railway (which has only 500MB RAM), it opens the browser,
intercepts the API, and completely closes the browser every poll cycle.
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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================================
# Config
# ============================================================================

# Matches to watch — filter by opponent name substring and/or date substring.
# Empty list = watch ALL matches.
WATCH_LIST: list[dict] = []

# Polling — slow and jittered to be polite
POLL_INTERVAL_SECONDS = 120       # base interval
JITTER_MIN_SECONDS = 15
JITTER_MAX_SECONDS = 45

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Real Madrid
TICKETS_URL = "https://www.realmadrid.com/es-ES/entradas"

# State
STATE_FILE = Path(__file__).parent / "rm_monitor_state.json"

# Robustness
BACKOFF_BASE = 30
BACKOFF_MAX = 300
BACKOFF_MULTIPLIER = 2
FAILURE_ALERT_THRESHOLD = 5

# Heartbeat
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

        opponent = ""
        for key in ("awayTeam", "visitorTeam"):
            if key in item and isinstance(item[key], dict):
                opponent = item[key].get("name") or item[key].get("shortName") or ""
                break
        opponent = opponent or item.get("title") or ""

        match_date = item.get("date") or item.get("matchDate") or item.get("startDate") or ""

        competition = ""
        if isinstance(item.get("competition"), dict):
            competition = item["competition"].get("name", "")
        elif item.get("competition"):
            competition = str(item["competition"])

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
# Fetch matches via Playwright
# ============================================================================

def fetch_matches_with_playwright() -> Optional[list[dict]]:
    """
    Launch a local headful Firefox via Playwright.
    Navigate to the page, intercept the native matches API call,
    extract the JSON, and close the browser.
    Returns: list of matches, or None on network/interception error.
    """
    matches_data = None

    def on_response(response):
        nonlocal matches_data
        if "rm-ms-match" in response.url:
            log.info("RM API Response: %s %s", response.status, response.url)
        if "/rm-ms-match-prd/api/v1/matches" in response.url and response.request.method == "GET":
            try:
                matches_data = response.json()
                log.info("Successfully intercepted matches JSON.")
            except Exception as e:
                log.warning("Failed to parse intercepted JSON: %r", e)

    with sync_playwright() as p:
        # headless=False is REQUIRED for Akamai, but it runs fine in Railway because of Xvfb
        browser = p.firefox.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
        )
        page = context.new_page()
        page.on("response", on_response)

        try:
            page.goto(TICKETS_URL, timeout=45000)
            
            # Dismiss the cookie banner which blocks the API fetch
            try:
                accept_btn = page.locator("button:has-text('Aceptar todas')")
                accept_btn.wait_for(state="visible", timeout=5000)
                accept_btn.click()
                log.info("Clicked 'Aceptar todas' cookie button.")
            except PlaywrightTimeoutError:
                pass
                
            # Wait up to 30 seconds for the API call to complete
            for _ in range(30):
                if matches_data is not None:
                    break
                page.wait_for_timeout(1000)
            
            if matches_data is None:
                log.warning("matches_data is still None. Taking debug screenshot...")
                try:
                    page.screenshot(path="debug_rm.png")
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                        data={"chat_id": TELEGRAM_CHAT_ID, "caption": "RM Debug Screenshot (failed to intercept)"},
                        files={"photo": open("debug_rm.png", "rb")},
                        timeout=15
                    )
                except Exception as e:
                    log.error("Screenshot error: %s", e)

        except Exception as e:
            log.warning("Playwright navigation error: %r", e)
        finally:
            browser.close()

    if matches_data is None:
        return None  # Failed to intercept or load
    
    return parse_matches(matches_data)

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

    # Offset start to avoid colliding with auronzo_monitor on boot
    log.info("Offsetting start by 30s to let Auronzo monitor boot first...")
    time.sleep(30)

    try:
        while running:
            try:
                matches = fetch_matches_with_playwright()
            except Exception as exc:
                matches = None
                log.error("fetch_matches failed: %r", exc, exc_info=True)

            if matches is None:
                consecutive_failures += 1
                backoff = min(
                    BACKOFF_BASE * (BACKOFF_MULTIPLIER ** (consecutive_failures - 1)),
                    BACKOFF_MAX,
                )
                log.warning(
                    "Fetch failed (failure #%d). Backing off %.0fs.",
                    consecutive_failures, backoff,
                )
                if consecutive_failures == FAILURE_ALERT_THRESHOLD:
                    send_telegram(
                        f"⚠️ RM Monitor: {consecutive_failures} consecutive fetch failures.\n"
                        f"Possibly blocked by Akamai or site changed."
                    )
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
        log.info("Monitor stopped.")

if __name__ == "__main__":
    main()
