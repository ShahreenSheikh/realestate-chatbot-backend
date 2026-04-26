import os
import resend
DEMO_MODE = os.getenv("DEMO_MODE", "true") == "true"

resend.api_key = os.getenv("RESEND_API_KEY", "")

FROM_EMAIL   = os.getenv("FROM_EMAIL",   "agent@elitepropertiesdubai.com")
AGENT_NAME   = os.getenv("AGENT_NAME",   "Omar Hassan")
AGENCY_NAME  = os.getenv("AGENCY_NAME",  "Elite Properties Dubai")
AGENT_EMAIL  = os.getenv("AGENT_EMAIL",  "omar.hassan@elitepropertiesdubai.com")


# ── Booking confirmation to client ────────────────────────────────────────────

def send_booking_confirmation(
    client_name: str,
    client_email: str,
    property_interest: str,
    viewing_date: str,
    viewing_time: str,
):
    subject = f"Your Property Viewing is Confirmed — {AGENCY_NAME}"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a2e">
      <div style="background:#1a1a2e;padding:28px 32px;border-radius:12px 12px 0 0">
        <h1 style="color:#e8c468;margin:0;font-size:22px">{AGENCY_NAME}</h1>
        <p style="color:#a8c7a0;margin:6px 0 0;font-size:13px">Dubai Real Estate Specialists</p>
      </div>
      <div style="background:#f9f8fc;padding:32px;border-radius:0 0 12px 12px;border:1px solid #e8e6f0">
        <p style="font-size:16px;font-weight:500">Dear {client_name},</p>
        <p style="color:#444;line-height:1.7">
          We are pleased to confirm your property viewing appointment with <strong>{AGENCY_NAME}</strong>.
          Our specialist agent will be in touch to confirm the exact location and any additional details.
        </p>
        <div style="background:#fff;border:1px solid #e8e6f0;border-radius:10px;padding:20px 24px;margin:24px 0">
          <h3 style="margin:0 0 14px;color:#1a1a2e;font-size:15px">Appointment Details</h3>
          <table style="width:100%;font-size:14px;color:#444">
            <tr><td style="padding:6px 0;color:#888;width:140px">Property Interest</td><td><strong>{property_interest}</strong></td></tr>
            <tr><td style="padding:6px 0;color:#888">Date</td><td><strong>{viewing_date}</strong></td></tr>
            <tr><td style="padding:6px 0;color:#888">Time</td><td><strong>{viewing_time} (GST — Dubai)</strong></td></tr>
            <tr><td style="padding:6px 0;color:#888">Your Agent</td><td><strong>{AGENT_NAME}</strong></td></tr>
            <tr><td style="padding:6px 0;color:#888">Agent Contact</td><td><strong>{AGENT_EMAIL}</strong></td></tr>
          </table>
        </div>
        <p style="color:#444;line-height:1.7;font-size:14px">
          Please ensure you have a valid passport or Emirates ID available on the day of the viewing.
          If you need to reschedule, please reply to this email or contact your agent directly.
        </p>
        <div style="border-top:1px solid #e8e6f0;margin-top:28px;padding-top:20px;font-size:12px;color:#888">
          <p style="margin:0">{AGENCY_NAME} · Dubai, UAE</p>
          <p style="margin:4px 0 0">This is an automated confirmation. Please do not reply directly to this email.</p>
        </div>
      </div>
    </div>
    """
    try:
        resend.Emails.send({
            "from":    f"{AGENCY_NAME} <{FROM_EMAIL}>",
            "to":      [client_email],
            "subject": subject,
            "html":    html,
        })
        print(f"[Email] Booking confirmation sent to {client_email}")
    except Exception as e:
        print(f"[Email] Error sending confirmation: {e}")


# ── New lead notification to agent ────────────────────────────────────────────

def send_agent_notification(
    client_name: str,
    client_email: str,
    client_phone: str,
    interest: str,
    budget: str,
    area: str,
    viewing_date: str,
    viewing_time: str,
    language: str,
):
    subject = f"New Lead: {client_name} — Viewing Request"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a2e">
      <div style="background:#1a1a2e;padding:24px 32px;border-radius:12px 12px 0 0">
        <h2 style="color:#e8c468;margin:0;font-size:18px">New Lead Alert</h2>
        <p style="color:#a8c7a0;margin:4px 0 0;font-size:13px">{AGENCY_NAME}</p>
      </div>
      <div style="background:#f9f8fc;padding:28px 32px;border-radius:0 0 12px 12px;border:1px solid #e8e6f0">
        <div style="background:#fff;border:1px solid #e8e6f0;border-radius:10px;padding:20px 24px">
          <h3 style="margin:0 0 14px;font-size:15px">Client Details</h3>
          <table style="width:100%;font-size:14px;color:#444">
            <tr><td style="padding:5px 0;color:#888;width:140px">Name</td><td><strong>{client_name}</strong></td></tr>
            <tr><td style="padding:5px 0;color:#888">Email</td><td><strong>{client_email}</strong></td></tr>
            <tr><td style="padding:5px 0;color:#888">Phone</td><td><strong>{client_phone}</strong></td></tr>
            <tr><td style="padding:5px 0;color:#888">Interest</td><td><strong>{interest}</strong></td></tr>
            <tr><td style="padding:5px 0;color:#888">Budget</td><td><strong>{budget or "Not specified"}</strong></td></tr>
            <tr><td style="padding:5px 0;color:#888">Preferred Area</td><td><strong>{area or "Not specified"}</strong></td></tr>
            <tr><td style="padding:5px 0;color:#888">Viewing Date</td><td><strong>{viewing_date}</strong></td></tr>
            <tr><td style="padding:5px 0;color:#888">Viewing Time</td><td><strong>{viewing_time}</strong></td></tr>
            <tr><td style="padding:5px 0;color:#888">Language</td><td><strong>{"Arabic" if language=="ar" else "English"}</strong></td></tr>
          </table>
        </div>
        <p style="font-size:13px;color:#888;margin-top:20px">
          A calendar event has been created in your Google Calendar. Please confirm the viewing location with the client.
        </p>
      </div>
    </div>
    """
    try:
        resend.Emails.send({
            "from":    f"{AGENCY_NAME} <{FROM_EMAIL}>",
            "to":      [AGENT_EMAIL],
            "subject": subject,
            "html":    html,
        })
        print(f"[Email] Agent notification sent to {AGENT_EMAIL}")
    except Exception as e:
        print(f"[Email] Error sending agent notification: {e}")


