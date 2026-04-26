import os
import httpx

WHATSAPP_TOKEN        = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID     = os.getenv("WHATSAPP_PHONE_ID")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
AGENT_WHATSAPP        = os.getenv("AGENT_WHATSAPP", "971501234567")
AGENT_NAME            = os.getenv("AGENT_NAME", "Omar Hassan")
AGENCY_NAME           = os.getenv("AGENCY_NAME", "Elite Properties Dubai")
BASE_URL = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"


async def _send(to: str, body: str):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type":  "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to":   to,
        "type": "text",
        "text": {"body": body},
    }
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(BASE_URL, json=payload, headers=headers, timeout=8.0)
            res.raise_for_status()
            return True
        except Exception as e:
            print(f"[WhatsApp] Error sending to {to}: {e}")
            return False


async def send_booking_whatsapp(
    client_phone: str,
    client_name: str,
    property_interest: str,
    viewing_date: str,
    viewing_time: str,
):
    msg = (
        f"Dear {client_name},\n\n"
        f"Your property viewing has been confirmed with {AGENCY_NAME}.\n\n"
        f"📋 *Details:*\n"
        f"• Property: {property_interest}\n"
        f"• Date: {viewing_date}\n"
        f"• Time: {viewing_time} (Dubai)\n"
        f"• Agent: {AGENT_NAME}\n\n"
        f"Our agent will be in touch to confirm the exact location.\n\n"
        f"_{AGENCY_NAME}_"
    )
    return await _send(client_phone, msg)


async def send_followup_whatsapp(
    client_phone: str,
    client_name: str,
    interest: str,
):
    msg = (
        f"Dear {client_name},\n\n"
        f"We are following up on your recent property enquiry regarding *{interest}*.\n\n"
        f"We would be pleased to assist you further. "
        f"Please let us know if you would like to schedule a viewing or require additional information.\n\n"
        f"Kindly reply to this message or contact us directly.\n\n"
        f"_{AGENT_NAME}_ · _{AGENCY_NAME}_"
    )
    return await _send(client_phone, msg)


async def notify_agent_whatsapp(
    client_name: str,
    client_phone: str,
    interest: str,
    viewing_date: str,
    viewing_time: str,
):
    msg = (
        f"🏠 *New Lead — {AGENCY_NAME}*\n\n"
        f"*Client:* {client_name}\n"
        f"*Phone:* {client_phone}\n"
        f"*Interest:* {interest}\n"
        f"*Viewing:* {viewing_date} at {viewing_time}\n\n"
        f"Calendar event created. Please confirm the location with the client."
    )
    return await _send(AGENT_WHATSAPP, msg)


def verify_webhook(mode: str, token: str, challenge: str):
    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return challenge
    return None


def parse_incoming(body: dict):
    try:
        msg = body["entry"][0]["changes"][0]["value"]["messages"][0]
        if msg["type"] != "text":
            return None, None
        return msg["from"], msg["text"]["body"]
    except (KeyError, IndexError):
        return None, None