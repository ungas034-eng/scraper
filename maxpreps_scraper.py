#!/usr/bin/env python3
"""
MaxPreps scraper — core library + CLI

UI (Streamlit):
  pip install streamlit
  streamlit run app.py

CLI:
  python3 maxpreps_scraper.py --state tx --sport football --date 9/12/2026

Modul ini berisi logika scrape (stdlib). UI ada di app.py.
"""

from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

WATCH_LIVE_DEFAULT = "https://example.com/live"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "schedules")
OUTPUT_FILE = os.path.join(BASE_DIR, "schedules.txt")
MASCOT_CACHE = os.path.join(BASE_DIR, "mascot_cache.json")
WATCH_FILE = os.path.join(BASE_DIR, "watch_links.txt")

# File sementara di dalam project (writable di PythonAnywhere home)
TEMP_DIR = os.path.join(BASE_DIR, "tmp_out")
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
_LATEST_OUTPUT: Optional[str] = None

# Deteksi PythonAnywhere → kurangi worker (resource limit)
ON_PYTHONANYWHERE = bool(
    os.environ.get("PYTHONANYWHERE_SITE")
    or os.environ.get("PYTHONANYWHERE_DOMAIN")
    or "pythonanywhere" in (os.environ.get("HOSTNAME") or "").lower()
)
DEFAULT_STATE_WORKERS = 3 if ON_PYTHONANYWHERE else 8
DEFAULT_GAME_WORKERS = 2 if ON_PYTHONANYWHERE else 5

STATES = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas",
    "ca": "California", "co": "Colorado", "ct": "Connecticut", "de": "Delaware",
    "dc": "Washington DC", "fl": "Florida", "ga": "Georgia", "hi": "Hawaii",
    "id": "Idaho", "il": "Illinois", "in": "Indiana", "ia": "Iowa",
    "ks": "Kansas", "ky": "Kentucky", "la": "Louisiana", "me": "Maine",
    "md": "Maryland", "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota",
    "ms": "Mississippi", "mo": "Missouri", "mt": "Montana", "ne": "Nebraska",
    "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey", "nm": "New Mexico",
    "ny": "New York", "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio",
    "ok": "Oklahoma", "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island",
    "sc": "South Carolina", "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas",
    "ut": "Utah", "vt": "Vermont", "va": "Virginia", "wa": "Washington",
    "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming", "ps": "Prep Schools",
}

SPORTS = {
    "football": ("Football (Boys)", "football"),
    "basketball": ("Basketball (Boys)", "basketball"),
    "basketball-girls": ("Basketball (Girls)", "basketball/girls"),
    "baseball": ("Baseball", "baseball"),
    "softball": ("Softball", "softball"),
    "volleyball": ("Volleyball (Girls)", "volleyball"),
    "volleyball-boys": ("Volleyball (Boys)", "volleyball/boys"),
    "soccer": ("Soccer (Boys)", "soccer"),
    "soccer-girls": ("Soccer (Girls)", "soccer/girls"),
}

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.137 Safari/537.36",
]


def load_watch_map(path: Optional[str] = None) -> Dict[str, str]:
    path = path or WATCH_FILE
    mapping: Dict[str, str] = {}
    if not os.path.isfile(path):
        return mapping
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            mapping[key.strip().lower()] = val.strip()
    return mapping


def save_watch_link(url: str, state: str = "", sport: str = "") -> None:
    """Persist the watch-live URL so next run remembers it."""
    mapping = load_watch_map()
    if url:
        mapping["default"] = url
        if state:
            mapping[state.lower()] = url
        if state and sport:
            mapping[f"{state.lower()}/{sport}"] = url
    lines = ["# watch live links", "# key=url", ""]
    for k in sorted(mapping):
        if mapping[k]:
            lines.append(f"{k}={mapping[k]}")
    with open(WATCH_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def resolve_watch_url(watch_cli: str, watch_map: Dict[str, str], state: str, sport: str) -> str:
    for k in (f"{state}/{sport}", f"{state}-{sport}", state, sport, "default"):
        if watch_map.get(k):
            return watch_map[k]
    return watch_cli or WATCH_LIVE_DEFAULT


def fetch(url: str, referer: str = "https://www.maxpreps.com/", insecure: bool = False) -> Tuple[int, str]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40, context=ctx) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body
    except Exception as exc:
        return 0, str(exc)


