import os
import re
import json
import dateparser
from logger import save_chat_log
from database import save_lead
from openai import OpenAI
from datetime import datetime, timedelta
from database import get_all_services, get_faqs, get_areas, get_projects, get_developers, get_payment_plans, get_crawled_properties, get_company_info 
from models import Lead
from hashlib import md5
import time

CACHE = {}
CACHE_TTL = 300  # 5 minutes

client = OpenAI(api_key=os.getenv("CEREBRAS_API_KEY"), base_url="https://api.cerebras.ai/v1")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
AGENT_NAME = os.getenv("AGENT_NAME", "Omar Hassan")
AGENCY = os.getenv("AGENCY_NAME", "Zahra Signature Realty")
AGENCY_CONTEXT = os.getenv("AGENCY_CONTEXT", "Dubai property brokerage focused on trusted, premium real estate guidance.")

SYSTEM_PROMPT = """You are a professional real estate sales assistant for {AGENCY}, a Dubai property brokerage. You are running on Cerebras GPT-OSS, so follow the workflow below literally and never skip qualification steps.

## LANGUAGE — CRITICAL
- Arabic input → reply ONLY in formal Gulf Arabic.
- English input → reply ONLY in English.
- Never mix languages.

## ROLE
Act like an experienced Dubai property consultant. Your job is to understand the client's needs first, recommend suitable properties from the supplied data, answer questions, and only then help arrange a viewing.

## REPLY STYLE
- Usually keep replies concise. When presenting property options, you may use up to 140 words so 2–3 options contain useful facts.
- Maximum 2 sentences.
- Ask ONE question at a time.
- Never ask for information already present in CONFIRMED USER PREFERENCES.
- Answer the client's direct question before asking the next relevant question.
- Be natural and consultative, not like a form or questionnaire.
- Never invent property details, prices, availability, payment plans, ROI, amenities, or locations.

## REQUIRED SALES FLOW — DO NOT SKIP STEPS

### STAGE 1 — UNDERSTAND THE CLIENT
Before discussing booking or collecting contact details, qualify the client ONE question at a time. Collect these when relevant:
1. Intent: buying, renting, investing, or off-plan.
2. Property type: apartment, villa, townhouse, etc.
3. Bedroom requirement: studio, 1BR, 2BR, 3BR, etc.
4. Preferred area/location.
5. Budget or budget range.
6. Purpose: personal/end use or investment, if not already obvious from intent.
7. Timeline: ready now, this month, 3–6 months, later, etc.
8. Important lifestyle/property preference when relevant: sea view, family community, furnished, waterfront, amenities, etc.

IMPORTANT:
- Do NOT ask all questions at once.
- Do NOT ask a question whose answer is already known.
- If the user says something broad like “I need an apartment,” continue qualifying instead of jumping to booking.
- If the user asks about a specific property, answer their question first, then collect only the missing qualification details needed to advise them properly.

### STAGE 2 — RECOMMEND AND GIVE VALUE
Once ALL core requirements (intent, property type, area, budget, bedrooms, purpose, timeline) are known, you MUST recommend 2–3 genuinely relevant options from PROPERTY DATA before asking about a viewing. Never skip directly to viewing/contact collection.
Recommendations must respect the latest confirmed area, budget, property type, bedrooms, purpose, timeline, and preferences.
For each option, briefly mention available facts such as:
- project/property name and developer
- property type / bedrooms when known
- location
- starting price
- payment plan
- relevant amenities or lifestyle benefits
Never switch the client's preferred area without asking permission.

### STAGE 3 — ESTABLISH INTEREST
After giving useful property options/details, ask which option they prefer or whether they would like to arrange a viewing.
Do NOT collect name, email, phone, viewing date, or viewing time merely because the client is asking questions.

### STAGE 4 — BOOKING
Only enter booking mode when BOTH are true:
A. The client has been sufficiently qualified and has received useful property guidance/options.
B. The client clearly asks to book/schedule/arrange a viewing or clearly agrees when you offer a viewing.

Then collect missing booking details ONE AT A TIME in this order:
1. viewing date and time
2. name
3. valid email
4. phone number

If the client gives a viewing date/time early, remember it, but DO NOT abandon qualification and immediately start collecting contact information. Finish the missing property qualification first.

## BOOKING BLOCK — STRICT
Only output a BOOKING block when you have:
- a clear property/viewing interest
- sufficient qualification details
- name
- valid email
- phone number
- viewing date
- viewing time

Output exactly:
<BOOKING>{"name":"...","email":"...","phone":"...","interest":"...","budget":"...","area":"...","viewing_date":"...","viewing_time":"...","language":"..."}</BOOKING>

BOOKING rules:
- Valid JSON only; double quotes only.
- Never output a partial BOOKING block.
- Do not expose internal booking markup as normal user-facing text.
- Natural dates such as “tomorrow at 10 am” are allowed; backend normalization handles them.
- After booking, continue answering normal property/company questions and do not restart qualification or booking collection.

## CONSULTANT BEHAVIOR
- Sea view → prioritize matching waterfront inventory such as Dubai Marina, JBR, Bluewaters, Palm Jumeirah, or Emaar Beachfront ONLY when compatible with the client's stated area/preferences and available data.
- Family lifestyle → prioritize suitable family communities only when compatible with the client's requirements.
- Investment → focus on available data relevant to investment; never fabricate ROI/yield.
- Never pressure the client into a viewing.
- Never offer discounts; refer discount requests to a human manager.
- If requested information is unavailable, say a specialist can confirm it.

## PROPERTY DATA
{DATA}
"""

FALLBACK_EN = """Collect name, email, phone, viewing date and time one at a time.
History: {HISTORY}
User said: {MESSAGE}
Max 20 words. Output <BOOKING> block when you have all details."""

FALLBACK_AR = """اجمع الاسم والبريد والهاتف وتاريخ ووقت المعاينة واحداً تلو الآخر.
السياق: {HISTORY}
قال المستخدم: {MESSAGE}
حد أقصى 20 كلمة. أخرج كتلة BOOKING عند اكتمال البيانات."""

_sessions: dict = {}
SESSION_TTL_SECONDS = 60 * 60  # auto-clean old demo sessions after 1 hour


def cleanup_old_demo_sessions() -> None:
    """Prevent old browser demo sessions from living forever in memory."""
    now = datetime.utcnow()
    expired = []
    for sid, sess in list(_sessions.items()):
        if not str(sid).startswith("demo_"):
            continue
        last_seen = sess.get("last_seen") or sess.get("window_start") or now
        if (now - last_seen).total_seconds() > SESSION_TTL_SECONDS:
            expired.append(sid)
    for sid in expired:
        _sessions.pop(sid, None)



def detect_language(text: str) -> str:
    arabic = len(re.findall(r'[\u0600-\u06FF]', text))
    return "ar" if arabic > len(text) * 0.2 else "en"


def is_placeholder(value: str) -> bool:
    if not value:
        return True
    v = value.strip().lower()
    return v in {"", "test", "none", "null", "your_whatsapp_token", "your_phone_number_id", "any_random_string"}


def _safe_get(row: dict, key: str, default: str = "") -> str:
    """Read sheet fields safely even if a column is missing."""
    return str((row or {}).get(key, default) or "").strip()[:300]


def _keywords(text: str) -> list:
    """Small keyword extractor for demo-friendly sheet filtering, no vector DB needed."""
    stop = {
        "about", "tell", "please", "property", "properties", "dubai", "want", "need",
        "looking", "interested", "information", "details", "price", "prices", "area",
        "areas", "project", "projects", "plan", "plans", "payment", "what", "which",
        "where", "when", "with", "from", "that", "this", "have", "there", "your",
    }
    words = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
    return [w for w in words if len(w) > 2 and w not in stop]


def row_matches_user(row: dict, user_message: str) -> bool:
    """Keyword match one sheet row against the user's message."""
    words = _keywords(user_message)
    if not words:
        return False
    haystack = " ".join(str(v).lower() for v in (row or {}).values())
    return any(w in haystack for w in words)


def pick_relevant_rows(rows: list, user_message: str, matched_limit: int = 5, fallback_limit: int = 3) -> list:
    """Use the full sheet in Python, but send only relevant rows to the LLM."""
    rows = rows or []
    matched = [r for r in rows if row_matches_user(r, user_message)]
    return (matched[:matched_limit] if matched else rows[:fallback_limit])


