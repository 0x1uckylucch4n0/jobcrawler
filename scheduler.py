#!/usr/bin/env python3
"""Scheduler — runs linkedin_runner every RUN_INTERVAL_HOURS hours.
Also accepts Telegram commands:
  /run   — trigger an immediate run
  /stop  — pause scheduled runs
  /start — resume scheduled runs
  /status — show last run time + applied count
"""
import os, time, subprocess, sys, threading, requests
from datetime import datetime, timezone

RUN_INTERVAL_HOURS = float(os.environ.get("RUN_INTERVAL_HOURS", "2"))
TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT   = os.environ.get("TG_CHAT", "")
LIMIT     = int(os.environ.get("BATCH_LIMIT", "20"))

paused       = False
last_run_ts  = None
last_applied = 0
_update_offset = 0


def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def run_now():
    global last_run_ts, last_applied
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting linkedin_runner (limit={LIMIT})", flush=True)
    tg(f"🚀 *JobCrawler starting* — processing up to {LIMIT} jobs...")
    result = subprocess.run(
        [sys.executable, "linkedin_runner.py", "--limit", str(LIMIT)],
        capture_output=False,
        timeout=7200,   # 2-hour hard cap
    )
    last_run_ts = datetime.now(timezone.utc)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Run finished (exit {result.returncode})", flush=True)


def poll_telegram():
    """Poll Telegram for commands in a background thread."""
    global paused, _update_offset
    if not TG_TOKEN:
        return
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": _update_offset, "timeout": 30},
                timeout=40,
            )
            updates = resp.json().get("result", [])
            for upd in updates:
                _update_offset = upd["update_id"] + 1
                text = upd.get("message", {}).get("text", "").strip().lower()
                if text == "/run":
                    tg("▶️ Manual run triggered...")
                    threading.Thread(target=run_now, daemon=True).start()
                elif text == "/stop":
                    paused = True
                    tg("⏸ Scheduler paused.")
                elif text == "/start":
                    paused = False
                    tg("▶️ Scheduler resumed.")
                elif text == "/status":
                    ts = last_run_ts.strftime("%Y-%m-%d %H:%M UTC") if last_run_ts else "never"
                    tg(f"📊 Last run: {ts}\nPaused: {paused}\nInterval: {RUN_INTERVAL_HOURS}h\nBatch limit: {LIMIT}")
        except Exception:
            pass
        time.sleep(1)


if __name__ == "__main__":
    print("JobCrawler scheduler started", flush=True)
    tg(f"🤖 *JobCrawler scheduler online* — running every {RUN_INTERVAL_HOURS}h")

    # Start Telegram command listener
    t = threading.Thread(target=poll_telegram, daemon=True)
    t.start()

    # Run immediately on startup
    run_now()

    # Then loop on schedule
    interval_secs = RUN_INTERVAL_HOURS * 3600
    while True:
        time.sleep(interval_secs)
        if not paused:
            run_now()