# ── Follow-up email to unresponsive lead ──────────────────────────────────────

def send_followup_email(
    client_name: str,
    client_email: str,
    interest: str,
):
    subject = f"Following Up on Your Property Enquiry — {AGENCY_NAME}"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a2e">
      <div style="background:#1a1a2e;padding:28px 32px;border-radius:12px 12px 0 0">
        <h1 style="color:#e8c468;margin:0;font-size:22px">{AGENCY_NAME}</h1>
        <p style="color:#a8c7a0;margin:6px 0 0;font-size:13px">Dubai Real Estate Specialists</p>
      </div>
      <div style="background:#f9f8fc;padding:32px;border-radius:0 0 12px 12px;border:1px solid #e8e6f0">
        <p style="font-size:16px;font-weight:500">Dear {client_name},</p>
        <p style="color:#444;line-height:1.7">
          We are following up on your recent property enquiry regarding <strong>{interest}</strong>.
          We would be delighted to assist you further and answer any questions you may have.
        </p>
        <p style="color:#444;line-height:1.7">
          Please let us know if you would like to proceed with scheduling a viewing or require
          any additional information regarding available properties, payment plans, or investment opportunities in Dubai.
        </p>
        <div style="text-align:center;margin:28px 0">
          <a href="mailto:{AGENT_EMAIL}"
             style="background:#1a1a2e;color:#e8c468;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:500;font-size:14px">
            Reply to This Email
          </a>
        </div>
        <div style="border-top:1px solid #e8e6f0;margin-top:20px;padding-top:20px;font-size:12px;color:#888">
          <p style="margin:0">{AGENT_NAME} · {AGENCY_NAME}</p>
          <p style="margin:4px 0 0">{AGENT_EMAIL}</p>
        </div>
      </div>
    </div>
    """
    try:
        resend.Emails.send({
            "from":    f"{AGENT_NAME}, {AGENCY_NAME} <{FROM_EMAIL}>",
            "to":      [client_email],
            "subject": subject,
            "html":    html,
        })
        print(f"[Email] Follow-up sent to {client_email}")
        return True
    except Exception as e:
        print(f"[Email] Error sending follow-up: {e}")
        return False