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

import os
import time
import sys
import traceback
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

IST = timezone(timedelta(hours=5, minutes=30))

# ── CONFIG ───────────────────────────────────────────────────────────────
APP_URL = "https://saaps1947.github.io/Protrader/"
# BUG FIX: the app reads its backend URL from localStorage.getItem("pt_server"),
# set once via the phone's Settings screen. That's fine for a real browser
# with persistent history — but this worker launches a brand-new, empty
# Chromium profile every session, with no localStorage carried over from
# anywhere. Without this, SERVER_URL silently stays "" forever, the page
# loads but can never reach the backend, and every heartbeat reports
# "server reachable: False" — confirmed exactly this in production logs.
# Injected into localStorage before the page's own scripts run, same key
# the app already reads — zero changes needed to index.html itself.
SERVER_URL = os.environ.get("PROTRADER_SERVER_URL", "https://protrader-server.onrender.com")
# Relaunch the browser periodically — long-lived headless Chromium sessions
# accumulate memory over many hours (growing SIGNAL_LOG/localStorage, DOM
# state, IndexedDB writes). A clean relaunch every few hours is cheap
# insurance against a slow OOM on a memory-constrained Render instance,
# and costs nothing functionally — the page reloads fresh and its own
# init sequence (startPolling, the 3-min scan timer, etc.) just starts over.
#
# FIX: was 4 hours — Render's own OOM-restart email confirmed the 512MB
# limit was actually being hit before this window elapsed. Shortened as a
# safety net alongside the resource-blocking above, not a replacement for
# it — restarting more often just means the page re-inits a bit more
# often, which costs a brief pause, not lost state (nothing persists
# between sessions in a fresh headless profile anyway).
RESTART_EVERY_SECONDS = 90 * 60   # 90 minutes
HEALTH_LOG_EVERY_SECONDS = 5 * 60     # print a heartbeat every 5 min
# The app's own init sequence fires its first /market call essentially
# immediately on load, but the ACTUAL network round-trip that flips
# serverOk=true takes real time — confirmed in production logs: the
# heartbeat fired ~1s after "Page loaded" and reported server reachable:
# False, even though SERVER_URL was correctly set by then. That's a race
# in this timing, not a real connectivity problem. First check now waits
# long enough to give that fetch a fair chance to actually complete.
FIRST_HEALTH_CHECK_DELAY_SECONDS = 25


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

            # FIX: Render's OOM-restart email confirmed headless Chromium
            # was exceeding the worker's 512MB memory limit. This app's
            # scoring/signal logic runs entirely on JSON fetch responses and
            # JS/DOM text state — nothing about it depends on images, fonts,
            # or media actually rendering, since no human ever looks at this
            # browser's pixels. Blocking those resource types at the network
            # level is a real, meaningful memory reduction with zero
            # functional risk for a headless-only browser. Does NOT block
            # stylesheets or scripts — those could plausibly affect JS
            # behavior indirectly, not worth the risk for a smaller saving.
            def _block_heavy_resources(route):
                if route.request.resource_type in ("image", "font", "media"):
                    route.abort()
                else:
                    route.continue_()
            page.route("**/*", _block_heavy_resources)

            # Must run BEFORE the app's own scripts execute on navigation —
            # add_init_script (not a post-load page.evaluate) guarantees this
            # ordering. Sets the exact localStorage key index.html already
            # reads for SERVER_URL, so the app picks it up on its own, same
            # as it would after a real one-time manual Settings entry.
            page.add_init_script(f"localStorage.setItem('pt_server', '{SERVER_URL}');")
            log(f"Injected SERVER_URL: {SERVER_URL}")

            # DIAGNOSTIC: heartbeat only shows serverOk at one instant every
            # 5 minutes — not enough to see WHY it flips. This intercepts
            # every read/write to window.serverOk via a property descriptor,
            # so every single change gets logged with a real timestamp,
            # before the app's own `var serverOk=false;` declaration runs.
            # A `var` at top-level scope assigns to window, so the app's own
            # assignments (serverOk=true / serverOk=false) route through
            # this getter/setter instead of silently overwriting it.
            page.add_init_script("""
                (function(){
                    var _v = false;
                    Object.defineProperty(window, 'serverOk', {
                        get: function(){ return _v; },
                        set: function(nv){
                            console.log('[serverOk] ' + _v + ' -> ' + nv);
                            _v = nv;
                        },
                        configurable: true
                    });
                })();
            """)

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

            # FIX: the browser's own generic "Failed to load resource: 404"
            # console line never includes which URL failed — that detail
            # only lives in the browser's Network panel, invisible from here
            # until now. Without this, "something 404'd" was unactionable —
            # this makes it a specific, fixable finding on the next run.
            def on_response(response):
                if response.status >= 400:
                    log(f"[network] {response.status} {response.request.method} {response.url}")
            def on_request_failed(request):
                log(f"[network] FAILED (no response) {request.method} {request.url} "
                    f"— {request.failure}")
            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)

            log(f"Navigating to {APP_URL}")
            page.goto(APP_URL, wait_until="domcontentloaded", timeout=60000)
            log("Page loaded — app's own scan/poll timers are now running")

            session_start = time.time()
            last_health_log = time.time() - HEALTH_LOG_EVERY_SECONDS + FIRST_HEALTH_CHECK_DELAY_SECONDS

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
