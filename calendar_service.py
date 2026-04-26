import os
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from credentials import get_credentials

SCOPES        = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID   = os.getenv("GOOGLE_CALENDAR_ID")
SA_FILE       = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
AGENT_NAME    = os.getenv("AGENT_NAME", "Omar Hassan")
AGENCY_NAME   = os.getenv("AGENCY_NAME", "Elite Properties Dubai")


def _get_service():
    creds = get_credentials(SCOPES)
    return build("calendar", "v3", credentials=creds)


def create_viewing_event(
    client_name: str,
    client_email: str,
    property_interest: str,
    date_str: str,        # e.g. "2026-05-10"
    time_str: str,        # e.g. "14:00"
    notes: str = "",
) -> dict:
    """
    Create a Google Calendar event for a property viewing.
    Returns the created event dict (includes id and htmlLink).
    """
    try:
        # Parse date and time
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_dt   = start_dt + timedelta(hours=1)

        event = {
            "summary":     f"Property Viewing — {client_name}",
            "description": (
                f"Client: {client_name}\n"
                f"Interest: {property_interest}\n"
                f"Agent: {AGENT_NAME}\n"
                f"{notes}"
            ),
            "start": {
                "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "Asia/Dubai",
            },
            "end": {
                "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "Asia/Dubai",
            },
            "attendees": [
                {"email": client_email,            "displayName": client_name},
                {"email": CALENDAR_ID,             "displayName": AGENT_NAME},
            ],
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email",  "minutes": 1440},   # 24h before
                    {"method": "popup",  "minutes": 60},     # 1h before
                ],
            },
            "sendUpdates": "all",   # sends email invite to attendees
        }

        service       = _get_service()
        created_event = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event,
            sendUpdates="all",
        ).execute()

        print(f"[Calendar] Event created: {created_event.get('htmlLink')}")
        return created_event

    except Exception as e:
        print(f"[Calendar] Error creating event: {e}")
        return {}
