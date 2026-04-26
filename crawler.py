"""
Zahra Signature Realty — Web Crawler
Crawls zahrasignaturerealty.com/properties/ monthly via APScheduler.

Smart upsert logic:
- New property   → adds new row
- Existing URL   → updates changed fields only
- Removed from site → marks as inactive (never deletes)
- No duplicates  → URL is the unique key

Schedule: 1st of every month at 8:00 AM Dubai time.
Manual trigger: POST /crawler/run
"""

import os
import re
import asyncio
import httpx
from bs4 import BeautifulSoup
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

BASE_URL       = "https://zahrasignaturerealty.com"
PROPERTIES_URL = f"{BASE_URL}/properties/"
SHEET_ID       = os.getenv("GOOGLE_SHEET_ID")
SA_FILE        = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]
CRAWLED_TAB    = "crawled_properties"

# Column order — must match HEADERS below
HEADERS = [
    "project_name", "developer", "location", "property_type",
    "description", "features", "amenities", "url",
    "first_seen", "last_updated", "active"
]
URL_COL     = HEADERS.index("url")           # column H (index 7)
ACTIVE_COL  = HEADERS.index("active")        # column K (index 10)
UPDATED_COL = HEADERS.index("last_updated")  # column J (index 9)

HEADERS_DISPLAY = {
    "project_name":  "project_name",
    "developer":     "developer",
    "location":      "location",
    "property_type": "property_type",
    "description":   "description",
    "features":      "features",
    "amenities":     "amenities",
    "url":           "url",
    "first_seen":    "first_seen",
    "last_updated":  "last_updated",
    "active":        "active",
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Google Sheets helpers ──────────────────────────────────────────────────────

def _get_sheets():
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds).spreadsheets()


def _ensure_tab(sheets):
    """Create the crawled_properties tab + header row if it doesn't exist."""
    meta     = sheets.get(spreadsheetId=SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]

    if CRAWLED_TAB not in existing:
        sheets.batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": CRAWLED_TAB}}}]},
        ).execute()
        # Write header row
        sheets.values().update(
            spreadsheetId=SHEET_ID,
            range=f"{CRAWLED_TAB}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [list(HEADERS_DISPLAY.values())]},
        ).execute()
        print(f"[Sheets] Created tab '{CRAWLED_TAB}' with headers.")


def _read_existing(sheets) -> dict:
    """
    Read all existing rows from the sheet.
    Returns a dict: {url: {"row_index": int, "data": list}}
    Row index is 1-based (row 1 = headers, row 2 = first data row).
    """
    result = sheets.values().get(
        spreadsheetId=SHEET_ID,
        range=f"{CRAWLED_TAB}!A1:K5000",
    ).execute()

    rows    = result.get("values", [])
    existing = {}

    for i, row in enumerate(rows[1:], start=2):   # skip header, 1-based index
        # Pad row to full width
        while len(row) < len(HEADERS):
            row.append("")
        url = row[URL_COL].strip()
        if url:
            existing[url] = {"row_index": i, "data": row}

    print(f"[Sheets] Found {len(existing)} existing rows in sheet.")
    return existing


def _prop_to_row(prop: dict, first_seen: str = "") -> list:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    return [
        prop.get("project_name", ""),
        prop.get("developer", ""),
        prop.get("location", ""),
        prop.get("property_type", ""),
        prop.get("description", "")[:500],
        prop.get("features", ""),
        prop.get("amenities", ""),
        prop.get("url", ""),
        first_seen or now,   # first_seen — preserve original date
        now,                 # last_updated — always current
        "yes",               # active
    ]


def _col_letter(index: int) -> str:
    """Convert 0-based column index to letter (0=A, 1=B, etc.)"""
    return chr(ord("A") + index)