def to_mdy(value: str) -> str:
    value = (value or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value)
    if m:
        return f"{int(m.group(2))}/{int(m.group(3))}/{m.group(1)}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)
    if m:
        return f"{int(m.group(1))}/{int(m.group(2))}/{m.group(3)}"
    now = datetime.now()
    return f"{now.month}/{now.day}/{now.year}"


def mdy_to_iso(mdy: str) -> str:
    parts = mdy.split("/")
    if len(parts) != 3:
        return datetime.now().strftime("%Y-%m-%d")
    return f"{int(parts[2]):04d}-{int(parts[0]):02d}-{int(parts[1]):02d}"


def format_tanggal(mdy: str) -> str:
    parts = mdy.split("/")
    try:
        dt = datetime(int(parts[2]), int(parts[0]), int(parts[1]))
    except Exception:
        return mdy
    return dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")


def scores_url(state: str, sport_key: str, mdy: Optional[str]) -> str:
    slug = SPORTS.get(sport_key, SPORTS["football"])[1]
    url = f"https://www.maxpreps.com/{state}/{slug}/scores/"
    if mdy:
        url += f"?date={mdy}"
    return url


def hashtag(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "", text or "")
    return "#" + text if text else ""


def strip_rank(name: str) -> str:
    return re.sub(r"^\(#\d+\)\s*", "", name).strip()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class ScoreParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: List[dict] = []
        self._in_card = False
        self._card_depth = 0
        self._cur: Optional[dict] = None
        self._buf: List[str] = []
        self._capture = None
        self._pending_team = {"name": "", "score": ""}

    def handle_starttag(self, tag, attrs):
        amap = dict(attrs)
        cls = amap.get("class", "")
        if tag == "div" and "contest-box-item" in cls:
            self._in_card = True
            self._card_depth = 1
            self._cur = {
                "state_attr": amap.get("data-contest-state", ""),
                "href": "",
                "teams": [],
                "details": "",
            }
            self._pending_team = {"name": "", "score": ""}
            return
        if not self._in_card or self._cur is None:
            return
        if tag == "div":
            self._card_depth += 1
        if tag == "a" and "c-c" in cls:
            self._cur["href"] = amap.get("href", "")
        if tag == "div" and "name" in cls.split():
            self._capture, self._buf = "name", []
        elif tag == "div" and "score" in cls.split():
            self._capture, self._buf = "score", []
        elif tag == "div" and "details" in cls.split():
            self._capture, self._buf = "details", []

    def handle_endtag(self, tag):
        if not self._in_card or self._cur is None:
            return
        if self._capture and tag == "div":
            text = clean("".join(self._buf))
            if self._capture == "name":
                self._pending_team["name"] = strip_rank(text)
                if self._pending_team["name"]:
                    self._cur["teams"].append(dict(self._pending_team))
                    self._pending_team = {"name": "", "score": ""}
            elif self._capture == "score":
                self._pending_team["score"] = text
            elif self._capture == "details":
                self._cur["details"] = text
            self._capture, self._buf = None, []
        if tag == "div":
            self._card_depth -= 1
            if self._card_depth <= 0:
                if self._cur.get("state_attr") != "placeholder":
                    self.cards.append(self._cur)
                self._in_card = False
                self._cur = None

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)


def parse_cards(html: str) -> List[dict]:
    p = ScoreParser()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    if p.cards:
        return p.cards
    for m in re.finditer(r'class="contest-box-item"[^>]*>(.{0,2500}?)</div>\s*</li>', html, re.S):
        block = m.group(0)
        names = [strip_rank(clean(re.sub(r"<[^>]+>", "", x))) for x in re.findall(r'class="name"[^>]*>(.*?)</div>', block, re.S)]
        names = [n for n in names if n]
        scores = [clean(re.sub(r"<[^>]+>", "", x)) for x in re.findall(r'class="score"[^>]*>(.*?)</div>', block, re.S)]
        hm = re.search(r'href="([^"]+)"', block)
        href = hm.group(1) if hm else ""
        dm = re.search(r'class="details"[^>]*>(.*?)</div>', block, re.S)
        det = clean(re.sub(r"<[^>]+>", " ", dm.group(1))) if dm else ""
        if len(names) >= 2:
            teams = [{"name": names[i], "score": scores[i] if i < len(scores) else ""} for i in range(2)]
            p.cards.append({"href": href, "teams": teams, "details": det, "state_attr": ""})
    return p.cards


