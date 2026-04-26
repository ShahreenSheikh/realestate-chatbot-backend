from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    language: str
    session_id: str
    lead_captured: bool = False
    booking_made: bool = False


class Lead(BaseModel):
    session_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    interest: Optional[str] = None
    budget: Optional[str] = None
    area: Optional[str] = None
    property_type: Optional[str] = None
    viewing_date: Optional[str] = None
    viewing_time: Optional[str] = None
    language: str = "en"
    source: str = "website"
    status: str = "new"           # new | booking_sent | followed_up | converted
    created_at: Optional[str] = None


class Booking(BaseModel):
    lead_session_id: str
    client_name: str
    client_email: str
    client_phone: str
    property_interest: str
    preferred_date: str
    preferred_time: str
    agent_name: str
    agent_email: str
    calendar_event_id: Optional[str] = None