def upsert_to_sheet(properties: list) -> dict:
    """
    Smart upsert:
    - New URL   → append new row
    - Known URL → update changed columns only
    - Missing   → mark active = 'no' (soft delete)
    Returns stats dict.
    """
    if not properties:
        return {"added": 0, "updated": 0, "deactivated": 0, "unchanged": 0}

    try:
        sheets  = _get_sheets()
        _ensure_tab(sheets)
        existing = _read_existing(sheets)

        crawled_urls = {p["url"] for p in properties}
        stats = {"added": 0, "updated": 0, "deactivated": 0, "unchanged": 0}
        batch_updates = []   # for updating existing rows
        new_rows      = []   # for appending new rows

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

        # ── Process each crawled property ─────────────────────────────────────
        for prop in properties:
            url = prop["url"]

            if url in existing:
                old_row = existing[url]["data"]
                new_row = _prop_to_row(prop, first_seen=old_row[HEADERS.index("first_seen")])
                row_idx = existing[url]["row_index"]

                # Check if anything meaningful changed
                changed_cols = [
                    i for i in range(len(HEADERS))
                    if i not in (HEADERS.index("first_seen"), HEADERS.index("last_updated"), ACTIVE_COL)
                    and (i >= len(old_row) or old_row[i] != new_row[i])
                ]

                if changed_cols:
                    # Update the whole row
                    batch_updates.append({
                        "range": f"{CRAWLED_TAB}!A{row_idx}:{_col_letter(len(HEADERS)-1)}{row_idx}",
                        "values": [new_row],
                    })
                    stats["updated"] += 1
                    print(f"[Sheets] Updated: {prop['project_name']}")
                else:
                    # Only update last_updated timestamp
                    batch_updates.append({
                        "range": f"{CRAWLED_TAB}!{_col_letter(UPDATED_COL)}{row_idx}",
                        "values": [[now]],
                    })
                    stats["unchanged"] += 1

            else:
                # New property — append
                new_rows.append(_prop_to_row(prop))
                stats["added"] += 1
                print(f"[Sheets] New: {prop['project_name']}")

        # ── Mark removed properties as inactive ───────────────────────────────
        for url, info in existing.items():
            if url not in crawled_urls:
                row_idx = info["row_index"]
                if info["data"][ACTIVE_COL] != "no":
                    batch_updates.append({
                        "range": f"{CRAWLED_TAB}!{_col_letter(ACTIVE_COL)}{row_idx}",
                        "values": [["no"]],
                    })
                    stats["deactivated"] += 1
                    print(f"[Sheets] Deactivated: {info['data'][0]}")

        # ── Send batch updates ────────────────────────────────────────────────
        if batch_updates:
            sheets.values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": batch_updates,
                },
            ).execute()

        # ── Append new rows ───────────────────────────────────────────────────
        if new_rows:
            sheets.values().append(
                spreadsheetId=SHEET_ID,
                range=f"{CRAWLED_TAB}!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": new_rows},
            ).execute()

        print(f"\n[Sheets] Upsert complete:")
        print(f"  Added:       {stats['added']}")
        print(f"  Updated:     {stats['updated']}")
        print(f"  Unchanged:   {stats['unchanged']}")
        print(f"  Deactivated: {stats['deactivated']}")
        return stats

    except Exception as e:
        print(f"[Sheets] Upsert error: {e}")
        return {"error": str(e)}


# ── Page fetcher ──────────────────────────────────────────────────────────────