def parse_calendar(html: str) -> List[Tuple[str, int]]:
    """Parse kalender; hanya kembalikan hari ini & tanggal mendatang."""
    best: Dict[str, int] = {}
    for href, count in re.findall(
        r'href="[^"]*[?&]date=(\d{1,2}/\d{1,2}/\d{4})"[^>]*data-contest-count="(\d+)"',
        html,
    ):
        best[href] = max(int(count), best.get(href, 0))
    for href in re.findall(r'href="[^"]*[?&]date=(\d{1,2}/\d{1,2}/\d{4})"[^>]*class="[^"]*active', html):
        best.setdefault(href, 0)

    today = datetime.now().date()

    def key(item):
        d = item[0].split("/")
        return (int(d[2]), int(d[0]), int(d[1]))

    def is_today_or_future(mdy: str) -> bool:
        try:
            parts = mdy.split("/")
            dt = datetime(int(parts[2]), int(parts[0]), int(parts[1])).date()
            return dt >= today
        except Exception:
            return False

    filtered = [(d, c) for d, c in best.items() if is_today_or_future(d)]
    return sorted(filtered, key=key)


def load_cache() -> dict:
    if os.path.isfile(MASCOT_CACHE):
        try:
            with open(MASCOT_CACHE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    with open(MASCOT_CACHE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2)


_CACHE_LOCK = __import__("threading").Lock()


def load_cache_safe() -> dict:
    with _CACHE_LOCK:
        return load_cache()


def merge_save_cache(updates: dict) -> None:
    """Merge partial cache updates thread-safely."""
    if not updates:
        return
    with _CACHE_LOCK:
        cache = load_cache()
        cache.update(updates)
        save_cache(cache)


def scrape_game_info(game_url: str, insecure: bool) -> Tuple[str, str, str]:
    """Ambil mascot + teks Game Info dari halaman game MaxPreps.

    Returns: (mascot_a, mascot_b, game_info_description)

    Contoh:
      The Animo Robinson varsity football team has an away non-conference
      game @ Chadwick (Palos Verdes Peninsula, CA) on Friday, September 11 @ 3:30p.
    """
    if not game_url:
        return "", "", ""
    code, page = fetch(game_url, insecure=insecure)
    if code != 200:
        return "", "", ""

    names = [clean(x) for x in re.findall(r'class="mascot-name"\s*>\s*([^<]+)', page)]
    if len(names) < 2:
        for extra in re.findall(r'"mascot"\s*:\s*"([^"]+)"', page):
            extra = clean(extra)
            if extra and extra not in names:
                names.append(extra)
    mascot_a = names[0] if names else ""
    mascot_b = names[1] if len(names) > 1 else ""

    description = ""
    m = re.search(
        r'class="game-info"[^>]*>.{0,1200}?class="description"[^>]*>(.*?)</div>',
        page,
        re.S | re.I,
    )
    if not m:
        m = re.search(r'class="description"[^>]*>(.*?)</div>', page, re.S | re.I)
    if m:
        description = clean(re.sub(r"<[^>]+>", " ", m.group(1)))
        description = re.sub(r"\s*@\s*", " @ ", description)
        description = re.sub(r"\s{2,}", " ", description).strip()

    return mascot_a, mascot_b, description


def scrape_mascots(game_url: str, insecure: bool) -> Tuple[str, str]:
    a, b, _ = scrape_game_info(game_url, insecure)
    return a, b


def pretty_clock(text: str) -> str:
    """7:00 PM / 6:00p → 6p ; keep Live / Final / quarter."""
    t = clean(text)
    t = re.sub(r"\s+", " ", t)
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", t, re.I)
    if m:
        hour = int(m.group(1))
        mins = m.group(2) or "00"
        ap = m.group(3).lower()
        clock = f"{hour}{ap}" if mins == "00" else f"{hour}:{mins}{ap}"
        t = re.sub(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\b", clock, t, flags=re.I)
    t = t.replace("Missing score", "score not reported")
    return t


def build_detail(team_a: str, team_b: str, raw_details: str, score_a: str, score_b: str) -> str:
    """Fallback kalau Game Info halaman game tidak tersedia."""
    status = pretty_clock(raw_details or "")
    low = status.lower()
    if status and not re.search(r"\b(live|final|quarter|halftime|progress|scheduled|tbd)\b", low):
        if re.search(r"\d+[ap]\b", low) or re.search(r"\d+:\d+", low):
            status = "starts at " + status
            if "started" in low or "start" in low:
                status = pretty_clock(raw_details)
    line = f"{team_a} @ {team_b}"
    bits = [line]
    if status:
        bits.append(status)
    if score_a or score_b:
        bits.append(f"{score_a or '-'}–{score_b or '-'}")
    return ", ".join(bits[:2]) + ((" | " + bits[2]) if len(bits) > 2 else "")


def watch_with_title(watch: str, team_a: str, team_b: str) -> str:
    """Tambah ?title=TeamA%20Vs.%20TeamB (atau &title= kalau URL sudah ada query)."""
    base = (watch or "").strip()
    if not base:
        return base
    parts = urllib.parse.urlsplit(base)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if k.lower() != "title"]
    q.append(("title", f"{team_a} Vs. {team_b}"))
    new_query = urllib.parse.urlencode(q, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def sport_hashtag_label(sport: str) -> str:
    """football → Football ; basketball-girls → BasketballGirls.
    Boys dihilangkan (default). Girls tetap ditempel agar beda.
    """
    label = SPORTS.get(sport, SPORTS["football"])[0]
    core = re.sub(r"\s*\([^)]*\)\s*", "", label).strip()
    gender = ""
    m = re.search(r"\((Girls)\)", label, re.I)
    if m:
        gender = "Girls"
    return hashtag(core + gender).lstrip("#") or "Football"


def format_match(
    title,
    team_a,
    mascot_a,
    team_b,
    mascot_b,
    tanggal,
    watch,
    detail,
    state_name,
    state_code: str = "",
    sport: str = "football",
    add_title: bool = True,
) -> str:
    """Format output:

    ===========================
    Title

    TeamA vs TeamB
    MascotA @ mascotB

    📺watch live: link
    🗒️Game Info

    #State #XXHSSport #MascotA #MascotB
    ============================
    """
    live = watch_with_title(watch, team_a, team_b) if add_title else (watch or "")
    state_tag = hashtag(state_name.replace(" ", ""))
    code = (state_code or "").upper() or "XX"
    sport_part = sport_hashtag_label(sport)
    combo_tag = f"#{code}HS{sport_part}"
    mascot_tags = " ".join(t for t in [hashtag(mascot_a), hashtag(mascot_b)] if t)
    tags = " ".join(t for t in [state_tag, combo_tag, mascot_tags] if t)
    # Hanya tampilkan baris mascot jika keduanya ada; jika kosong → baris kosong
    if mascot_a and mascot_b:
        mascot_line = f"{mascot_a} @ {mascot_b}"
    elif mascot_a or mascot_b:
        mascot_line = mascot_a or mascot_b
    else:
        mascot_line = ""
    _ = tanggal  # kompatibilitas pemanggil
    return (
        "===========================\n"
        f"{title}\n"
        f"{team_a} vs {team_b}\n"
        f"{mascot_line}\n"
        f"📺watch live: {live}\n"
        f"🗒️{detail}\n"
        f"\n"
        f"{tags}\n"
        "============================\n"
        "\n"
    )



def state_file(state: str, sport: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    sport_safe = re.sub(r"[^a-z0-9]+", "-", (sport or "football").lower()).strip("-")
    return os.path.join(OUTPUT_DIR, f"{state.lower()}-{sport_safe}.txt")


def all_states_file(sport: str, mdy: str) -> str:
    """Satu file untuk All States: schedules/{sport}-{YYYY-MM-DD}.txt"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    sport_safe = re.sub(r"[^a-z0-9]+", "-", (sport or "football").lower()).strip("-")
    iso = mdy_to_iso(mdy)
    return os.path.join(OUTPUT_DIR, f"{sport_safe}-{iso}.txt")


def temp_output_path(state: str, sport: str, mdy: str) -> str:
    """Buat path file sementara unik di TEMP_DIR."""
    sport_safe = re.sub(r"[^a-z0-9]+", "-", (sport or "football").lower()).strip("-")
    iso = mdy_to_iso(mdy)
    stamp = datetime.now().strftime("%H%M%S")
    if state == "all":
        name = f"{sport_safe}-all-{iso}-{stamp}.txt"
    else:
        name = f"{state.lower()}-{sport_safe}-{iso}-{stamp}.txt"
    return os.path.join(TEMP_DIR, name)


def set_latest_output(path: str) -> None:
    global _LATEST_OUTPUT
    _LATEST_OUTPUT = path


def get_latest_output() -> Optional[str]:
    return _LATEST_OUTPUT if _LATEST_OUTPUT and os.path.isfile(_LATEST_OUTPUT) else None


def block_key(block: str) -> str:
    lines = block.strip().splitlines()
    return "\n".join(lines[:3]) if len(lines) >= 3 else block


def write_blocks(path: str, blocks: List[str], overwrite: bool) -> Tuple[int, int]:
    """overwrite=True ganti isi file. False = append, skip duplikat."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    written = skipped = 0
    if overwrite:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(blocks))
        set_latest_output(path)
        return len(blocks), 0
    existing = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    with open(path, "a", encoding="utf-8") as fh:
        for block in blocks:
            key = block_key(block)
            if key and key in existing:
                skipped += 1
                continue
            fh.write(block)
            existing += block
            written += 1
    set_latest_output(path)
    return written, skipped


def append_unique(path: str, block: str) -> bool:
    w, _ = write_blocks(path, [block], overwrite=False)
    return w == 1


def list_dates(state: str, sport: str, insecure: bool = False) -> Tuple[int, str, List[Tuple[str, int]]]:
    # Jangan pernah fetch /all/ — MaxPreps tidak punya path itu
    probe = state if state in STATES else "tx"
    url = scores_url(probe, sport, None)
    code, page = fetch(url, referer="https://www.maxpreps.com/", insecure=insecure)
    if code != 200:
        return code, url, []
    return code, url, parse_calendar(page)


def list_dates_with_fallback(sport: str, insecure: bool = False) -> Tuple[int, str, List[Tuple[str, int]], str]:
    """Coba beberapa state acuan sampai kalender kebaca (untuk mode All States)."""
    for probe in ("tx", "ca", "fl", "oh", "pa", "ga"):
        code, url, dates = list_dates(probe, sport, insecure)
        if code == 200 and dates:
            return code, url, dates, probe
        if code == 200 and not dates:
            # halaman ok tapi kosong — coba state lain
            continue
    # last attempt result
    code, url, dates = list_dates("tx", sport, insecure)
    return code, url, dates, "tx"


def scrape_state(
    state: str,
    sport: str,
    mdy: str,
    watch: str,
    with_mascots: bool,
    mascot_limit: int,
    insecure: bool,
    add_title: bool = True,
    with_game_info: bool = True,
    game_info_limit: int = 80,
    game_workers: int = 4,
) -> Tuple[int, str, List[str]]:
    """Scrape scores + (opsional) Game Info paralel per state."""
    state = state.lower()
    sport_label = SPORTS.get(sport, SPORTS["football"])[0]
    state_name = STATES.get(state, state.upper())
    title = f"{state_name} High School {sport_label}"
    tanggal = format_tanggal(mdy)
    url = scores_url(state, sport, mdy)
    code, page = fetch(url, referer=f"https://www.maxpreps.com/{state}/", insecure=insecure)
    if code != 200:
        return code, url, []
    cards = parse_cards(page)
    cache = load_cache_safe()
    fetch_limit = mascot_limit if with_mascots else game_info_limit
    need_fetch = (with_game_info or with_mascots) and fetch_limit > 0

    prepared = []
    pending_urls = []
    for card in cards:
        teams = card.get("teams") or []
        if len(teams) < 2:
            continue
        team_a, team_b = teams[0]["name"], teams[1]["name"]
        score_a, score_b = teams[0].get("score", ""), teams[1].get("score", "")
        href = card.get("href") or ""
        if href.startswith("/"):
            href = urljoin("https://www.maxpreps.com/", href)
        mascot_a = mascot_b = ""
        game_info = ""
        ck = f"{state}|{team_a}|{team_b}|{mdy}".lower()
        ck_legacy = f"{state}|{team_a}|{team_b}".lower()
        cached = cache.get(ck) or cache.get(ck_legacy)
        if cached and isinstance(cached, list) and len(cached) >= 2:
            mascot_a, mascot_b = cached[0] or "", cached[1] or ""
            if len(cached) >= 3:
                game_info = cached[2] or ""
        idx = len(prepared)
        prepared.append({
            "team_a": team_a, "team_b": team_b,
            "score_a": score_a, "score_b": score_b,
            "href": href, "ck": ck,
            "mascot_a": mascot_a, "mascot_b": mascot_b,
            "game_info": game_info,
            "raw_details": card.get("details") or "",
        })
        if need_fetch and href:
            want_info = with_game_info and not game_info
            want_mascot = with_mascots and not (mascot_a and mascot_b)
            if want_info or want_mascot:
                pending_urls.append(idx)

    pending_urls = pending_urls[:fetch_limit]
    cache_updates = {}

    def _fetch_one(idx: int):
        href = prepared[idx]["href"]
        time.sleep(random.uniform(0.05, 0.18))
        return idx, scrape_game_info(href, insecure)

    workers = max(1, min(game_workers, 8))
    if pending_urls:
        if workers == 1 or len(pending_urls) == 1:
            results = [_fetch_one(idx) for idx in pending_urls]
        else:
            results = []
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_fetch_one, idx) for idx in pending_urls]
                for fut in as_completed(futs):
                    try:
                        results.append(fut.result())
                    except Exception:
                        pass
        for i, (ma, mb, desc) in results:
            if ma or mb:
                prepared[i]["mascot_a"] = prepared[i]["mascot_a"] or ma
                prepared[i]["mascot_b"] = prepared[i]["mascot_b"] or mb
            if desc:
                prepared[i]["game_info"] = desc
            cache_updates[prepared[i]["ck"]] = [
                prepared[i]["mascot_a"], prepared[i]["mascot_b"], prepared[i]["game_info"]
            ]

    if cache_updates:
        merge_save_cache(cache_updates)

    blocks: List[str] = []
    for row in prepared:
        if row["game_info"]:
            details = row["game_info"]
        else:
            details = build_detail(
                row["team_a"], row["team_b"], row["raw_details"],
                row["score_a"], row["score_b"],
            )
        blocks.append(
            format_match(
                title, row["team_a"], row["mascot_a"], row["team_b"], row["mascot_b"],
                tanggal, watch, details, state_name,
                state_code=state, sport=sport, add_title=add_title,
            )
        )
    return code, url, blocks


