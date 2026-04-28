import os
import re
import json
import dateparser
from logger import save_chat_log
from database import save_lead
from openai import OpenAI
from datetime import datetime, timedelta
from database import get_all_services, get_faqs, get_areas, get_projects, get_developers, get_payment_plans, get_crawled_properties 
from models import Lead
from hashlib import md5
import time

CACHE = {}
CACHE_TTL = 300  # 5 minutes

client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
AGENT_NAME = os.getenv("AGENT_NAME", "Omar Hassan")
AGENCY = os.getenv("AGENCY_NAME", "Zahra Signature Realty", "In a city built on ambition and inspired by dreams, Zahra Signature Realty was founded with a clear purpose to redefine real estate through trust, innovation, and the human touch. inspired by the visionary leadership of His Highness Sheikh Mohammed bin Rashid Al Maktoum, whose vision made Dubai a global capital of ambition and innovation.

"I founded this company to contribute to Dubai’s growth, not as a follower, but as a creator — delivering signature experiences to every client and respect and recognition to the agents who make it all possible.

"At ZSR, real estate isn’t just about transactions – it’s about trust, transformation, and lasting impact. Create experiences clients will never forget, celebrate agents as heros and shape a culture of contribution, excellence, and integrity.")

SYSTEM_PROMPT = """You are a professional real estate assistant for {AGENCY}, a Dubai property brokerage.

## LANGUAGE RULES — CRITICAL
- Arabic input → reply ONLY in formal Gulf Arabic
- English input → reply ONLY in English
- NEVER mix languages

## YOUR ROLE
Help clients find the right property, answer questions, and book viewings.
You are knowledgeable, concise, and trustworthy.

## STRICT REPLY RULES
- Maximum 50 words per reply
- Maximum 2 sentences
- Ask ONLY one question at a time
- Never repeat what the client already told you
- Always end with one clear question or next step

## CONVERSATION FLOW
1. Greet briefly and ask what they are looking for: buying, renting, investing, or off-plan.
2. Ask key qualifying questions ONE AT A TIME:
   - Property type if unknown
   - Preferred area if unknown
   - Budget if unknown
   - Timeline if unknown
3. Recommend one relevant service, project, or area from the data below.
4. If the client shows interest, tell them  more about the property details, how many room in the apartments, details for the villa etc, and the amenities it has.
5. ask them if they want to know more about the property or move to booking, if they want to book, schedule the viewing
6. For booking, collect missing details ONE AT A TIME in this order:
   - viewing date/time
   - name
   - email
   - phone
7. If the user already gave a detail, do NOT ask for it again.
8. After a booking is confirmed, continue answering the user's questions normally.
   - If they ask about the booked property, area, viewing, pricing, or next steps, answer helpfully using available property data.
   - Do NOT keep asking for booking details after booking is completed.
   - Do NOT say only “How can I help you next?” when the user asks a real question.

## BOOKING BLOCK — STRICT FORMAT
Only output a booking block when you have ALL of these:
- name
- valid email
- phone number
- viewing date/time

You MUST output booking ONLY in this exact JSON format:
<BOOKING>{"name":"...","email":"...","phone":"...","interest":"...","budget":"...","area":"...","viewing_date":"...","viewing_time":"...","language":"..."}</BOOKING>

Rules:
- Tell details about the property before asking about scheduling.
- Always answer the client's actual question first, even if it is in the middle of booking or contact collection. After answering, continue with the next missing booking detail.
- MUST be valid JSON.
- MUST use double quotes.
- DO NOT output plain text inside BOOKING.
- DO NOT output partial booking.
- DO NOT show the BOOKING block to the user as normal text.
- Users may say natural language like "tomorrow at 10 am", "next Monday evening", or "Friday 3pm".
- If date/time is given naturally, include it in viewing_date/viewing_time and the backend will normalize it.

## PRICING & DATA RULES
- Only mention prices from the data below — never guess
- For custom pricing say a specialist will provide a quote
- Never offer discounts — refer to a human manager
- If asked about something not in the data, say a specialist can assist

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


async def build_system_prompt(language: str) -> str:
    services = await get_all_services()
    faqs = await get_faqs(language)
    areas = await get_areas()
    projects = await get_projects()
    developers = await get_developers()
    plans = await get_payment_plans()
    crawled = await get_crawled_properties()  # ✅ NEW

    svc = "\n".join(f"- {s['name']}: {s['price_range']} — {s['description']}" for s in services)
    faq = "\n".join(f"Q: {f['question']}\nA: {f['answer']}" for f in faqs[:12])
    area = "\n".join(f"- {a['name']}: AED {a['price']} psf, {a['yield']} yield — {a['best_for']}" for a in areas[:8])
    proj = "\n".join(f"- {p['name']} by {p['developer']} in {p['location']}: from AED {p['price']}, {p['plan']}, handover {p['handover']}" for p in projects[:8])
    dev = "\n".join(f"- {d['name']}: {d['known_for']}" for d in developers[:6])
    plan = "\n".join(f"- {p['plan']}: {p['structure']} — {p['description']}" for p in plans[:5])

    # ✅ NEW: Crawled properties injected
    crawled_data = "\n".join(
        f"- {p['name']} in {p['location']} by {p['developer']} ({p['type']}): {p['description']}"
        for p in crawled[:10]
    )

    data = (
        f"### SERVICES\n{svc}\n\n"
        f"### KEY AREAS\n{area}\n\n"
        f"### OFF-PLAN PROJECTS\n{proj}\n\n"
        f"### DEVELOPERS\n{dev}\n\n"
        f"### PAYMENT PLANS\n{plan}\n\n"
        f"### CRAWLED PROPERTIES\n{crawled_data}\n\n"  # ✅ NEW BLOCK
        f"### FAQS\n{faq}"
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
    words = (text or "").split()
    if len(words) <= max_w:
        return text or ""
    trimmed = " ".join(words[:max_w])
    sentences = re.split(r'(?<=[.!?])\s+', trimmed)
    return sentences[0].strip() if len(sentences) > 1 else trimmed


def history_to_messages(history: list) -> list:
    return [{"role": m["role"], "content": m.get("content", "")} for m in history]


def build_history_text(history: list) -> str:
    lines = [
        f"{'User' if m['role']=='user' else 'Assistant'}: {m.get('content','')}"
        for m in history[-6:]
    ]
    return "\n".join(lines) or "No prior conversation."


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
            "RELATIVE_BASE": datetime.now(),
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if parsed:
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
            "RELATIVE_BASE": datetime.now(),
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
        "confirmation for what", "tomorrow", "today"
    }
    for msg in user_texts:
        clean = msg.strip()
        lowered = clean.lower()
        if not clean or lowered in ignored:
            continue
        if "@" in clean or re.search(r"\d", clean) or looks_like_datetime(clean):
            continue
        if len(clean.split()) <= 4 and len(clean) <= 40:
            name = clean
    return {
        "name": name,
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(0).strip() if phone_match else "",
    }


def update_lead_state_from_message(session: dict, user_message: str):
    """Store booking/contact details deterministically so the AI cannot lose them."""
    lead = session.setdefault("lead", {})
    msg = user_message.strip()

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
    budget_match = re.search(r"\b(?:aed\s*)?\d+(?:\.\d+)?\s*(?:k|K|m|M|million|aed|dirham|dirhams|usd|\$)?\b", msg)
    if budget_match and re.search(r"\b(budget|aed|dirham|dirhams|million|\d+\s*[kKmM])\b", msg, re.I):
        lead["budget"] = budget_match.group(0).strip()

    # Area extraction
    areas = [
        "business bay", "dubai marina", "downtown dubai", "downtown",
        "dubai hills", "dubai hills estate", "palm jumeirah", "jvc",
        "creek harbour", "dubai creek harbour", "arjan", "damac hills",
        "jumeirah village circle", "jlt", "bluewaters", "meydan",
    ]
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
        if msg and "@" not in msg and not re.search(r"\d", msg) and not looks_like_datetime(msg):
            if len(msg.split()) <= 4 and len(msg) <= 40:
                lead["name"] = msg

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
    
def next_missing_booking_question(session: dict, language: str):
    """Ask the next missing booking question in a stable order."""
    lead = session.get("lead", {})

    has_date = _valid_lead_value(lead.get("viewing_date"))
    has_time = _valid_lead_value(lead.get("viewing_time"))

    if not has_date and not has_time:
        return (
            "What date and time would you prefer for the viewing?"
            if language == "en"
            else "ما التاريخ والوقت المناسبان للمعاينة؟"
        )

    if has_date and not has_time:
        return (
            "Great. What time would you prefer for the viewing?"
            if language == "en"
            else "رائع. ما الوقت المناسب للمعاينة؟"
        )

    if has_time and not has_date:
        return (
            "Great. What day would you prefer for the viewing?"
            if language == "en"
            else "رائع. ما اليوم المناسب للمعاينة؟"
        )

    if not _valid_lead_value(lead.get("name")):
        return (
            "Great. What is your name?"
            if language == "en"
            else "رائع. ما اسمك الكريم؟"
        )

    if not _valid_lead_value(lead.get("email")):
        return (
            "Can we have your email to send the booking confirmation?"
            if language == "en"
            else "هل يمكننا الحصول على بريدك الإلكتروني لإرسال تأكيد الحجز؟"
        )

    if not _valid_lead_value(lead.get("phone")):
        return (
            "What phone number should the agent contact you on?"
            if language == "en"
            else "ما رقم الهاتف الذي يمكن للوكيل التواصل معك عليه؟"
        )

    return None

def fallback_missing_lead_question(session: dict, language: str) -> str:
    """Professional fallback prompt when the LLM is unavailable.

    Collects the minimum details needed for a human handoff and, if a viewing
    time is supplied too, allows the deterministic booking engine to proceed.
    """
    lead = session.get("lead", {})

    if not _valid_lead_value(lead.get("name")):
        return (
            "Our team is currently busy and will contact you shortly. Kindly share your name."
            if language == "en"
            else "فريقنا مشغول حالياً وسيتواصل معك قريباً. يرجى إرسال الاسم."
        )
    if not _valid_lead_value(lead.get("email")):
        return (
            "Thank you. Please share your email address."
            if language == "en"
            else "شكراً. يرجى إرسال البريد الإلكتروني."
        )
    if not _valid_lead_value(lead.get("phone")):
        return (
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


def post_booking_fallback_reply(user_message: str, session: dict, language: str) -> str:
    """Only used when LLM fails after a booking is already completed."""
    msg = (user_message or "").lower()
    lead = session.get("lead", {})
    interest = lead.get("interest") or "the selected property"
    vdate = lead.get("viewing_date") or "the confirmed date"
    vtime = lead.get("viewing_time") or "the confirmed time"

    if language == "ar":
        if any(w in msg for w in ["وين", "أين", "الموقع", "العنوان"]):
            return "سيشارك الوكيل الموقع الدقيق وتفاصيل الوصول قبل موعد المعاينة."
        if any(w in msg for w in ["تفاصيل", "العقار", "الملكية", "المشروع"]):
            return f"المعاينة مؤكدة للعقار المطلوب: {interest}. سيشرح لك الوكيل التفاصيل الكاملة أثناء التواصل."
        if any(w in msg for w in ["وقت", "موعد", "تاريخ"]):
            return f"موعد المعاينة مؤكد بتاريخ {vdate} الساعة {vtime}. سيتواصل معك الوكيل قريباً."
        return "تم تأكيد الحجز. يمكنك إرسال أي سؤال إضافي، وسيتواصل معك الوكيل قريباً بالتفاصيل."

    if any(w in msg for w in ["where", "location", "address", "directions"]):
        return "The exact viewing location and directions will be shared by the agent before the appointment."
    if any(w in msg for w in ["property", "details", "showing", "viewing", "project"]):
        return f"You are booked for: {interest}. The agent will share full property details, availability, and next steps before the viewing."
    if any(w in msg for w in ["time", "date", "when"]):
        return f"Your viewing is confirmed for {vdate} at {vtime}. The agent will contact you shortly."
    if any(w in msg for w in ["price", "cost", "payment", "plan", "budget"]):
        return "The agent will confirm the exact price, payment plan, and availability before your viewing."
    return "Your booking is confirmed. You can ask anything about the property, area, pricing, or next steps."


async def handle_llm_failure(session: dict, user_message: str, language: str, source: str, error: Exception) -> dict:
    """Fallback path when Groq/LLM fails.

    If booking is not completed, deterministic fallback continues collecting details
    and can still create the booking. If booking is already completed, LLM is only
    bypassed for this failed request and a safe post-booking answer is returned.
    """
    print(f"[LLM Fallback Engine]: {error}")

    # IMPORTANT: After booking, do not restart booking flow and do not ask for details again.
    # This is only a temporary fallback for the failed LLM request.
    if session.get("booking_made"):
        reply = post_booking_fallback_reply(user_message, session, language)
        session["history"].append({"role": "user", "content": user_message})
        session["history"].append({"role": "assistant", "content": reply})

        save_chat_log(session.get("id"), user_message, reply)
        
        if len(session["history"]) > 20:
            session["history"] = session["history"][-20:]
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
    if len(session["history"]) > 20:
        session["history"] = session["history"][-20:]

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
    if booking and not session["booking_made"]:
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
            "booking_status": session.get("booking_status", {}),
        }

    if session["system_prompt"] is None:
        session["system_prompt"] = await build_system_prompt(language)

    messages = [{"role": "system", "content": session["system_prompt"]}]
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
    if booking_flow_active_now and is_user_query(user_message):
        llm_user_message = (
            user_message
            + "\n\nImportant: Answer this question first using available property data. "
            + "Do not ignore it. After answering, you may continue with the next missing booking detail."
        )

    messages.append({"role": "user", "content": llm_user_message})

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=150,
        )
        raw = response.choices[0].message.content
    except Exception as e:
        error_str = str(e)
        print(f"[Groq Error]: {error_str}")
        if "429" in error_str or "rate_limit" in error_str.lower():
            session["msg_count"] -= 1
        return await handle_llm_failure(session, user_message, language, source, e)

    reply = shorten(clean_reply(raw))
    booking = extract_booking(raw) if not session["booking_made"] else None

    # ✅ HANDLE USER QUERY DURING BOOKING FLOW
    if is_user_query(user_message):
        ai_reply = reply  # AI answer first

        next_q = next_missing_booking_question(session, language)

        if next_q:
            reply = f"{ai_reply}\n\n{next_q}"
        else:
            reply = ai_reply
            
    # After booking, keep normal LLM conversation. Do not ask booking questions again.
    if session.get("booking_made"):
        session["history"].append({"role": "user", "content": user_message})
        session["history"].append({"role": "assistant", "content": reply or "Got it. How can I help you next?"})
        if len(session["history"]) > 20:
            session["history"] = session["history"][-20:]
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
            "booking_status": session.get("booking_status", {}),
        }

    if not reply:
        q = next_missing_booking_question(session, language)
        reply = q or "Got it. How can I help you next?"

    if booking:
        booking = normalize_booking_datetime(booking)
        # Merge any deterministic state that the LLM missed
        state = session.get("lead", {})
        for k in ["name", "email", "phone", "interest", "budget", "area", "viewing_date", "viewing_time"]:
            if state.get(k) and not booking.get(k):
                booking[k] = state[k]

        # Do not process a booking until required contact details exist.
        session_lead = session.setdefault("lead", {})
        for k in ["name", "email", "phone", "interest", "budget", "area", "viewing_date", "viewing_time"]:
            if booking.get(k):
                session_lead[k] = booking.get(k)

        required_ready = all(_valid_lead_value(session_lead.get(k)) for k in ["viewing_date", "viewing_time", "name", "email", "phone"])
        if required_ready:
            booking = maybe_build_booking_from_state(session, language) or booking
            await _process_booking(booking, session, language, source)
            reply = f"Confirmed. Your viewing is scheduled for {booking.get('viewing_date')} at {booking.get('viewing_time')}. Our agent will contact you shortly."
            booking_made = True
        else:
            booking_made = False
            q = next_missing_booking_question(session, language)
            reply = q or reply
    else:
        booking_made = False
        

    session["history"].append({"role": "user", "content": user_message})
    session["history"].append({"role": "assistant", "content": reply})

    save_chat_log(session_id, user_message, reply)

    if len(session["history"]) > 20:
        session["history"] = session["history"][-20:]

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
