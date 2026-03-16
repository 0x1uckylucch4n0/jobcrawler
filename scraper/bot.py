"""
B1mo Job Bot — real-time job alerts for Alramina.
Commands:
  /start   - Register chat ID + start receiving alerts
  /search  - Manual search now
  /stop    - Stop alerts
  /help    - Commands
"""
import logging
import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from job_store import init_db
from sheets import log_jobs_to_sheet

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# Store active chat IDs (persisted in a simple file)
CHAT_IDS_FILE = "chat_ids.txt"


def load_chat_ids() -> set:
    if not os.path.exists(CHAT_IDS_FILE):
        return set()
    with open(CHAT_IDS_FILE) as f:
        return {int(line.strip()) for line in f if line.strip()}


def save_chat_id(chat_id: int):
    ids = load_chat_ids()
    ids.add(chat_id)
    with open(CHAT_IDS_FILE, "w") as f:
        f.write("\n".join(str(i) for i in ids))


def remove_chat_id(chat_id: int):
    ids = load_chat_ids()
    ids.discard(chat_id)
    with open(CHAT_IDS_FILE, "w") as f:
        f.write("\n".join(str(i) for i in ids))


def format_job(job: dict, index: int = None) -> str:
    number = f"{index}. " if index else ""
    sponsor_tag = {
        True:  "Visa · Confirmed sponsor",
        False: "Visa · Not on UK register",
        None:  "Visa · Unverified",
    }[job["is_sponsor"]]

    target_tag = "  ★ Target company\n" if job.get("is_target_company") else "\n"
    summary = f"_{job['summary']}_\n\n" if job.get("summary") else ""

    return (
        f"*{number}{job['title']}*\n"
        f"─────────────────────\n"
        f"{job['company']}  ·  {job['location']}\n"
        f"Match · {job['score']}/10{target_tag}"
        f"{sponsor_tag}\n\n"
        f"{summary}"
        f"[Apply →]({job['url']})"
    )


async def send_jobs(app: Application, jobs: list[dict], intro: str):
    """Send job results to all registered chat IDs."""
    chat_ids = load_chat_ids()
    if not chat_ids:
        return

    for chat_id in chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=intro, parse_mode="Markdown")
            for i, job in enumerate(jobs, 1):
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=format_job(job, i),
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
        except Exception as e:
            logging.error(f"Failed to send to {chat_id}: {e}")


async def run_scheduled_search(app: Application):
    """Runs every 30 mins — only sends NEW jobs not seen before."""
    logging.info("Running scheduled job search...")
    try:
        from job_search import search_jobs
        from job_store import purge_old_entries
        purge_old_entries(days=3)
        jobs = search_jobs(max_results=50, new_only=True)
        if jobs:
            await send_jobs(
                app, jobs,
                f"🚨 *{len(jobs)} new job(s) found!*\nLocation: London"
            )
            log_jobs_to_sheet(jobs)
            logging.info(f"Sent {len(jobs)} new jobs.")
        else:
            logging.info("No new jobs found.")
    except Exception as e:
        logging.error(f"Scheduled search error: {e}")


# --- Command handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    await update.message.reply_text(
        "✅ *Registered! You'll now get real-time job alerts.*\n\n"
        "I check LinkedIn & Indeed every 30 minutes.\n"
        "Locations: 🇬🇧 London · 🇫🇷 Paris · 🇺🇸 Bay Area\n"
        "Filters: Full-time · Professional hire · Visa sponsorship\n\n"
        "Commands:\n"
        "/search — Search right now\n"
        "/stop — Stop alerts\n"
        "/help — Show this",
        parse_mode="Markdown"
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    remove_chat_id(chat_id)
    await update.message.reply_text("Alerts stopped. Send /start to re-enable.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*B1mo Bot*\n\n"
        "/search — Find jobs now (LinkedIn + Indeed)\n"
        "/stop — Stop real-time alerts\n"
        "/start — Re-enable alerts\n\n"
        "Auto-checks every 30 minutes.\n"
        "Only sends jobs you haven't seen before.",
        parse_mode="Markdown"
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    save_chat_id(chat_id)
    await update.message.reply_text(
        "🔍 Searching LinkedIn & Indeed across London, Paris and Bay Area...\n"
        "This takes ~90 seconds ⏳"
    )
    try:
        from job_search import search_jobs
        jobs = search_jobs(max_results=10, new_only=False)

        if not jobs:
            await update.message.reply_text("No matching jobs found right now. Try again later.")
            return

        await update.message.reply_text(
            f"Found *{len(jobs)} jobs* matching your profile:",
            parse_mode="Markdown"
        )
        for i, job in enumerate(jobs, 1):
            await update.message.reply_text(
                format_job(job, i),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        log_jobs_to_sheet(jobs)
    except Exception as e:
        logging.error(f"Search error: {e}")
        await update.message.reply_text(f"Error: {str(e)}\n\nTry /search again.")


async def post_init(app: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_scheduled_search,
        trigger="interval",
        minutes=15,
        args=[app],
        id="job_check",
        next_run_time=datetime.now()
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    logging.info("Scheduler started — checking for new jobs every 30 minutes.")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN missing from .env")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search))

    print("B1mo bot running. Checking for new jobs every 30 minutes.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
