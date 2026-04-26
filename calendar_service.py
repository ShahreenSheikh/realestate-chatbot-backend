import os
from datetime import timedelta

import dateparser
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
SA_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
AGENT_NAME = os.getenv("AGENT_NAME", "Omar Hassan")
AGENCY_NAME = os.getenv("AGENCY_NAME", "Elite Properties Dubai")


def _get_service():
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds)


def create_viewing_event(
    client_name: str,
    client_email: str,
    property_interest: str,
    date_str: str,
    time_str: str,
    notes: str = "",
) -> dict:
    try:
        parsed = dateparser.parse(
            f"{date_str} {time_str}",
            settings={
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )

        if not parsed:
            raise ValueError(f"Invalid date/time: {date_str} {time_str}")

        start_dt = parsed
        end_dt = start_dt + timedelta(hours=1)

        event = {
            "summary": f"Property Viewing — {client_name}",
            "description": (
                f"Client: {client_name}\n"
                f"Email: {client_email}\n"
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
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 60},
                ],
            },
        }

        service = _get_service()
        created_event = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event,
        ).execute()

        print(f"[Calendar] Event created: {created_event.get('htmlLink')}")
        return created_event

    except Exception as e:
        print(f"[Calendar] Error creating event: {e}")
        return {}