def scrape_states_parallel(
    states: List[str],
    sport: str,
    mdy: str,
    watch: str,
    with_mascots: bool,
    mascot_limit: int,
    insecure: bool,
    add_title: bool,
    with_game_info: bool,
    game_info_limit: int,
    state_workers: int = 6,
    game_workers: int = 3,
) -> Tuple[List[str], List[str], int, str, List[str]]:
    """Scrape banyak state secara paralel. Returns all_blocks, failed, last_code, last_url, last_blocks."""
    all_blocks: List[str] = []
    failed: List[str] = []
    last_code, last_url = 0, ""
    last_blocks: List[str] = []
    lock = __import__("threading").Lock()

    def job(st: str):
        wurl = resolve_watch_url(watch, load_watch_map(), st, sport)
        return st, scrape_state(
            st, sport, mdy, wurl or WATCH_LIVE_DEFAULT,
            with_mascots, mascot_limit, insecure, add_title=add_title,
            with_game_info=with_game_info, game_info_limit=game_info_limit,
            game_workers=game_workers,
        )

    workers = max(1, min(state_workers, 10))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(job, st): st for st in states}
        for fut in as_completed(futs):
            st = futs[fut]
            try:
                st2, (code, url, blocks) = fut.result()
            except Exception as exn:
                with lock:
                    failed.append(f"{st.upper()} ERR {type(exn).__name__}")
                continue
            with lock:
                last_code, last_url = code, url
                if code != 200:
                    failed.append(f"{st2.upper()} HTTP {code}")
                else:
                    all_blocks.extend(blocks)
                    if blocks:
                        last_blocks = blocks
    return all_blocks, failed, last_code, last_url, last_blocks


