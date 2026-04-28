import os
import csv
import httpx
from datetime import datetime
from models import Lead
from dotenv import load_dotenv
load_dotenv()
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")


def _url(tab: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet={tab}"


# ── Read helpers ───────────────────────────────────────────────────────────────

async def _fetch_tab(tab: str) -> list:
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(_url(tab), timeout=6.0)
            res.raise_for_status()
        lines = res.text.strip().splitlines()
        return list(csv.DictReader(lines))
    except Exception as e:
        print(f"[Sheets] Could not fetch {tab}: {e}")
        return []


async def get_all_services() -> list:
    rows = await _fetch_tab("services")
    return [
        {"name": r["name"].strip(), "description": r["description"].strip(), "price_range": r["price_range"].strip()}
        for r in rows
        if r.get("name","").strip() and r.get("active","yes").strip().lower() in ("yes","true","1","")
    ] or _fallback_services()


async def get_faqs(language: str = "en") -> list:
    rows = await _fetch_tab("faqs")
    return [
        {"question": r["question"].strip(), "answer": r["answer"].strip()}
        for r in rows
        if r.get("language","en").strip() == language and r.get("question","").strip()
    ]


async def get_areas() -> list:
    rows = await _fetch_tab("areas")
    return [
        {"name": r.get("area_name","").strip(), "yield": r.get("avg_rental_yield_pct","").strip(),
         "price": r.get("avg_price_sqft_aed","").strip(), "best_for": r.get("best_for","").strip()}
        for r in rows if r.get("area_name","").strip()
    ]


async def get_projects() -> list:
    rows = await _fetch_tab("offplan_projects")
    return [
        {"name": r.get("project_name","").strip(), "developer": r.get("developer","").strip(),
         "location": r.get("location","").strip(), "price": r.get("starting_price_aed","").strip(),
         "plan": r.get("payment_plan","").strip(), "handover": r.get("handover","").strip(),
         "highlights": r.get("highlights","").strip()}
        for r in rows if r.get("project_name","").strip()
    ]


async def get_developers() -> list:
    rows = await _fetch_tab("developers")
    return [
        {"name": r.get("developer_name","").strip(), "known_for": r.get("known_for","").strip(),
         "track_record": r.get("track_record","").strip()}
        for r in rows if r.get("developer_name","").strip()
    ]


async def get_payment_plans() -> list:
    rows = await _fetch_tab("payment_plans")
    return [
        {"plan": r.get("plan_name","").strip(), "structure": r.get("structure","").strip(),
         "description": r.get("description","").strip()}
        for r in rows if r.get("plan_name","").strip()
    ]



async def get_crawled_properties() -> list:
    rows = await _fetch_tab("crawled_properties")
    return [
        {
            "name": r.get("property_name", "").strip(),
            "developer": r.get("developer", "").strip(),
            "location": r.get("location", "").strip(),
            "type": r.get("type", "").strip(),
            "description": r.get("description", "").strip(),
            "highlights": r.get("highlights", "").strip(),
            "link": r.get("link", "").strip(),
        }
        for r in rows if r.get("property_name", "").strip()
    ]



async def get_company_info() -> dict:
    """Read Zahra Signature Realty company identity crawled from website.

Expected tab: company_info
Columns: company_name, home_content, about_content, contact_info, last_updated
"""
    rows = await _fetch_tab("company_info")
    if not rows:
        return {
            "company_name": os.getenv("AGENCY_NAME", "Zahra Signature Realty"),
            "home": "",
            "about": os.getenv("AGENCY_CONTEXT", "Dubai property brokerage focused on trusted, premium real estate guidance."),
            "contact": "",
        }

    r = rows[0]
    return {
        "company_name": r.get("company_name", "").strip(),
        "home": r.get("home_content", "").strip(),
        "about": r.get("about_content", "").strip(),
        "contact": r.get("contact_info", "").strip(),
        "last_updated": r.get("last_updated", "").strip(),
    }


# ── Save lead to sheet (needs service account for writing) ────────────────────
# For demo: prints to terminal + saves to in-memory store
# For production: replace with Google Sheets write via service account

_leads_store: list = []


def save_lead(lead: Lead):
    lead.created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    _leads_store.append(lead)
    print(f"\n{'='*40}")
    print(f"  NEW LEAD — {lead.name}")
    print(f"  Email:    {lead.email}")
    print(f"  Phone:    {lead.phone}")
    print(f"  Interest: {lead.interest}")
    print(f"  Budget:   {lead.budget}")
    print(f"  Area:     {lead.area}")
    print(f"  Viewing:  {lead.viewing_date} at {lead.viewing_time}")
    print(f"  Language: {lead.language}")
    print(f"{'='*40}\n")


def get_pending_followups(days: int = 2) -> list:
    """Return leads with no response after X days."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    return [
        l for l in _leads_store
        if l.status in ("new", "booking_sent")
        and l.created_at
        and datetime.strptime(l.created_at, "%Y-%m-%d %H:%M") < cutoff
    ]


def update_lead_status(session_id: str, status: str):
    for lead in _leads_store:
        if lead.session_id == session_id:
            lead.status = status
            break


def _fallback_services() -> list:
    return [
        {"name": "Property Buying Assistance", "price_range": "Commission-based", "description": "End-to-end support for buying ready or off-plan properties in Dubai"},
        {"name": "Off-Plan Investment Advisory", "price_range": "Free consultation", "description": "Expert guidance on off-plan projects, payment plans, ROI projections"},
        {"name": "Property Management", "price_range": "5–8% of annual rent", "description": "Full management of your investment property in Dubai"},
        {"name": "Golden Visa Property Guidance", "price_range": "Free consultation", "description": "Guidance on qualifying for UAE Golden Visa through property investment"},
    ]
