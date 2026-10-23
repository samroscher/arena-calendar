import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from curl_cffi import requests
from icalendar import Calendar, Event

BASE = "https://allianz-arena.com/de/events"
BERLIN = ZoneInfo("Europe/Berlin")
MONTHS_AHEAD = 3
OUTPUT_FILE = Path(__file__).resolve().parent / "arena.ics"

warnings = []


def warn(message):
    print("WARNUNG:", message)
    warnings.append(message)


# ---------- Abruf ----------

def season_of(year, month):
    if month >= 7:
        return f"{year}-{year + 1}"
    return f"{year - 1}-{year}"


def next_months(n):
    today = date.today()
    y, m = today.year, today.month
    months = []
    for _ in range(n):
        months.append((y, m))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return months


def class_starts_with(prefix):
    def check(css_class):
        return css_class is not None and css_class.startswith(prefix)
    return check


def scrape_month(year, month):
    season = season_of(year, month)
    url = f"{BASE}/{season}/{year}-{month}"
    resp = requests.get(url, impersonate="chrome", timeout=15)
    print("Abruf:", url, "->", resp.status_code)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    events = []
    for tag in soup.find_all("script", type="application/ld+json"):
        data = json.loads(tag.string)
        card = tag.parent

        comp_tag = card.find("p", class_=class_starts_with("CompactCompetition"))
        short_tags = card.find_all("div", class_=class_starts_with("TeamNameShort"))
        short_names = [t.get_text(strip=True) for t in short_tags]

        home_full = data.get("homeTeam", {}).get("name")
        away_full = data.get("awayTeam", {}).get("name")

        events.append({
            "season": season,
            "type": data.get("@type"),
            "name": data.get("name"),
            "start": data.get("startDate"),
            "end": data.get("endDate"),
            "home_full": home_full,
            "away_full": away_full,
            "home_short": short_names[0] if len(short_names) == 2 else home_full,
            "away_short": short_names[1] if len(short_names) == 2 else away_full,
            "competition": comp_tag.get_text(strip=True) if comp_tag else "",
            "pending": "Terminierung ausstehend" in card.get_text(),
        })
    return events


# ---------- Umwandlung in Kalendertermine ----------

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def is_midnight_berlin(dt):
    local = dt.astimezone(BERLIN)
    return local.hour == 0 and local.minute == 0


def match_duration(competition):
    comp = competition.lower()
    if "football" in comp:
        return timedelta(hours=3, minutes=30)
    if "dfb-pokal" in comp or "finale" in comp:
        return timedelta(hours=2, minutes=45)
    return timedelta(hours=2)


def build_event(e):
    start = datetime.fromisoformat(e["start"])

    ev = Event()
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("location", "Allianz Arena, München")
    ev.add("transp", "TRANSPARENT")

    if e["type"] == "SportsEvent":
        if not e["competition"]:
            warn(f"Kein Wettbewerb gefunden: {e['name']}")

        uid = slug(f"{e['season']} {e['home_full']} {e['away_full']} {e['competition']}")
        title = f"{e['home_short']} – {e['away_short']}"
        if e["competition"]:
            title += f" · {e['competition']}"

        if is_midnight_berlin(start):
            title = "[Anstoß offen] " + title
            day = start.astimezone(BERLIN).date()
            ev.add("dtstart", day)
            ev.add("dtend", day + timedelta(days=1))
        else:
            if e["pending"]:
                title = "[Zeit vorläufig] " + title
            ev.add("dtstart", start)
            ev.add("dtend", start + match_duration(e["competition"]))
    else:
        title = e["name"].split(" am ")[0]
        first_day = start.astimezone(BERLIN).date()
        if e["end"]:
            last_day = datetime.fromisoformat(e["end"]).astimezone(BERLIN).date()
        else:
            last_day = first_day
        uid = slug(f"{e['season']} {title} {first_day}")
        ev.add("dtstart", first_day)
        ev.add("dtend", last_day + timedelta(days=1))

    ev.add("uid", uid + "@arena-scrape")
    ev.add("summary", title)
    return uid, ev


# ---------- Hauptablauf ----------

def main():
    raw = []
    for year, month in next_months(MONTHS_AHEAD):
        raw.extend(scrape_month(year, month))
        time.sleep(3)

    events = {}
    for e in raw:
        if "Museum" in (e["name"] or ""):
            continue
        try:
            uid, ev = build_event(e)
        except Exception as err:
            warn(f"Übersprungen: {e['name']} -> {err}")
            continue
        events[uid] = ev

    if not events:
        raise SystemExit("FEHLER: Keine Termine gefunden – Datei wird nicht überschrieben.")

    cal = Calendar()
    cal.add("prodid", "-//arena-scrape//DE")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Allianz Arena Events")
    cal.add("refresh-interval", timedelta(hours=12), parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", "PT12H")
    for ev in events.values():
        cal.add_component(ev)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(cal.to_ical())

    print(f"\n{len(events)} Termine geschrieben nach {OUTPUT_FILE}:")
    for ev in events.values():
        print(" ", ev["dtstart"].dt, "|", ev["summary"])

    if warnings:
        print(f"\nFertig mit {len(warnings)} Warnung(en).")
        sys.exit(1)
    print("\nFertig ohne Warnungen.")


main()