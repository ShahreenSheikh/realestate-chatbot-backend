"""
Follow-up service — runs daily via APScheduler.
Finds leads with no response after 2 days and sends
email + WhatsApp follow-ups.
"""
import asyncio
from database import get_pending_followups, update_lead_status
from email_service import send_followup_email
from whatsapp import send_followup_whatsapp


async def run_followups():
    """Check for stale leads and send follow-ups."""
    print("[Followup] Running daily follow-up check...")
    pending = get_pending_followups(days=2)

    if not pending:
        print("[Followup] No pending follow-ups.")
        return

    print(f"[Followup] Found {len(pending)} leads to follow up.")

    for lead in pending:
        if not lead.email and not lead.phone:
            continue

        name     = lead.name     or "Valued Client"
        interest = lead.interest or "Dubai Properties"

        # Send email if available
        if lead.email:
            send_followup_email(
                client_name=name,
                client_email=lead.email,
                interest=interest,
            )

        # Send WhatsApp if phone available
        if lead.phone:
            await send_followup_whatsapp(
                client_phone=lead.phone,
                client_name=name,
                interest=interest,
            )

        # Update status so we don't follow up again
        update_lead_status(lead.session_id, "followed_up")
        print(f"[Followup] Sent to {name} ({lead.email or lead.phone})")

    print(f"[Followup] Done. {len(pending)} follow-ups sent.")