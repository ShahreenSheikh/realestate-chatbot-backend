import os
import asyncio
from database import save_lead
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from models import ChatRequest, ChatResponse
from ai import get_ai_response
from whatsapp import verify_webhook, parse_incoming, _send as wa_send
from followup import run_followups

app = FastAPI(title="Elite Properties Dubai — AI Chatbot", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Scheduler ──────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def start_scheduler():
    from crawler import run_crawler

    # Daily follow-up: every day at 10:00 AM Dubai time
    scheduler.add_job(
        run_followups,
        "cron",
        hour=10, minute=0,
        timezone="Asia/Dubai",
        id="daily_followup",
    )

    # Monthly crawler: 1st of every month at 08:00 AM Dubai time
    scheduler.add_job(
        run_crawler,
        "cron",
        day=1, hour=8, minute=0,
        timezone="Asia/Dubai",
        id="monthly_crawler",
    )

    scheduler.start()
    print("[Scheduler] Jobs started:")
    print("  • Daily follow-up  — every day at 10:00 AM Dubai")
    print("  • Monthly crawler  — 1st of month at 08:00 AM Dubai")


@app.on_event("shutdown")
async def stop_scheduler():
    scheduler.shutdown()


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "status":  "ok",
        "service": "Elite Properties Dubai — AI Chatbot",
        "agent":   os.getenv("AGENT_NAME", "Omar Hassan"),
    }


# ── Website chat ───────────────────────────────────────────────────────────────

@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        result = await get_ai_response(
            session_id=req.session_id,
            user_message=req.message,
            source="website",
        )
        result["session_id"] = req.session_id
        return result
    except Exception as e:
        print(f"[Route Error]: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Follow-up trigger ──────────────────────────────────────────────────────────

@app.post("/followup/run")
async def trigger_followup():
    """Manually trigger follow-up — for demo or n8n webhook."""
    await run_followups()
    return {"status": "ok", "message": "Follow-up run complete"}


# ── Crawler routes ─────────────────────────────────────────────────────────────

@app.post("/crawler/run")
async def run_crawler_now():
    """
    Manually trigger the web crawler.
    Crawls zahrasignaturerealty.com/properties/ and updates Google Sheets.
    Smart upsert: adds new, updates changed, deactivates removed — no duplicates.
    """
    from crawler import run_crawler
    result = await run_crawler()
    return result


@app.get("/crawler/status")
def crawler_status():
    """Check crawler schedule and target."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id":       job.id,
            "next_run": str(job.next_run_time),
        })
    return {
        "target_site": "https://zahrasignaturerealty.com/properties/",
        "sheet_tab":   "crawled_properties",
        "schedule":    "1st of every month at 08:00 AM Dubai time",
        "jobs":        jobs,
    }


# ── WhatsApp webhook — verification ───────────────────────────────────────────

@app.get("/webhook/whatsapp")
def whatsapp_verify(
    hub_mode:         str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge:    str = Query(None, alias="hub.challenge"),
):
    challenge = verify_webhook(hub_mode, hub_verify_token, hub_challenge)
    if challenge:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


# ── WhatsApp webhook — incoming messages ──────────────────────────────────────

@app.post("/webhook/whatsapp")
async def whatsapp_incoming(request: Request):
    body = await request.json()
    phone, text = parse_incoming(body)
    if not phone or not text:
        return {"status": "ignored"}

    result = await get_ai_response(
        session_id   = f"wa_{phone}",
        user_message = text,
        source       = "whatsapp",
    )
    await wa_send(phone, result["reply"])
    return {"status": "ok"}