def list_schedule_files() -> List[str]:
    """Daftar file .txt di schedules/ + schedules.txt di root."""
    files: List[str] = []
    if os.path.isfile(OUTPUT_FILE):
        files.append(OUTPUT_FILE)
    if os.path.isdir(OUTPUT_DIR):
        for name in sorted(os.listdir(OUTPUT_DIR)):
            if name.endswith(".txt"):
                files.append(os.path.join(OUTPUT_DIR, name))
    return files


def safe_schedule_path(req: str) -> Optional[str]:
    """Hanya izinkan file di dalam BASE_DIR (schedules atau schedules.txt)."""
    if not req:
        return None
    abs_path = os.path.abspath(req)
    base = os.path.abspath(BASE_DIR)
    if not abs_path.startswith(base) or not os.path.isfile(abs_path):
        return None
    if not abs_path.endswith(".txt"):
        return None
    return abs_path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="MaxPreps scraper (CLI). UI: streamlit run app.py"
    )
    ap.add_argument("--state", default="tx", help="Kode state atau 'all'")
    ap.add_argument("--sport", default="football")
    ap.add_argument("--date", default="", help="MM/DD/YYYY atau YYYY-MM-DD")
    ap.add_argument("--watch", default="")
    ap.add_argument("--check-dates", action="store_true")
    ap.add_argument("--mascots", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="Timpa file state")
    ap.add_argument("--combined", action="store_true", help="Tulis ke schedules.txt gabungan")
    ap.add_argument("--all-states", action="store_true", help="Scrape semua state")
    ap.add_argument("--no-title", action="store_true", help="Jangan tambah ?title= di watch live")
    args = ap.parse_args()

    state = (args.state or "tx").lower()
    if args.check_dates:
        code, url, days = list_dates(state, args.sport)
        print(f"HTTP {code} {url}")
        for d, c in days:
            print(f"  {d:12} {c:4}  {format_tanggal(d)}")
        return 0 if code == 200 else 1

    if not args.date and not args.all_states:
        print("UI Streamlit:  streamlit run app.py")
        print("CLI contoh:   python3 maxpreps_scraper.py --state tx --date 9/12/2026")
        print("              python3 maxpreps_scraper.py --check-dates --state ca")
        return 0

    watch = args.watch or resolve_watch_url("", load_watch_map(), state if state != "all" else "tx", args.sport)
    mdy = to_mdy(args.date or datetime.now().strftime("%Y-%m-%d"))
    if watch:
        save_watch_link(watch, state if state in STATES else "", args.sport)
    targets = list(STATES.keys()) if (args.all_states or state == "all") else [state]
    any_fail = False
    all_blocks: List[str] = []
    do_all = args.all_states or state == "all"
    gi_limit = (20 if args.mascots else 12) if do_all else (60 if args.mascots else 40)
    if do_all:
        all_blocks, failed, last_code, last_url, _ = scrape_states_parallel(
            targets, args.sport, mdy, watch,
            args.mascots, gi_limit, False, not args.no_title,
            with_game_info=True, game_info_limit=gi_limit,
            state_workers=DEFAULT_STATE_WORKERS, game_workers=DEFAULT_GAME_WORKERS,
        )
        for msg in failed:
            print("[fail]", msg)
            any_fail = True
        print(f"parallel done matches={len(all_blocks)} failed={len(failed)}")
    else:
        for st in targets:
            wurl = resolve_watch_url(watch, load_watch_map(), st, args.sport)
            code, url, blocks = scrape_state(
                st, args.sport, mdy, wurl, args.mascots, gi_limit,
                False, add_title=not args.no_title, with_game_info=True,
                game_info_limit=gi_limit, game_workers=DEFAULT_GAME_WORKERS,
            )
            print(f"[{st}] HTTP {code} {url}  matches={len(blocks)}")
            if code != 200:
                any_fail = True
            else:
                dest = OUTPUT_FILE if args.combined else state_file(st, args.sport)
                w, s = write_blocks(dest, blocks, overwrite=args.overwrite)
                print(f"  written={w} skipped={s} file={dest}")
    if do_all:
        dest = all_states_file(args.sport, mdy)
        w, s = write_blocks(dest, all_blocks, overwrite=args.overwrite)
        print(f"ALL → written={w} skipped={s} file={dest} matches={len(all_blocks)}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