async def build_system_prompt(language: str, user_message: str = "") -> str:
    """Build a small per-message prompt from the full sheet data.

    This avoids sending the whole sheet to Cerebras on every message while still
    allowing the backend to search the whole sheet with simple keywords.
    """
    services = await get_all_services()
    faqs = await get_faqs(language)
    areas = await get_areas()
    projects = await get_projects()
    developers = await get_developers()
    plans = await get_payment_plans()
    crawled = await get_crawled_properties()
    company = await get_company_info()

    services_for_prompt = pick_relevant_rows(services, user_message, 3, 2)
    faqs_for_prompt = pick_relevant_rows(faqs, user_message, 3, 1)
    areas_for_prompt = pick_relevant_rows(areas, user_message, 3, 2)
    projects_for_prompt = pick_relevant_rows(projects, user_message, 3, 2)
    developers_for_prompt = pick_relevant_rows(developers, user_message, 2, 1)
    plans_for_prompt = pick_relevant_rows(plans, user_message, 2, 1)
    crawled_for_prompt = pick_relevant_rows(crawled, user_message, 3, 1)

    svc = "\n".join(
        f"- {_safe_get(s,'name')}: {_safe_get(s,'price_range')} — {_safe_get(s,'description')}"
        for s in services_for_prompt
    )
    faq = "\n".join(
        f"Q: {_safe_get(f,'question')}\nA: {_safe_get(f,'answer')}"
        for f in faqs_for_prompt
    )
    area = "\n".join(
        f"- {_safe_get(a,'name')}: AED {_safe_get(a,'price')} psf, {_safe_get(a,'yield')} yield — {_safe_get(a,'best_for')}"
        for a in areas_for_prompt
    )
    proj = "\n".join(
        f"- {_safe_get(p,'name')} by {_safe_get(p,'developer')} in {_safe_get(p,'location')}: from AED {_safe_get(p,'price')}, {_safe_get(p,'plan')}, handover {_safe_get(p,'handover')}"
        for p in projects_for_prompt
    )
    dev = "\n".join(
        f"- {_safe_get(d,'name')}: {_safe_get(d,'known_for')}"
        for d in developers_for_prompt
    )
    plan = "\n".join(
        f"- {_safe_get(p,'plan')}: {_safe_get(p,'structure')} — {_safe_get(p,'description')}"
        for p in plans_for_prompt
    )
    crawled_data = "\n".join(
        f"- {_safe_get(p,'name')} in {_safe_get(p,'location')} by {_safe_get(p,'developer')} ({_safe_get(p,'type')}): {_safe_get(p,'description')}"
        for p in crawled_for_prompt
    )

    company_name = _safe_get(company, "company_name") or AGENCY
    company_about = _safe_get(company, "about")[:350]
    company_home = _safe_get(company, "home")[:250]
    company_contact = _safe_get(company, "contact_info")[:180]

    company_block = (
        f"Name: {company_name}\n"
        f"About: {company_about or AGENCY_CONTEXT}\n"
        f"Website/Home: {company_home}\n"
        f"Contact: {company_contact}"
    )

    data = (
        f"### COMPANY IDENTITY\n{company_block}\n\n"
        f"### AGENCY CONTEXT\n{AGENCY_CONTEXT}\n\n"
        f"### RELEVANT SERVICES\n{svc}\n\n"
        f"### RELEVANT KEY AREAS\n{area}\n\n"
        f"### RELEVANT OFF-PLAN PROJECTS\n{proj}\n\n"
        f"### RELEVANT DEVELOPERS\n{dev}\n\n"
        f"### RELEVANT PAYMENT PLANS\n{plan}\n\n"
        f"### RELEVANT CRAWLED PROPERTIES\n{crawled_data}\n\n"
        f"### RELEVANT FAQS\n{faq}"
    )

    return SYSTEM_PROMPT.replace("{AGENCY}", AGENCY).replace("{DATA}", data)

