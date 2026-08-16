"""
PRO Trader — Always-On Scanning Worker
════════════════════════════════════════════════════════════════════════════
Runs the ACTUAL app (index.html) headless, so signal generation continues
independent of whether the phone has the app open. This is deliberately NOT
a reimplementation of the scoring engine — it drives a real Chromium tab
running your real, unmodified JS, so there is zero risk of the worker's
signals drifting from what the phone app itself would show. Same code,
same behaviour, just running on a schedule instead of only when you have
the app open.

WHAT THIS DOES NOT NEED:
  - Its own Kite login. /market, get_oi(), get_technicals() all pull Kite
    credentials from the server's own cache (populated whenever the PHONE
    logs into Kite) — confirmed directly in server.py. As long as you do
    your normal daily Kite login on the phone, this worker's data reads
    ride on that same cached session automatically.
  - A static IP. This worker only reads market/OI data and writes signals —
    it never places an order, and Zerodha's static-IP requirement applies
    specifically to order-placement calls (confirmed directly against
    Zerodha's own support docs).

WHAT IT DOES NEED:
  - Your phone to do its normal daily Kite login at least once each trading
    day. If the server restarts (a deploy, or a rare crash — not routine on
    a paid, always-on Render plan) before that happens, data reads will
    fail until you reconnect on the phone. That's expected, not a bug.

DEPLOY AS: a separate Render Background Worker service, same repo as
server.py, different Start Command. See DEPLOY.md in this folder.
"""

import time
import sys
import traceback
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

IST = timezone(timedelta(hours=5, minutes=30))

# ── CONFIG ───────────────────────────────────────────────────────────────
APP_URL = "https://saaps1947.github.io/Protrader/"
# Relaunch the browser periodically — long-lived headless Chromium sessions
# accumulate memory over many hours (growing SIGNAL_LOG/localStorage, DOM
# state, IndexedDB writes). A clean relaunch every few hours is cheap
# insurance against a slow OOM on a memory-constrained Render instance,
# and costs nothing functionally — the page reloads fresh and its own
# init sequence (startPolling, the 3-min scan timer, etc.) just starts over.
RESTART_EVERY_SECONDS = 4 * 60 * 60   # 4 hours
HEALTH_LOG_EVERY_SECONDS = 5 * 60     # print a heartbeat every 5 min


def log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S IST")
    print(f"[Worker {ts}] {msg}", flush=True)


def run_session():
    """
    One browser session: launch, navigate, keep alive with periodic health
    logging, until RESTART_EVERY_SECONDS elapses or something goes wrong.
    Returns normally on a clean scheduled restart; raises on a real failure
    so the outer loop can log it and retry.
    """
    with sync_playwright() as p:
        # Standard headless flags — nothing exotic, just what's needed to
        # run reliably in a constrained container (no GPU, no sandbox
        # issues under Render's build environment).
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",  # avoid /dev/shm size issues in containers
                "--no-sandbox",
            ],
        )
        try:
            page = browser.new_page(viewport={"width": 430, "height": 932})

            # Pipe the app's own console output into Render's logs — this is
            # the actual visibility into whether scans are firing, whether
            # /market calls are succeeding, etc. Filtered to skip pure noise.
            def on_console(msg):
                text = msg.text
                # Skip extremely chatty, low-value lines; keep everything
                # else so real problems are visible in Render's Logs tab.
                if any(skip in text for skip in ("Keepalive", "favicon")):
                    return
                log(f"[page] {text}")

            page.on("console", on_console)
            page.on("pageerror", lambda exc: log(f"[page error] {exc}"))

            log(f"Navigating to {APP_URL}")
            page.goto(APP_URL, wait_until="domcontentloaded", timeout=60000)
            log("Page loaded — app's own scan/poll timers are now running")

            session_start = time.time()
            last_health_log = 0

            while True:
                elapsed = time.time() - session_start
                if elapsed >= RESTART_EVERY_SECONDS:
                    log("Scheduled restart — relaunching for a clean session")
                    return  # clean exit, outer loop relaunches

                # Lightweight liveness check — confirms the page hasn't
                # silently died (e.g. renderer crash) between console events.
                if time.time() - last_health_log >= HEALTH_LOG_EVERY_SECONDS:
                    try:
                        stats = page.evaluate("""
                            () => ({
                                signals: (typeof allSigs!=='undefined') ? allSigs.length : -1,
                                journalOpen: (typeof SIGNAL_LOG!=='undefined')
                                    ? SIGNAL_LOG.filter(x=>x.status==='OPEN'||x.status==='T1_HIT').length
                                    : -1,
                                serverOk: (typeof serverOk!=='undefined') ? serverOk : null
                            })
                        """)
                        log(f"heartbeat — live signals: {stats['signals']}, "
                            f"open trades: {stats['journalOpen']}, "
                            f"server reachable: {stats['serverOk']}")
                    except Exception as e:
                        log(f"heartbeat check failed (page may have crashed): {e}")
                        raise  # let the outer loop relaunch
                    last_health_log = time.time()

                time.sleep(10)
        finally:
            browser.close()


def main():
    log("Starting always-on scanning worker")
    consecutive_failures = 0
    while True:
        try:
            run_session()
            consecutive_failures = 0  # clean scheduled restart, not a failure
        except Exception:
            consecutive_failures += 1
            log(f"Session crashed (attempt {consecutive_failures}):")
            traceback.print_exc()
            # Back off a bit longer after repeated failures, so a genuinely
            # broken deploy doesn't spin the CPU relaunching every second.
            backoff = min(30 * consecutive_failures, 300)
            log(f"Retrying in {backoff}s")
            time.sleep(backoff)


if __name__ == "__main__":
    main()