async def fetch_page(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(
            headers=HTTP_HEADERS, timeout=15.0, follow_redirects=True
        ) as client:
            res = await client.get(url)
            res.raise_for_status()
            return res.text
    except Exception as e:
        print(f"[Crawler] Failed: {url} — {e}")
        return None


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_property_links(html: str) -> list:
    soup  = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/property/" in href and href not in links:
            links.append(href)
    print(f"[Crawler] Found {len(links)} property pages.")
    return links


def parse_property_detail(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    # Project name
    h1 = soup.find("h1")
    project_name = h1.get_text(strip=True) if h1 else ""
    project_name = re.sub(
        r'\s*[-–]\s*(Zahra Signature|ZSR).*$', '', project_name, flags=re.IGNORECASE
    ).strip()

    # Developer — detect from img alt or page text
    developer = ""
    for img in soup.find_all("img", alt=True):
        alt = img["alt"].strip().lower()
        if alt in ("damac","emaar","nakheel","sobha","binghatti","meraas","ellington","danube","aldar"):
            developer = alt.title()
            break
    if not developer:
        page_text = soup.get_text().lower()
        for dev in ["emaar","damac","nakheel","sobha","binghatti","meraas","ellington","danube","aldar"]:
            if dev in page_text:
                developer = dev.title()
                break

    # Location
    location = ""
    page_text_full = soup.get_text()
    for area in [
        "Dubai Creek Harbour","Dubai Marina","Downtown Dubai","Business Bay",
        "Dubai Hills","Palm Jumeirah","Dubai South","DAMAC Hills","MBR City",
        "Dubai Land","Dubailand","JVC","Palm Jebel Ali","Dubai Islands",
    ]:
        if area in page_text_full:
            location = area
            break
    if not location:
        for h2 in soup.find_all("h2"):
            t = h2.get_text(strip=True)
            if "Dubai" in t or "Sharjah" in t:
                location = t
                break

    # Property type
    property_type = "Residential"
    for kw in ["Penthouse","Villa","Townhouse","Apartment","Studio"]:
        if kw in page_text_full:
            property_type = kw
            break

    # Description — longest paragraph
    paras = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 80]
    description = max(paras, key=len) if paras else ""

    # Features — h3 tags near "Features" section
    features = []
    feat = soup.find(string=re.compile(r"Features?", re.I))
    if feat:
        parent = feat.find_parent()
        if parent:
            for h3 in parent.find_all_next("h3", limit=10):
                t = h3.get_text(strip=True)
                if t and len(t) < 80:
                    features.append(t)
                if len(features) >= 6:
                    break

    # Amenities — img alt tags near "Amenities" section
    amenities = []
    amen = soup.find(string=re.compile(r"Amenities?", re.I))
    if amen:
        parent = amen.find_parent()
        if parent:
            for img in parent.find_all_next("img", limit=15):
                alt = img.get("alt","").strip()
                if alt and len(alt) < 60 and alt not in amenities:
                    amenities.append(alt)
                if len(amenities) >= 8:
                    break

    return {
        "project_name":  project_name or url.split("/property/")[-1].replace("-"," ").title().rstrip("/"),
        "developer":     developer or "Unknown",
        "location":      location  or "Dubai",
        "property_type": property_type,
        "description":   description,
        "features":      " | ".join(features),
        "amenities":     " | ".join(amenities),
        "url":           url,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_crawler() -> dict:
    print(f"\n{'='*50}")
    print(f"[Crawler] Started: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"[Crawler] Target: {PROPERTIES_URL}")
    print(f"{'='*50}")

    listing_html = await fetch_page(PROPERTIES_URL)
    if not listing_html:
        return {"status": "error", "message": "Could not fetch listings page", "count": 0}

    property_urls = parse_property_links(listing_html)
    if not property_urls:
        return {"status": "error", "message": "No property links found", "count": 0}

    properties = []
    for i, url in enumerate(property_urls):
        print(f"[Crawler] {i+1}/{len(property_urls)} — {url}")
        html = await fetch_page(url)
        if html:
            prop = parse_property_detail(html, url)
            properties.append(prop)
            print(f"           ✓ {prop['project_name']} ({prop['developer']}, {prop['location']})")
        await asyncio.sleep(1.5)   # polite crawl delay

    stats  = upsert_to_sheet(properties)
    result = {
        "status":     "success",
        "count":      len(properties),
        "crawled_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "stats":      stats,
        "properties": [p["project_name"] for p in properties],
    }
    print(f"\n[Crawler] Finished: {len(properties)} properties processed.")
    return result