def extract_booking(text: str):
    match = re.search(r"<BOOKING>(.*?)</BOOKING>", text or "", re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            return None
    return None


def clean_reply(text: str) -> str:
    """Remove any internal booking markup from text shown to the user."""
    cleaned = re.sub(r"<BOOKING>.*?</BOOKING>", "", text or "", flags=re.DOTALL).strip()
    cleaned = re.sub(r"<BOOKING>.*", "", cleaned, flags=re.DOTALL).strip()
    return cleaned


def shorten(text: str, max_w: int = 50) -> str:
    """Shorten fallback/static text without ever chopping a sentence mid-way."""
    text = (text or "").strip()
    if len(text.split()) <= max_w:
        return text

    sentences = re.split(r'(?<=[.!?])\s+', text)
    kept = []
    count = 0
    for sentence in sentences:
        words = sentence.split()
        if kept and count + len(words) > max_w:
            break
        kept.append(sentence)
        count += len(words)
        if count >= max_w:
            break

    # If the first sentence itself exceeds max_w, return it whole rather than truncating it.
    return " ".join(kept).strip() if kept else text


def history_to_messages(history: list) -> list:
    return [{"role": m["role"], "content": m.get("content", "")} for m in history[-4:]]


def build_history_text(history: list) -> str:
    lines = [
        f"{'User' if m['role']=='user' else 'Assistant'}: {m.get('content','')}"
        for m in history[-4:]
    ]
    return "\n".join(lines) or "No prior conversation."


def build_lead_context(lead: dict) -> str:
    """Give the LLM a compact memory lock so it stops repeating questions or switching preferences."""
    if not lead:
        return ""

    labels = {
        "interest": "Interest",
        "area": "Area",
        "budget": "Budget",
        "property_type": "Property type",
        "bedrooms": "Bedrooms",
        "purpose": "Purpose",
        "timeline": "Timeline",
        "view_preference": "View preference",
        "viewing_date": "Viewing date",
        "viewing_time": "Viewing time",
        "name": "Client name",
        "email": "Email",
        "phone": "Phone",
    }
    lines = []
    for key, label in labels.items():
        val = lead.get(key)
        if _valid_lead_value(val):
            lines.append(f"- {label}: {val}")

    if not lines:
        return ""

    return (
        "## CONFIRMED USER PREFERENCES — MEMORY LOCK\n"
        + "\n".join(lines)
        + "\nRules: Do not ask for these again. Keep recommendations aligned with these preferences. "
        + "Do not switch area, budget, bedroom count, or purpose unless the user clearly changes them."
    )


def looks_like_datetime(message: str) -> bool:
    m = (message or "").lower()
    temporal_words = [
        "today", "tomorrow", "tonight", "morning", "afternoon", "evening", "night",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "next", "week", "am", "pm", "a.m", "p.m"
    ]
    if any(w in m for w in temporal_words):
        return True
    if re.search(r"\b\d{1,2}\s*(:\d{2})?\s*(am|pm)\b", m):
        return True
    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", m):
        return True
    if re.search(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", m):
        return True
    return False


def has_explicit_viewing_time(message: str) -> bool:
    """Return True only when the user gave a real viewing date/time.

    This prevents words like "immediately" or "move there soon" from being
    treated as an appointment time.
    """
    m = (message or "").lower().strip()

    # Explicit time like 10am, 10:30 pm, 14:00
    if re.search(r"\b\d{1,2}\s*(:\d{2})?\s*(am|pm)\b", m):
        return True
    if re.search(r"\b\d{1,2}:\d{2}\b", m):
        return True

    # Explicit numeric/calendar dates
    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", m):
        return True
    if re.search(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", m):
        return True

    # Natural date phrases that are appointment-like
    appointment_phrases = [
        "tomorrow at", "today at", "tonight at",
        "monday at", "tuesday at", "wednesday at", "thursday at",
        "friday at", "saturday at", "sunday at",
        "next monday", "next tuesday", "next wednesday", "next thursday",
        "next friday", "next saturday", "next sunday",
    ]
    return any(phrase in m for phrase in appointment_phrases)


def fallback_extract_datetime_from_message(message: str):
    """Extract natural date/time only when the message actually looks temporal."""
    if not looks_like_datetime(message):
        return None

    parsed = dateparser.parse(
        message,
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": datetime.now() + timedelta(hours=4),
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if parsed:
        # Preserve an explicitly typed clock time; dateparser can occasionally inherit
        # the current minute/hour for short phrases such as "tomorrow at 9".
        tm = re.search(r"\b(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", message.lower())
        if tm:
            hour = int(tm.group(1)); minute = int(tm.group(2) or 0); ap = tm.group(3)
            if ap == "pm" and hour < 12: hour += 12
            if ap == "am" and hour == 12: hour = 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                parsed = parsed.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return {
            "viewing_date": parsed.strftime("%Y-%m-%d"),
            "viewing_time": parsed.strftime("%H:%M"),
            "raw_datetime": message.strip(),
        }
    return None


def normalize_booking_datetime(booking: dict) -> dict:
    """Convert natural language date/time into calendar-safe YYYY-MM-DD + HH:MM."""
    date_text = str(booking.get("viewing_date", "") or "").strip()
    time_text = str(booking.get("viewing_time", "") or "").strip()
    combined = f"{date_text} {time_text}".strip()

    if not combined or combined.upper() == "TBD":
        return booking

    parsed = dateparser.parse(
        combined,
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": datetime.now() + timedelta(hours=4),
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if parsed:
        booking["viewing_date"] = parsed.strftime("%Y-%m-%d")
        booking["viewing_time"] = parsed.strftime("%H:%M")
    return booking


def extract_contact_from_history(history: list) -> dict:
    """Best-effort extraction of name/email/phone from user messages."""
    user_texts = [m.get("content", "") for m in history if m.get("role") == "user"]
    joined = "\n".join(user_texts)

    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", joined)
    phone_match = re.search(r"(?:\+?\d[\d\s\-()]{7,}\d)", joined)

    name = ""
    ignored = {
        "yes", "sure", "ok", "okay", "hello", "hi", "rent", "buy", "invest",
        "tomorrow at 10am", "tomorrow at 10 am", "today at 10am", "today at 10 am",
        "confirmation for what", "tomorrow", "today", "tell me more", "more details",
        "details", "book", "booking", "schedule", "viewing", "that's not my name",
        "thats not my name", "not my name"
    }
    for msg in user_texts:
        clean = msg.strip()
        lowered = clean.lower()
        if not clean or lowered in ignored:
            continue
        if "@" in clean or re.search(r"\d", clean) or looks_like_datetime(clean) or looks_like_non_name_reply(clean):
            continue
        explicit = extract_explicit_name(clean)
        if explicit:
            name = explicit
        elif len(clean.split()) <= 4 and len(clean) <= 40:
            name = clean
    return {
        "name": name,
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(0).strip() if phone_match else "",
    }



def extract_explicit_name(message: str) -> str:
    """Extract names from phrases like 'my name is shahreen' even if we asked for email."""
    msg = (message or "").strip()
    patterns = [
        r"\bmy name is\s+([A-Za-z][A-Za-z\s.'-]{1,40})",
        r"\bi am\s+([A-Za-z][A-Za-z\s.'-]{1,40})",
        r"\bi'm\s+([A-Za-z][A-Za-z\s.'-]{1,40})",
        r"\bthis is\s+([A-Za-z][A-Za-z\s.'-]{1,40})",
        r"\bname\s*[:\-]\s*([A-Za-z][A-Za-z\s.'-]{1,40})",
    ]
    for pat in patterns:
        m = re.search(pat, msg, re.I)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .,-")
            # Remove trailing conversational words if present
            name = re.sub(r"\b(and|but|also|email|phone|number)\b.*$", "", name, flags=re.I).strip()
            if 1 <= len(name.split()) <= 4 and len(name) <= 40:
                return name
    return ""


def is_name_rejection(message: str) -> bool:
    """Detect when user says the bot stored the wrong name."""
    msg = (message or "").lower()
    return any(p in msg for p in [
        "not my name",
        "that's not my name",
        "that is not my name",
        "wrong name",
        "incorrect name",
        "not name",
    ])


def looks_like_non_name_reply(message: str) -> bool:
    """Avoid storing intent replies like 'tell me more' as a person's name."""
    msg = (message or "").strip().lower()
    bad_exact = {
        "tell me more", "more details", "details", "yes", "no", "sure", "ok", "okay",
        "book", "booking", "schedule", "viewing", "send details", "interested",
        "i am interested", "not now", "later", "thats not my name", "that's not my name",
    }
    if msg in bad_exact:
        return True
    if "?" in msg:
        return True
    if any(x in msg for x in ["tell me", "more about", "details", "price", "payment", "available", "amenities"]):
        return True
    return False

def update_lead_state_from_message(session: dict, user_message: str):
    """Store booking/contact details deterministically so the AI cannot lose them."""
    lead = session.setdefault("lead", {})
    msg = user_message.strip()

    # If user corrects the assistant, clear the wrong stored name and ask again.
    if is_name_rejection(msg):
        lead.pop("name", None)
        session.setdefault("asked_missing_fields", {}).pop("name", None)

    # Explicit name phrases work even if the previous question was email/phone.
    explicit_name = extract_explicit_name(msg)
    if explicit_name:
        lead["name"] = explicit_name

    # Email
    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", msg)
    if email_match:
        lead["email"] = email_match.group(0)

    # Phone. Avoid treating budget like 750k as phone.
    phone_match = re.search(r"(?:\+?\d[\d\s\-()]{7,}\d)", msg)
    if phone_match and not re.search(r"\b\d+\s*[kKmM]\b", msg):
        lead["phone"] = phone_match.group(0).strip()

    # Date/time — only store when the user gives an explicit appointment time.
    # This avoids wrongly booking from phrases like "move there immediately".
    if has_explicit_viewing_time(msg):
        dt = fallback_extract_datetime_from_message(msg)
        if dt:
            only_time = (
                re.fullmatch(r"\s*\d{1,2}\s*(:\d{2})?\s*(am|pm)\s*", msg.lower())
                or re.fullmatch(r"\s*\d{1,2}:\d{2}\s*", msg.lower())
            )
            if only_time:
                lead["viewing_time"] = dt["viewing_time"]
            else:
                lead["viewing_date"] = dt["viewing_date"]
                lead["viewing_time"] = dt["viewing_time"]
            lead["raw_datetime"] = dt["raw_datetime"]
    else:
        date_only_words = [
            "today", "tomorrow", "monday", "tuesday", "wednesday",
            "thursday", "friday", "saturday", "sunday", "next"
        ]
        if any(w in msg.lower() for w in date_only_words):
            dt = fallback_extract_datetime_from_message(msg)
            if dt:
                lead["viewing_date"] = dt["viewing_date"]
                lead["raw_datetime"] = dt["raw_datetime"]

    # Budget
    budget_match = re.search(r"\b(?:aed\s*)?\d+(?:\.\d+)?\s*(?:k|K|m|M|mil|MIL|mn|MN|million|aed|dirham|dirhams|usd|\$)?\b", msg)
    if budget_match and re.search(r"\b(budget|aed|dirham|dirhams|million|mil|mn|\d+\s*(?:k|K|m|M|mil|MIL|mn|MN))\b", msg, re.I):
        lead["budget"] = budget_match.group(0).strip()

    # Bedroom extraction: stores inputs like "1 bedroom", "2 bed", "3br", "studio".
    lowered_msg = msg.lower()
    bedroom_match = re.search(r"\b(\d+)\s*(?:bed|beds|bedroom|bedrooms|br)\b", lowered_msg)
    if bedroom_match:
        lead["bedrooms"] = bedroom_match.group(1)
    elif re.search(r"\bstudio\b", lowered_msg):
        lead["bedrooms"] = "Studio"

    # Property type extraction
    if re.search(r"\bapartment|flat\b", lowered_msg):
        lead["property_type"] = "Apartment"
    elif re.search(r"\bvilla\b", lowered_msg):
        lead["property_type"] = "Villa"
    elif re.search(r"\btownhouse|town house\b", lowered_msg):
        lead["property_type"] = "Townhouse"

    # Purpose extraction
    if re.search(r"\b(personal|end use|end-use|live in|move in|own use|own|myself|for me|family use)\b", lowered_msg):
        lead["purpose"] = "Personal use"
    elif re.search(r"\binvest|investment|roi|rental yield|yield\b", lowered_msg):
        lead["purpose"] = "Investment"

    # Lifestyle/view preference extraction
    if re.search(r"\bsea view|seaview|waterfront|marina view|beach view|ocean view\b", lowered_msg):
        lead["view_preference"] = "Sea view"

    # Timeline extraction - do NOT treat this as a viewing appointment date.
    if re.search(r"\b(immediately|asap|ready now|move now)\b", lowered_msg):
        lead["timeline"] = "Immediate"
    elif re.search(r"\b(within (?:a|one|1) month|next month|in (?:a|one|1) month)\b", lowered_msg):
        lead["timeline"] = "Within a month"
    elif re.search(r"\b(this month|soon)\b", lowered_msg):
        lead["timeline"] = "Soon"

    # Area extraction
    areas = [
        "business bay", "dubai marina", "downtown dubai", "downtown",
        "dubai hills", "dubai hills estate", "palm jumeirah", "jvc",
        "creek harbour", "dubai creek harbour", "arjan", "damac hills",
        "jumeirah village circle", "jlt", "bluewaters", "meydan",
    ]
    area_aliases_direct = {
        "marina": "Dubai Marina", "jbr": "JBR", "palm": "Palm Jumeirah",
        "jvc": "JVC", "jlt": "JLT", "downtown": "Downtown Dubai"
    }
    normalized_area_answer = re.sub(r"[^a-z0-9 ]+", " ", lowered_msg).strip()
    if normalized_area_answer in area_aliases_direct:
        lead["area"] = area_aliases_direct[normalized_area_answer]
    else:
        for area in areas:
            if area in msg.lower():
                lead["area"] = area.title()
                break

    # Interest: store a clean intent instead of the full user sentence.
    if not lead.get("interest") and re.search(r"\b(buy|rent|invest|apartment|villa|townhouse|property|off-plan|marina|hills|downtown|business bay)\b", msg, re.I):
        lower_msg = msg.lower()
        if "rent" in lower_msg:
            lead["interest"] = "Rent Property"
        elif "buy" in lower_msg:
            lead["interest"] = "Buy Property"
        elif "invest" in lower_msg or "investment" in lower_msg:
            lead["interest"] = "Investment Property"
        elif "villa" in lower_msg:
            lead["interest"] = "Villa"
        elif "townhouse" in lower_msg:
            lead["interest"] = "Townhouse"
        elif "apartment" in lower_msg:
            lead["interest"] = "Apartment"
        else:
            lead["interest"] = "Dubai Property"

    # Name only when the previous assistant asked for it.
    last_assistant = ""
    for m in reversed(session.get("history", [])):
        if m.get("role") == "assistant":
            last_assistant = m.get("content", "").lower()
            break

    if "name" in last_assistant and not lead.get("name"):
        if (
            msg
            and "@" not in msg
            and not re.search(r"\d", msg)
            and not looks_like_datetime(msg)
            and not looks_like_non_name_reply(msg)
        ):
            clean_name = re.sub(r"^(my name is|i am|i'm|this is)\s+", "", msg, flags=re.I).strip()
            if len(clean_name.split()) <= 4 and len(clean_name) <= 40:
                lead["name"] = clean_name

    # CONTEXTUAL ANSWER CAPTURE -------------------------------------------------
    # The user often answers with short values such as "marina", "7000", or
    # "within a month". Capture those based on the question we just asked so
    # Python state stays in sync with the natural Cerebras conversation.
    last_q = last_assistant.lower()

    # Area aliases / short answers.
    area_aliases = {
        "marina": "Dubai Marina",
        "dubai marina": "Dubai Marina",
        "jbr": "JBR",
        "palm": "Palm Jumeirah",
        "palm jumeirah": "Palm Jumeirah",
        "downtown": "Downtown Dubai",
        "business bay": "Business Bay",
        "jvc": "JVC",
        "jlt": "JLT",
        "bluewaters": "Bluewaters",
        "meydan": "Meydan",
        "arjan": "Arjan",
        "dubai hills": "Dubai Hills Estate",
        "creek harbour": "Dubai Creek Harbour",
    }
    compact = re.sub(r"[^a-z0-9 ]+", " ", lowered_msg).strip()
    if ("which area" in last_q or "area in dubai" in last_q or "waterfront area" in last_q or "community" in last_q):
        for alias, canonical in area_aliases.items():
            if compact == alias or alias in compact:
                lead["area"] = canonical
                break

    # A bare number is a valid budget when it directly answers a budget question.
    if ("budget" in last_q or "budget range" in last_q) and not _valid_lead_value(lead.get("budget")):
        bare_budget = re.search(r"(?:aed\s*)?(\d+(?:[.,]\d+)?)\s*(k|m|mil|mn|million)?", lowered_msg, re.I)
        if bare_budget:
            raw_amount = bare_budget.group(1).replace(",", "")
            unit = (bare_budget.group(2) or "").lower()
            lead["budget"] = f"{raw_amount} {unit}".strip()

    # Natural timeline answers.
    timeline_patterns = [
        (r"\b(immediately|asap|right away|now)\b", "Immediate"),
        (r"\b(within (?:a|one|1) month|next month|in (?:a|one|1) month)\b", "Within a month"),
        (r"\b(within (?:two|2) months|in (?:two|2) months)\b", "Within 2 months"),
        (r"\b(within (?:three|3) months|in (?:three|3) months)\b", "Within 3 months"),
        (r"\b(this month|soon)\b", "Soon"),
        (r"\b(later|no rush|flexible)\b", "Later / flexible"),
    ]
    if "when" in last_q or "move" in last_q or "timeline" in last_q or "complete the purchase" in last_q:
        for pat, canonical in timeline_patterns:
            if re.search(pat, lowered_msg, re.I):
                lead["timeline"] = canonical
                break

    # If this is a normal residential rental, purpose is implicitly personal use.
    if "rent" in str(lead.get("interest") or "").lower() and not _valid_lead_value(lead.get("purpose")):
        lead["purpose"] = "Personal use"

    return lead


def _valid_lead_value(value: str) -> bool:
    if not value:
        return False
    v = str(value).strip().lower()
    return v not in {"", "—", "-", "none", "null", "tbd", "confirmed", "client", "unknown"}


def maybe_build_booking_from_state(session: dict, language: str):
    """Create booking only when all required details are present and real."""
    lead = session.get("lead", {})
    required = ["viewing_date", "viewing_time", "name", "email", "phone"]
    if all(_valid_lead_value(lead.get(k)) for k in required):
        return {
            "name": lead.get("name"),
            "email": lead.get("email"),
            "phone": lead.get("phone"),
            "interest": lead.get("interest") or "Dubai Property Viewing",
            "budget": lead.get("budget", ""),
            "area": lead.get("area", ""),
            "property_type": lead.get("property_type", ""),
            "bedrooms": lead.get("bedrooms", ""),
            "purpose": lead.get("purpose", ""),
            "timeline": lead.get("timeline", ""),
            "viewing_date": lead.get("viewing_date"),
            "viewing_time": lead.get("viewing_time"),
            "language": language,
        }
    return None

def is_user_query(message: str) -> bool:
    """Detect if user is asking a question (not giving booking info)."""
    msg = (message or "").lower()

    question_keywords = [
        "what", "how", "why", "where", "when", "which",
        "price", "cost", "details", "information",
        "do you", "can you", "is there", "are there"
    ]

    return (
        "?" in msg
        or any(q in msg for q in question_keywords)
    )



def _parse_budget_aed(value: str):
    """Best-effort conversion of values like '20 mil', '2.5m', '750k' to AED."""
    if not value:
        return None
    text = str(value).lower().replace(",", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|m|mil|mn|million)?", text)
    if not m:
        return None
    amount = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "k":
        amount *= 1_000
    elif unit in {"m", "mil", "mn", "million"}:
        amount *= 1_000_000
    return amount


def _row_price_aed(row: dict):
    for key in ("price", "starting_price", "price_from", "starting price"):
        raw = _safe_get(row, key)
        if raw:
            parsed = _parse_budget_aed(raw)
            if parsed:
                return parsed
    return None


def _recommendation_score(row: dict, lead: dict) -> int:
    hay = " ".join(str(v or "").lower() for v in (row or {}).values())
    score = 0
    area = str(lead.get("area") or "").lower()
    ptype = str(lead.get("property_type") or "").lower()
    beds = str(lead.get("bedrooms") or "").lower()
    purpose = str(lead.get("purpose") or "").lower()
    if area and area in hay:
        score += 12
    if ptype and ptype in hay:
        score += 5
    if beds and (f"{beds} bed" in hay or f"{beds}br" in hay or f"{beds} bedroom" in hay):
        score += 6
    if "investment" in purpose and any(x in hay for x in ("yield", "roi", "invest")):
        score += 2
    budget = _parse_budget_aed(lead.get("budget"))
    price = _row_price_aed(row)
    if budget and price:
        if price <= budget:
            score += 5
        else:
            score -= 8
    return score


async def build_matching_options_reply(session: dict, language: str) -> str:
    """Build the recommendation stage from database rows so the model cannot skip the actual options."""
    lead = session.get("lead", {})
    projects = await get_projects() or []
    crawled = await get_crawled_properties() or []
    rows = projects + crawled
    ranked = sorted(rows, key=lambda r: _recommendation_score(r, lead), reverse=True)
    ranked = [r for r in ranked if _recommendation_score(r, lead) > 0][:3]

    if not ranked:
        if language == "ar":
            return "لدي متطلباتك الآن، لكن لا توجد خيارات مطابقة كافية في بيانات العقارات الحالية. هل ترغب أن أطلب من مستشار عقاري البحث عن خيارات مناسبة لك؟"
        return "I have your requirements, but I don't have enough matching listings in the current property data to present real options. Would you like a property specialist to find suitable matches for you?"

    session["recommended_options"] = []
    lines = []
    for i, row in enumerate(ranked, 1):
        name = _safe_get(row, "name") or _safe_get(row, "project") or f"Option {i}"
        location = _safe_get(row, "location") or lead.get("area", "")
        developer = _safe_get(row, "developer")
        ptype = _safe_get(row, "type") or lead.get("property_type", "")
        price = _safe_get(row, "price") or _safe_get(row, "starting_price") or _safe_get(row, "price_from")
        plan = _safe_get(row, "plan") or _safe_get(row, "payment_plan")
        desc = _safe_get(row, "description")
        session["recommended_options"].append({"index": i, "name": name, "row": row})
        bits = []
        if developer:
            bits.append(f"by {developer}")
        if location:
            bits.append(f"in {location}")
        if ptype:
            bits.append(ptype)
        if price:
            bits.append(f"from AED {price}" if "aed" not in price.lower() else f"from {price}")
        if plan:
            bits.append(f"payment plan: {plan}")
        summary = ", ".join(bits)
        if desc:
            summary += (" — " if summary else "") + shorten(desc, 22)
        lines.append(f"{i}. {name} — {summary}".rstrip(" —"))

    if language == "ar":
        intro = "بناءً على متطلباتك، هذه أفضل الخيارات المطابقة المتاحة لدي الآن:"
        outro = "أي خيار ترغب أن أشرح لك تفاصيله أكثر؟"
    else:
        intro = "Based on your requirements, these are the best matching options I have available right now:"
        outro = "Which option would you like to explore further?"
    return intro + "\n\n" + "\n".join(lines) + "\n\n" + outro

def qualification_missing_fields(session: dict) -> list:
    """Return only genuinely missing property requirements.

    Renters do not need an investment-vs-own-use question: renting a home is
    treated as personal use unless they explicitly say otherwise. This keeps
    qualification natural and prevents unnecessary loops.
    """
    lead = session.get("lead", {})
    interest = str(lead.get("interest") or "").lower()

    required = ["interest", "property_type", "area", "budget", "bedrooms", "timeline"]
    if "rent" not in interest:
        required.insert(-1, "purpose")

    return [k for k in required if not _valid_lead_value(lead.get(k))]


def qualification_ready(session: dict) -> bool:
    return not qualification_missing_fields(session)


def next_qualification_question(session: dict, language: str) -> str | None:
    """Ask exactly one missing sales-qualification question before recommendations."""
    missing = qualification_missing_fields(session)
    if not missing:
        return None
    field = missing[0]
    lead = session.get("lead", {})
    is_rent = "rent" in str(lead.get("interest") or "").lower()

    en = {
        "interest": "Are you looking to buy, rent, invest, or explore an off-plan property?",
        "property_type": "What type of property are you looking for — apartment, villa, or townhouse?",
        "area": "Which Dubai area or community do you prefer?",
        "budget": "What is your monthly rental budget?" if is_rent else "What budget or budget range are you working with?",
        "bedrooms": "How many bedrooms do you need?",
        "purpose": "Is the property for your own use or primarily as an investment?",
        "timeline": "When would you like to move in?" if is_rent else "When are you planning to move or complete the purchase?",
    }
    ar = {
        "interest": "هل تبحث عن شراء عقار، استئجار، استثمار، أم عقار على المخطط؟",
        "property_type": "ما نوع العقار الذي تبحث عنه: شقة، فيلا، أم تاون هاوس؟",
        "area": "ما المنطقة أو المجتمع الذي تفضله في دبي؟",
        "budget": "ما ميزانيتك الشهرية للإيجار؟" if is_rent else "ما الميزانية أو نطاق الميزانية المناسب لك؟",
        "bedrooms": "كم عدد غرف النوم التي تحتاجها؟",
        "purpose": "هل العقار للاستخدام الشخصي أم للاستثمار بشكل أساسي؟",
        "timeline": "متى ترغب في الانتقال؟" if is_rent else "متى تخطط للانتقال أو إتمام الشراء؟",
    }
    return (ar if language == "ar" else en).get(field)


def booking_required_fields_ready(session: dict) -> bool:
    """Booking is impossible until qualification, recommendations, and viewing consent are complete."""
    lead = session.get("lead", {})
    return (
        qualification_ready(session)
        and bool(session.get("recommendations_shown"))
        and bool(session.get("viewing_requested"))
        and all(_valid_lead_value(lead.get(k)) for k in ["viewing_date", "viewing_time", "name", "email", "phone"])
    )

def recommendation_stage_ready(session: dict) -> bool:
    return qualification_ready(session) and not session.get("recommendations_shown")

def viewing_can_start(session: dict) -> bool:
    return qualification_ready(session) and bool(session.get("recommendations_shown")) and bool(session.get("viewing_requested"))


def user_requested_booking(message: str) -> bool:
    """Detect clear user intent to schedule/book, not just asking property details."""
    msg = (message or "").lower().strip()
    booking_phrases = [
        "book", "schedule", "viewing", "view it", "see it", "visit",
        "arrange", "appointment", "showing", "tour", "lets view",
        "let's view", "i want to view", "can i view", "make booking",
        "set up", "reserve", "confirm viewing"
    ]
    positive_short = {"yes", "sure", "ok", "okay", "go ahead", "yes please", "book it", "schedule it"}
    return msg in positive_short or any(p in msg for p in booking_phrases)


def booking_started(session: dict) -> bool:
    """Contact/date collection is allowed only after recommendations and explicit viewing consent."""
    if not viewing_can_start(session):
        return False
    lead = session.get("lead", {})
    return any(_valid_lead_value(lead.get(k)) for k in ["viewing_date", "viewing_time", "name", "email", "phone"])


    
def _repeat_prefix(session: dict, field: str, language: str) -> str:
    """Make repeated asks sound intentional instead of broken."""
    asked = session.setdefault("asked_missing_fields", {})
    asked[field] = asked.get(field, 0) + 1
    if asked[field] <= 1:
        return ""

    if language == "ar":
        labels = {
            "viewing_datetime": "تاريخ ووقت المعاينة",
            "viewing_time": "وقت المعاينة",
            "viewing_date": "يوم المعاينة",
            "name": "الاسم",
            "email": "البريد الإلكتروني",
            "phone": "رقم الهاتف",
        }
        return f"عذراً، لم ألتقط {labels.get(field, 'هذه المعلومة')} بوضوح. "

    labels = {
        "viewing_datetime": "the viewing date and time",
        "viewing_time": "the viewing time",
        "viewing_date": "the viewing day",
        "name": "your name",
        "email": "your email",
        "phone": "your phone number",
    }
    return f"Sorry, I didn’t catch {labels.get(field, 'that detail')} clearly. "


def next_missing_booking_question(session: dict, language: str):
    """Ask the next missing booking question in a stable order without sounding repetitive."""
    lead = session.get("lead", {})

    has_date = _valid_lead_value(lead.get("viewing_date"))
    has_time = _valid_lead_value(lead.get("viewing_time"))

    if not has_date and not has_time:
        field = "viewing_datetime"
        question = (
            "What date and time would you prefer for the viewing?"
            if language == "en"
            else "ما التاريخ والوقت المناسبان للمعاينة؟"
        )
        return _repeat_prefix(session, field, language) + question

    if has_date and not has_time:
        field = "viewing_time"
        question = (
            "What time would you prefer for the viewing?"
            if language == "en"
            else "ما الوقت المناسب للمعاينة؟"
        )
        return _repeat_prefix(session, field, language) + question

    if has_time and not has_date:
        field = "viewing_date"
        question = (
            "What day would you prefer for the viewing?"
            if language == "en"
            else "ما اليوم المناسب للمعاينة؟"
        )
        return _repeat_prefix(session, field, language) + question

    if not _valid_lead_value(lead.get("name")):
        field = "name"
        question = (
            "Perfect, I can arrange that viewing. What name should I use for the booking?"
            if language == "en"
            else "ما اسمك الكريم؟"
        )
        return _repeat_prefix(session, field, language) + question

    if not _valid_lead_value(lead.get("email")):
        field = "email"
        question = (
            "What email should we use for the booking confirmation?"
            if language == "en"
            else "ما البريد الإلكتروني المناسب لإرسال تأكيد الحجز؟"
        )
        return _repeat_prefix(session, field, language) + question

    if not _valid_lead_value(lead.get("phone")):
        field = "phone"
        question = (
            "What phone number should the agent contact you on?"
            if language == "en"
            else "ما رقم الهاتف الذي يمكن للوكيل التواصل معك عليه؟"
        )
        return _repeat_prefix(session, field, language) + question

    return None

def fallback_missing_lead_question(session: dict, language: str) -> str:
    """Professional fallback prompt when the LLM is unavailable.

    Collects the minimum details needed for a human handoff and, if a viewing
    time is supplied too, allows the deterministic booking engine to proceed.
    """
    lead = session.get("lead", {})

    if not _valid_lead_value(lead.get("name")):
        return _repeat_prefix(session, "name", language) + (
            "Our team is currently busy and will contact you shortly. Kindly share your name."
            if language == "en"
            else "فريقنا مشغول حالياً وسيتواصل معك قريباً. يرجى إرسال الاسم."
        )
    if not _valid_lead_value(lead.get("email")):
        return _repeat_prefix(session, "email", language) + (
            "Thank you. Please share your email address."
            if language == "en"
            else "شكراً. يرجى إرسال البريد الإلكتروني."
        )
    if not _valid_lead_value(lead.get("phone")):
        return _repeat_prefix(session, "phone", language) + (
            "Please share your phone number so our team can contact you."
            if language == "en"
            else "يرجى إرسال رقم الهاتف حتى يتمكن فريقنا من التواصل معك."
        )
    if not _valid_lead_value(lead.get("interest")):
        return (
            "What type of property are you interested in — apartment, villa, townhouse, rent, buy, or investment?"
            if language == "en"
            else "ما نوع العقار الذي تهتم به؟ شقة، فيلا، تاون هاوس، إيجار، شراء، أم استثمار؟"
        )
    if not _valid_lead_value(lead.get("area")):
        return (
            "Which area are you interested in?"
            if language == "en"
            else "ما المنطقة التي تهتم بها؟"
        )
    if not _valid_lead_value(lead.get("viewing_date")) or not _valid_lead_value(lead.get("viewing_time")):
        return (
            "Our team is busy right now, we will contact you soon kindly let us know What date and time would you prefer for the viewing?"
            if language == "en"
            else "ما التاريخ والوقت المناسبان للمعاينة؟"
        )

    return (
        "Thank you. Our team will contact you shortly."
        if language == "en"
        else "شكراً. سيتواصل معك فريقنا قريباً."
    )


def fallback_booking_ready(session: dict) -> bool:
    """Fallback should not create a booking until all handoff details are present."""
    lead = session.get("lead", {})
    required = ["name", "email", "phone", "interest", "area", "viewing_date", "viewing_time"]
    return all(_valid_lead_value(lead.get(k)) for k in required)


def fallback_collect_and_book_response(session: dict, user_message: str, language: str, source: str):
    """Rule-based booking flow used only when the LLM is unavailable/rate-limited.

    It never sounds broken. It collects name, email, phone, property type, area,
    and only creates a booking after those details plus viewing date/time exist.
    """
    lead = session.setdefault("lead", {})

    # If user gives everything in one message, best-effort extract contact again.
    contact = extract_contact_from_history(session.get("history", []) + [{"role": "user", "content": user_message}])
    for key in ["name", "email", "phone"]:
        if contact.get(key) and not lead.get(key):
            lead[key] = contact[key]

    booking = maybe_build_booking_from_state(session, language)
    if booking and not session.get("booking_made") and fallback_booking_ready(session):
        return {
            "action": "book",
            "booking": booking,
            "reply": f"Confirmed. Your viewing is scheduled for {booking.get('viewing_date')} at {booking.get('viewing_time')}. Our agent will contact you shortly."
            if language == "en"
            else f"تم التأكيد. موعد المعاينة بتاريخ {booking.get('viewing_date')} الساعة {booking.get('viewing_time')}. سيتواصل معك الوكيل قريباً.",
        }

    return {
        "action": "ask",
        "reply": fallback_missing_lead_question(session, language),
    }


async def post_booking_fallback_reply(user_message: str, session: dict, language: str) -> str:
    """Safe fallback after booking is completed.

    Important: after booking, users may still ask normal questions. If Cerebras fails
    or rate-limits, we still answer simple company/property questions instead of
    repeating "your booking is confirmed".
    """
    msg = (user_message or "").lower()
    lead = session.get("lead", {})
    interest = lead.get("interest") or "the selected property"
    vdate = lead.get("viewing_date") or "the confirmed date"
    vtime = lead.get("viewing_time") or "the confirmed time"

    company = {}
    try:
        company = await get_company_info()
    except Exception:
        company = {}

    company_name = _safe_get(company, "company_name") or AGENCY
    company_about = _safe_get(company, "about") or AGENCY_CONTEXT
    company_home = _safe_get(company, "home")
    company_contact = _safe_get(company, "contact_info")

    # Company identity questions
    if any(x in msg for x in [
        "company name", "your company", "agency name", "who are you",
        "what is your name", "brokerage name", "which company"
    ]):
        if language == "ar":
            return f"اسم الشركة هو {company_name}. نحن مساعد عقاري تابع لها، ونساعدك في اختيار العقارات وحجز المعاينات."
        return f"Our company is {company_name}. I’m the property assistant for the brokerage, helping with property options, details, and viewing bookings."

    # Company/about questions
    if any(x in msg for x in ["about company", "about your company", "tell me about", "company about", "what do you do"]):
        short_about = shorten(company_about, 35)
        if language == "ar":
            return f"{company_name} هي شركة وساطة عقارية في دبي. {short_about}"
        return f"{company_name} is a Dubai real estate brokerage. {short_about}"

    # Contact questions
    if any(x in msg for x in ["contact", "phone", "email", "number", "call", "whatsapp"]):
        if company_contact:
            if language == "ar":
                return f"يمكنك التواصل مع {company_name} عبر: {company_contact}"
            return f"You can contact {company_name} here: {company_contact}"
        if language == "ar":
            return f"يمكن لفريق {company_name} التواصل معك قريباً لتأكيد التفاصيل."
        return f"The {company_name} team can contact you shortly to confirm the details."

    if language == "ar":
        if any(w in msg for w in ["وين", "أين", "الموقع", "العنوان"]):
            return "سيشارك الوكيل الموقع الدقيق وتفاصيل الوصول قبل موعد المعاينة."
        if any(w in msg for w in ["تفاصيل", "العقار", "الملكية", "المشروع"]):
            return f"المعاينة مؤكدة للعقار المطلوب: {interest}. سيشرح لك الوكيل التفاصيل الكاملة أثناء التواصل."
        if any(w in msg for w in ["وقت", "موعد", "تاريخ"]):
            return f"موعد المعاينة مؤكد بتاريخ {vdate} الساعة {vtime}. سيتواصل معك الوكيل قريباً."
        return "تم تأكيد الحجز. يمكنك أيضاً سؤالي عن الشركة، العقار، المنطقة، الأسعار، أو الخطوات التالية."

    if any(w in msg for w in ["where", "location", "address", "directions"]):
        return "The exact viewing location and directions will be shared by the agent before the appointment."
    if any(w in msg for w in ["property", "details", "showing", "viewing", "project"]):
        return f"You are booked for: {interest}. The agent will share full property details, availability, and next steps before the viewing."
    if any(w in msg for w in ["time", "date", "when"]):
        return f"Your viewing is confirmed for {vdate} at {vtime}. The agent will contact you shortly."
    if any(w in msg for w in ["price", "cost", "payment", "plan", "budget"]):
        return "The agent will confirm the exact price, payment plan, and availability before your viewing."

    return f"Your booking is confirmed. You can also ask me about {company_name}, the property, area, pricing, or next steps."


async def handle_llm_failure(session: dict, user_message: str, language: str, source: str, error: Exception) -> dict:
    """Fallback path when Cerebras/LLM fails.

    If booking is not completed, deterministic fallback continues collecting details
    and can still create the booking. If booking is already completed, LLM is only
    bypassed for this failed request and a safe post-booking answer is returned.
    """
    print(f"[LLM Fallback Engine]: {error}")

    # IMPORTANT: After booking, do not restart booking flow and do not ask for details again.
    # This is only a temporary fallback for the failed LLM request.
    if session.get("booking_made"):
        reply = await post_booking_fallback_reply(user_message, session, language)
        session["history"].append({"role": "user", "content": user_message})
        session["history"].append({"role": "assistant", "content": reply})

        save_chat_log(session.get("id"), user_message, reply)
        
        if len(session["history"]) > 8:
            session["history"] = session["history"][-8:]
        return {
            "reply": reply,
            "language": language,
            "lead_captured": True,
            "booking_made": True,
            "viewing_date": session.get("lead", {}).get("viewing_date"),
            "viewing_time": session.get("lead", {}).get("viewing_time"),
            "name": session.get("lead", {}).get("name"),
            "email": session.get("lead", {}).get("email"),
            "phone": session.get("lead", {}).get("phone"),
            "interest": session.get("lead", {}).get("interest"),
            "budget": session.get("lead", {}).get("budget"),
            "area": session.get("lead", {}).get("area"),
            "property_type": session.get("lead", {}).get("property_type"),
            "bedrooms": session.get("lead", {}).get("bedrooms"),
            "purpose": session.get("lead", {}).get("purpose"),
            "timeline": session.get("lead", {}).get("timeline"),
            "booking_status": session.get("booking_status", {}),
            "fallback_mode": True,
        }

    fallback = fallback_collect_and_book_response(session, user_message, language, source)
    booking = fallback.get("booking")
    booking_made = False

    if fallback.get("action") == "book" and booking:
        await _process_booking(booking, session, language, source)
        booking_made = True

    reply = fallback.get("reply") or "Got it. What is the next detail?"

    session["history"].append({"role": "user", "content": user_message})
    session["history"].append({"role": "assistant", "content": reply})
    if len(session["history"]) > 8:
        session["history"] = session["history"][-8:]

    return {
        "reply": reply,
        "language": language,
        "lead_captured": booking_made,
        "booking_made": booking_made,
        "viewing_date": booking.get("viewing_date") if booking else session.get("lead", {}).get("viewing_date"),
        "viewing_time": booking.get("viewing_time") if booking else session.get("lead", {}).get("viewing_time"),
        "name": booking.get("name") if booking else session.get("lead", {}).get("name"),
        "email": booking.get("email") if booking else session.get("lead", {}).get("email"),
        "phone": booking.get("phone") if booking else session.get("lead", {}).get("phone"),
        "interest": booking.get("interest") if booking else session.get("lead", {}).get("interest"),
        "budget": booking.get("budget") if booking else session.get("lead", {}).get("budget"),
        "area": booking.get("area") if booking else session.get("lead", {}).get("area"),
        "property_type": booking.get("property_type") if booking else session.get("lead", {}).get("property_type"),
        "bedrooms": booking.get("bedrooms") if booking else session.get("lead", {}).get("bedrooms"),
        "purpose": booking.get("purpose") if booking else session.get("lead", {}).get("purpose"),
        "timeline": booking.get("timeline") if booking else session.get("lead", {}).get("timeline"),
        "booking_status": session.get("booking_status", {}),
        "fallback_mode": True,
    }


async def handle_fallback(session: dict, user_message: str, language: str, source: str) -> dict:
    """Compatibility wrapper: use deterministic fallback instead of another LLM call."""
    return await handle_llm_failure(
        session=session,
        user_message=user_message,
        language=language,
        source=source,
        error=Exception("Fallback mode active"),
    )


async def _process_booking(booking: dict, session: dict, language: str, source: str):
    """Save lead, create calendar event, send emails and WhatsApp."""
    booking = normalize_booking_datetime(booking)
    from calendar_service import create_viewing_event
    from email_service import send_booking_confirmation, send_agent_notification
    from whatsapp import send_booking_whatsapp, notify_agent_whatsapp

    lead = Lead(
        session_id=session.get("id", "unknown"),
        name=booking.get("name"),
        email=booking.get("email"),
        phone=booking.get("phone"),
        interest=booking.get("interest"),
        budget=booking.get("budget"),
        area=booking.get("area"),
        viewing_date=booking.get("viewing_date"),
        viewing_time=booking.get("viewing_time"),
        language=language,
        source=source,
        status="booking_sent",
    )
    save_lead(lead)
    session["booking_made"] = True
    session.setdefault("lead", {}).update({
        "name": booking.get("name"),
        "email": booking.get("email"),
        "phone": booking.get("phone"),
        "interest": booking.get("interest"),
        "budget": booking.get("budget"),
        "area": booking.get("area"),
        "property_type": booking.get("property_type"),
        "bedrooms": booking.get("bedrooms"),
        "purpose": booking.get("purpose"),
        "timeline": booking.get("timeline"),
        "viewing_date": booking.get("viewing_date"),
        "viewing_time": booking.get("viewing_time"),
    })

    vdate = booking.get("viewing_date", "TBD")
    vtime = booking.get("viewing_time", "TBD")
    name = booking.get("name", "Client")
    email = booking.get("email", "")
    phone = booking.get("phone", "")
    intr = booking.get("interest", "Dubai Property")

    calendar_status = "pending"
    email_status = "pending"
    agent_email_status = "pending"
    whatsapp_status = "skipped"

    # Google Calendar
    try:
        if email:
            create_viewing_event(client_name=name, client_email=email, property_interest=intr, date_str=vdate, time_str=vtime)
            calendar_status = "created"
        else:
            calendar_status = "skipped_no_email"
    except Exception as e:
        calendar_status = "failed"
        print(f"[Calendar] Skipped: {e}")

    # Emails
    try:
        if email:
            send_booking_confirmation(name, email, intr, vdate, vtime)
            email_status = "sent"
            send_agent_notification(name, email, phone, intr, booking.get("budget", ""), booking.get("area", ""), vdate, vtime, language)
            agent_email_status = "sent"
        else:
            email_status = "skipped_no_email"
            agent_email_status = "skipped_no_email"
    except Exception as e:
        email_status = "failed"
        agent_email_status = "failed"
        print(f"[Email] Skipped: {e}")

    # WhatsApp: skip if test/placeholders are still in .env
    try:
        wa_token = os.getenv("WHATSAPP_TOKEN", "")
        wa_phone_id = os.getenv("WHATSAPP_PHONE_ID", "")
        if not is_placeholder(wa_token) and not is_placeholder(wa_phone_id):
            if phone:
                await send_booking_whatsapp(phone, name, intr, vdate, vtime)
                whatsapp_status = "sent"
            await notify_agent_whatsapp(name, phone or "N/A", intr, vdate, vtime)
        else:
            whatsapp_status = "skipped_demo"
            print("[WhatsApp] Skipped: WhatsApp demo credentials not configured.")
    except Exception as e:
        whatsapp_status = "failed"
        print(f"[WhatsApp] Skipped: {e}")

    session["booking_status"] = {
        "calendar": calendar_status,
        "email": email_status,
        "agent_email": agent_email_status,
        "whatsapp": whatsapp_status,
    }
def make_cache_key(session_id: str, message: str) -> str:
    key = f"{session_id}:{message.lower().strip()}"
    return md5(key.encode()).hexdigest()

async def get_ai_response(session_id: str, user_message: str, source: str = "website") -> dict:
    language = detect_language(user_message)

    # The frontend now sends a new demo_* session_id on every refresh.
    # This cleanup keeps Railway memory clean and prevents old demo states from
    # accidentally being reused if a stale tab sends an old id.
    cleanup_old_demo_sessions()

    if not session_id:
        session_id = f"demo_backend_{int(time.time())}_{md5(user_message.encode()).hexdigest()[:8]}"

    if session_id not in _sessions:
        _sessions[session_id] = {
            "id": session_id,
            "history": [],
            "system_prompt": None,
            "booking_made": False,
            "msg_count": 0,
            "window_start": datetime.utcnow(),
            "last_seen": datetime.utcnow(),
            "fallback_mode": False,
            "fallback_history": [],
            "lead": {},
            "asked_missing_fields": {},
            "recommendations_shown": False,
            "recommended_options": [],
            "viewing_requested": False,
        }

    session = _sessions[session_id]
    session["last_seen"] = datetime.utcnow()

    cache_key = make_cache_key(session_id, user_message)

    lead = session.get("lead", {})
    is_booking_flow = any([
        lead.get("name"),
        lead.get("email"),
        lead.get("phone"),
        lead.get("viewing_date"),
        lead.get("viewing_time"),
        session.get("booking_made"),
    ])

    is_demo_session = str(session_id).startswith("demo_") or source == "demo"

    # Check cache only for simple/general queries.
    # Demo sessions skip cache so every refresh feels like a fresh live conversation.
    # Do not cache lead-capture or booking-flow messages.
    if not is_booking_flow and not is_demo_session:
        cached = CACHE.get(cache_key)
        if cached:
            if time.time() - cached["time"] < CACHE_TTL:
                print("[CACHE HIT]")
                return cached["response"]
            else:
                del CACHE[cache_key]

    lead_state = update_lead_state_from_message(session, user_message)

    # HARD QUALIFICATION GATE --------------------------------------------------
    # Qualification is controlled by Python, not by Cerebras. Once a value is
    # captured it is never re-asked unless the user explicitly changes it.
    if not qualification_ready(session):
        q = next_qualification_question(session, language)
        if q:
            session["history"].append({"role": "user", "content": user_message})
            session["history"].append({"role": "assistant", "content": q})
            if len(session["history"]) > 8:
                session["history"] = session["history"][-8:]
            save_chat_log(session_id, user_message, q)
            return {
                "reply": q, "language": language, "lead_captured": False, "booking_made": False,
                "viewing_date": session.get("lead", {}).get("viewing_date"),
                "viewing_time": session.get("lead", {}).get("viewing_time"),
                "name": session.get("lead", {}).get("name"), "email": session.get("lead", {}).get("email"),
                "phone": session.get("lead", {}).get("phone"), "interest": session.get("lead", {}).get("interest"),
                "budget": session.get("lead", {}).get("budget"), "area": session.get("lead", {}).get("area"),
                "property_type": session.get("lead", {}).get("property_type"), "bedrooms": session.get("lead", {}).get("bedrooms"),
                "purpose": session.get("lead", {}).get("purpose"), "timeline": session.get("lead", {}).get("timeline"),
                "booking_status": session.get("booking_status", {}),
            }

    # A viewing can only be requested after options have been shown.
    if session.get("recommendations_shown") and user_requested_booking(user_message):
        session["viewing_requested"] = True

    # HARD RECOMMENDATION GATE: once qualification is complete, show real database options
    # before any viewing/contact collection. Do not rely on the LLM to remember this step.
    if recommendation_stage_ready(session):
        reply = await build_matching_options_reply(session, language)
        session["recommendations_shown"] = bool(session.get("recommended_options"))
        session["history"].append({"role": "user", "content": user_message})
        session["history"].append({"role": "assistant", "content": reply})
        if len(session["history"]) > 8:
            session["history"] = session["history"][-8:]
        save_chat_log(session_id, user_message, reply)
        return {
            "reply": reply, "language": language, "lead_captured": False, "booking_made": False,
            "viewing_date": session.get("lead", {}).get("viewing_date"),
            "viewing_time": session.get("lead", {}).get("viewing_time"),
            "name": session.get("lead", {}).get("name"), "email": session.get("lead", {}).get("email"),
            "phone": session.get("lead", {}).get("phone"), "interest": session.get("lead", {}).get("interest"),
            "budget": session.get("lead", {}).get("budget"), "area": session.get("lead", {}).get("area"),
            "property_type": session.get("lead", {}).get("property_type"), "bedrooms": session.get("lead", {}).get("bedrooms"),
            "purpose": session.get("lead", {}).get("purpose"), "timeline": session.get("lead", {}).get("timeline"),
            "recommendations_shown": session.get("recommendations_shown", False),
            "recommended_options": [{"index": x["index"], "name": x["name"]} for x in session.get("recommended_options", [])],
            "booking_status": session.get("booking_status", {}),
        }

    # If the client gave a viewing date/time before contact details,
    # NEVER confirm booking yet. Collect name, email, and phone first.
    if (
        not session.get("booking_made")
        and (
            _valid_lead_value(session.get("lead", {}).get("viewing_date"))
            or _valid_lead_value(session.get("lead", {}).get("viewing_time"))
        )
        and not booking_required_fields_ready(session)
        and (not qualification_ready(session) or session.get("recommendations_shown"))
    ):
        # A user may mention a viewing time early. Keep it in memory, but finish
        # property qualification before collecting name/email/phone.
        q = next_qualification_question(session, language)
        # If qualification is complete but recommendations have not been shown,
        # do NOT enter booking collection. Continue below so Cerebras presents options.
        if not q and not session.get("recommendations_shown"):
            pass
        elif not q and not session.get("viewing_requested"):
            reply = (
                "I’ll first show you the best matching options. Tell me which one interests you, then I can arrange a viewing."
                if language == "en" else
                "سأعرض لك أولاً أفضل الخيارات المطابقة. اختر ما يناسبك، وبعدها يمكنني ترتيب المعاينة."
            )
        else:
            reply = q or next_missing_booking_question(session, language)
        session["history"].append({"role": "user", "content": user_message})
        session["history"].append({"role": "assistant", "content": reply})
        if len(session["history"]) > 8:
            session["history"] = session["history"][-8:]
        save_chat_log(session_id, user_message, reply)
        return {
            "reply": reply,
            "language": language,
            "lead_captured": False,
            "booking_made": False,
            "viewing_date": session.get("lead", {}).get("viewing_date"),
            "viewing_time": session.get("lead", {}).get("viewing_time"),
            "name": session.get("lead", {}).get("name"),
            "email": session.get("lead", {}).get("email"),
            "phone": session.get("lead", {}).get("phone"),
            "interest": session.get("lead", {}).get("interest"),
            "budget": session.get("lead", {}).get("budget"),
            "area": session.get("lead", {}).get("area"),
            "property_type": session.get("lead", {}).get("property_type"),
            "bedrooms": session.get("lead", {}).get("bedrooms"),
            "purpose": session.get("lead", {}).get("purpose"),
            "timeline": session.get("lead", {}).get("timeline"),
            "booking_status": session.get("booking_status", {}),
        }


    now = datetime.utcnow()
    if now - session["window_start"] > timedelta(hours=1):
        session["msg_count"] = 0
        session["window_start"] = now

    if session["msg_count"] >= 25:
        msg = "You've reached the session limit. Please contact us directly." if language == "en" else "لقد تجاوزت حد المحادثة. يرجى التواصل معنا مباشرة."
        return {"reply": msg, "language": language, "lead_captured": False, "booking_made": False}

    session["msg_count"] += 1

    # If we already have all details, book immediately before asking the AI again.
    booking = maybe_build_booking_from_state(session, language)
    if booking and not session["booking_made"] and booking_required_fields_ready(session):
        await _process_booking(booking, session, language, source)
        reply = f"Confirmed. Your viewing is scheduled for {booking.get('viewing_date')} at {booking.get('viewing_time')}. Our agent will contact you shortly."
        session["history"].append({"role": "user", "content": user_message})
        session["history"].append({"role": "assistant", "content": reply})
        return {
            "reply": reply,
            "language": language,
            "lead_captured": True,
            "booking_made": True,
            "viewing_date": booking.get("viewing_date"),
            "viewing_time": booking.get("viewing_time"),
            "name": booking.get("name"),
            "email": booking.get("email"),
            "phone": booking.get("phone"),
            "interest": booking.get("interest"),
            "budget": booking.get("budget"),
            "area": booking.get("area"),
            "property_type": booking.get("property_type"),
            "bedrooms": booking.get("bedrooms"),
            "purpose": booking.get("purpose"),
            "timeline": booking.get("timeline"),
            "booking_status": session.get("booking_status", {}),
        }

    # Build a small, relevant prompt per message. Full sheet data remains in Python,
    # but only matching rows are sent to Cerebras to reduce token usage.
    retrieval_query = " ".join(
        [user_message] + [
            m.get("content", "")
            for m in session.get("history", [])[-4:]
            if m.get("role") == "user"
        ]
    )
    session["system_prompt"] = await build_system_prompt(language, retrieval_query)

    messages = [{"role": "system", "content": session["system_prompt"]}]

    lead_context = build_lead_context(session.get("lead", {}))
    if lead_context:
        messages.append({"role": "system", "content": lead_context})

    messages += history_to_messages(session["history"])

    lead_now = session.get("lead", {})
    booking_flow_active_now = any([
        lead_now.get("viewing_date"),
        lead_now.get("viewing_time"),
        lead_now.get("name"),
        lead_now.get("email"),
        lead_now.get("phone"),
    ])

    llm_user_message = user_message
    if recommendation_stage_ready(session):
        llm_user_message += (
            "\n\nMANDATORY NEXT ACTION: Qualification is complete. Present 2–3 best matching property/project options "
            "from the supplied PROPERTY DATA now. Include names and available price/location/payment-plan/amenity facts. "
            "Do not ask for viewing date, name, email, or phone. End by asking which option interests the client."
        )
    if booking_flow_active_now and is_user_query(user_message):
        llm_user_message = (
            user_message
            + "\n\nImportant: Answer this question first using available property data. "
            + "Do not ignore it. After answering, you may continue with the next missing booking detail."
        )

    messages.append({"role": "user", "content": llm_user_message})

    try:
        response = client.chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=700 if recommendation_stage_ready(session) else 500,
        )
        raw = response.choices[0].message.content
    except Exception as e:
        error_str = str(e)
        print(f"[Cerebras Error]: {error_str}")
        if "429" in error_str or "rate_limit" in error_str.lower():
            session["msg_count"] -= 1
        return await handle_llm_failure(session, user_message, language, source, e)

    was_recommendation_stage = recommendation_stage_ready(session)
    # Do not hard-truncate Cerebras output. The system prompt controls normal reply
    # length; hard word/token clipping can leave questions visibly unfinished.
    cleaned = clean_reply(raw)
    reply = cleaned.strip()
    if was_recommendation_stage and reply:
        session["recommendations_shown"] = True
    booking = extract_booking(raw) if not session["booking_made"] else None

    # ✅ HANDLE USER QUERY DURING BOOKING FLOW
    if is_user_query(user_message):
        ai_reply = reply  # AI answer first

        # Only continue booking questions after booking collection has started.
        # This prevents feature questions like "sea facing?" from triggering date/time requests.
        next_q = next_missing_booking_question(session, language) if booking_started(session) else None

        if next_q:
            reply = f"{ai_reply}\n\n{next_q}"
        else:
            reply = ai_reply
            
    # After booking, keep normal LLM conversation. Do not ask booking questions again.
    if session.get("booking_made"):
        session["history"].append({"role": "user", "content": user_message})
        session["history"].append({"role": "assistant", "content": reply or "Got it. How can I help you next?"})
        if len(session["history"]) > 8:
            session["history"] = session["history"][-8:]
        return {
            "reply": reply or "Got it. How can I help you next?",
            "language": language,
            "lead_captured": True,
            "booking_made": True,
            "viewing_date": session.get("lead", {}).get("viewing_date"),
            "viewing_time": session.get("lead", {}).get("viewing_time"),
            "name": session.get("lead", {}).get("name"),
            "email": session.get("lead", {}).get("email"),
            "phone": session.get("lead", {}).get("phone"),
            "interest": session.get("lead", {}).get("interest"),
            "budget": session.get("lead", {}).get("budget"),
            "area": session.get("lead", {}).get("area"),
            "property_type": session.get("lead", {}).get("property_type"),
            "bedrooms": session.get("lead", {}).get("bedrooms"),
            "purpose": session.get("lead", {}).get("purpose"),
            "timeline": session.get("lead", {}).get("timeline"),
            "booking_status": session.get("booking_status", {}),
        }

    if not reply:
        q = next_missing_booking_question(session, language) if viewing_can_start(session) else next_qualification_question(session, language)
        reply = q or "Which of the matching property options would you like to explore further?"

    if booking and not viewing_can_start(session):
        booking = None

    if booking:
        booking = normalize_booking_datetime(booking)
        # Merge any deterministic state that the LLM missed
        state = session.get("lead", {})
        for k in ["name", "email", "phone", "interest", "budget", "area", "property_type", "bedrooms", "purpose", "timeline", "viewing_date", "viewing_time"]:
            if state.get(k) and not booking.get(k):
                booking[k] = state[k]

        # Do not process a booking until required contact details exist.
        session_lead = session.setdefault("lead", {})
        for k in ["name", "email", "phone", "interest", "budget", "area", "property_type", "bedrooms", "purpose", "timeline", "viewing_date", "viewing_time"]:
            if booking.get(k):
                session_lead[k] = booking.get(k)

        required_ready = booking_required_fields_ready(session)
        if required_ready:
            booking = maybe_build_booking_from_state(session, language) or booking
            await _process_booking(booking, session, language, source)
            reply = f"Confirmed. Your viewing is scheduled for {booking.get('viewing_date')} at {booking.get('viewing_time')}. Our agent will contact you shortly."
            booking_made = True
        else:
            # Hard stop: never process or expose a fake booking with missing contact details.
            booking = None
            booking_made = False
            q = next_missing_booking_question(session, language)
            reply = q or (
                "Perfect, I can arrange that viewing. What name should I use for the booking?"
                if language == "en"
                else "تمام، يمكنني ترتيب المعاينة. ما الاسم الذي أستخدمه للحجز؟"
            )
    else:
        booking_made = False
        

    session["history"].append({"role": "user", "content": user_message})
    session["history"].append({"role": "assistant", "content": reply})

    save_chat_log(session_id, user_message, reply)

    if len(session["history"]) > 8:
        session["history"] = session["history"][-8:]

    booking_for_response = booking if booking_made else None

    result = {
        "reply": reply,
        "language": language,
        "lead_captured": booking_made,
        "booking_made": booking_made,
        "viewing_date": booking_for_response.get("viewing_date") if booking_for_response else None,
        "viewing_time": booking_for_response.get("viewing_time") if booking_for_response else None,
        "name": booking_for_response.get("name") if booking_for_response else session.get("lead", {}).get("name"),
        "email": booking_for_response.get("email") if booking_for_response else session.get("lead", {}).get("email"),
        "phone": booking_for_response.get("phone") if booking_for_response else session.get("lead", {}).get("phone"),
        "interest": booking_for_response.get("interest") if booking_for_response else session.get("lead", {}).get("interest"),
        "budget": booking_for_response.get("budget") if booking_for_response else session.get("lead", {}).get("budget"),
        "area": booking_for_response.get("area") if booking_for_response else session.get("lead", {}).get("area"),
        "property_type": booking_for_response.get("property_type") if booking_for_response else session.get("lead", {}).get("property_type"),
        "bedrooms": booking_for_response.get("bedrooms") if booking_for_response else session.get("lead", {}).get("bedrooms"),
        "purpose": booking_for_response.get("purpose") if booking_for_response else session.get("lead", {}).get("purpose"),
        "timeline": booking_for_response.get("timeline") if booking_for_response else session.get("lead", {}).get("timeline"),
        "booking_status": session.get("booking_status", {}),
    }

    # Save only simple, non-booking responses to cache.
    if not is_booking_flow and not booking_made and not is_demo_session:
        CACHE[cache_key] = {
            "response": result,
            "time": time.time(),
        }
        print("[CACHE SAVE]")

    return result
