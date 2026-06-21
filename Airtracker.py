"""
Air Haifa Flight Tracker (Excel-versie)
=======================================
Volgt Air Haifa's vloot via:
  - Live posities (adsb.fi open API)
  - Historische data (adsb.lol GitHub releases, via HTTP range requests
    om alleen de relevante vliegtuigen uit de dagelijkse tar-archieven
    te halen - ~25 MB i.p.v. ~3.6 GB per dag)

Output: een Excel-bestand naast het script (air_haifa.xlsx) met tabs:
  - Vluchten: 1 rij per gedetecteerde vlucht (+ departure/arrival airport)
  - Posities: alle GPS-punten
  - Vloot:    metadata per vliegtuig
  - Live:     live posities (alleen als je live-logging aanzet)

Installatie:
    pip install requests openpyxl
    python Airtracker.py
"""
import os
import sys
import csv
import io
import json
import gzip
import tarfile
import threading
import time
import re
import math
from datetime import datetime, timedelta, timezone
from io import RawIOBase
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

try:
    import requests
except ImportError:
    print("ERROR: 'requests' niet geinstalleerd. pip install requests")
    sys.exit(1)

try:
    from openpyxl import Workbook, load_workbook
except ImportError:
    print("ERROR: 'openpyxl' niet geinstalleerd. pip install openpyxl")
    sys.exit(1)


# ===========================
# CONFIGURATIE
# ===========================
# Bevestigd via tar1090-db: 739600..739605 = 4X-IHA..4X-IHF (ATR-72-600).
DEFAULT_HEXES = ["739600", "739601", "739602", "739603", "739604", "739605"]

# PyInstaller-aware: bij een .exe wijst sys.executable naar de .exe locatie,
# bij een gewone Python-run wijst __file__ naar het script. We willen altijd
# de map naast het draaiende ding (zodat .cache/ en air_haifa.xlsx daar komen).
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
EXCEL_PATH = SCRIPT_DIR / "air_haifa.xlsx"
CACHE_DIR = SCRIPT_DIR / ".cache"
SETTINGS_PATH = SCRIPT_DIR / "air_haifa_settings.json"

ADSB_FI_API = "https://opendata.adsb.fi/api/v2/icao/"
LIVE_REFRESH_SEC = 30
USER_AGENT = "AirHaifaTracker/2.1 (personal use)"

# mlatonly-0 toegevoegd — sommige dagen is dat de enige variant.
RELEASE_VARIANTS = ["prod-0", "prod-1", "staging-0", "mlatonly-0"]
SPLIT_SUFFIXES = ["aa", "ab", "ac", "ad", "ae", "af"]

# Canonieke hex<->registratie database, gevoed door tar1090/readsb feeders.
TAR1090_DB_URL = (
    "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"
)

# Mediterrane luchthavens die Air Haifa potentieel aandoet.
# (ICAO, naam, lat, lon)
AIRPORTS = [
    ("LLHA", "Haifa",            32.8094, 35.0432),
    ("LLBG", "Tel Aviv Ben Gurion", 32.0114, 34.8867),
    ("LLER", "Eilat Ramon",      29.7236, 35.0125),
    ("LCLK", "Larnaca",          34.8751, 33.6249),
    ("LCPH", "Paphos",           34.7180, 32.4857),
    ("LGAV", "Athene",           37.9364, 23.9445),
    ("LGIR", "Heraklion",        35.3397, 25.1803),
    ("LGRP", "Rhodes",           36.4054, 28.0862),
    ("LGKP", "Karpathos",        35.4214, 27.1461),
    ("LGMK", "Mykonos",          37.4351, 25.3481),
    ("LBSF", "Sofia",            42.6967, 23.4114),
]
AIRPORT_RADIUS_KM = 10.0


# ===========================
# HTTP RANGE READER VOOR GESPLITSTE TARS
# ===========================
class MultiPartTarReader:
    HEADER_SIZE = 512
    BLOCK_SIZE = 512
    SCAN_CHUNK = 8 * 1024 * 1024  # 8 MB chunks (was 2 MB) -> sneller

    def __init__(self, urls, session=None, log_func=None):
        self.urls = urls
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.log = log_func or (lambda msg: None)
        self.part_sizes = None
        self.total_size = None

    def _resolve_sizes(self):
        if self.part_sizes is not None:
            return
        sizes = []
        for url in self.urls:
            r = self.session.head(url, allow_redirects=True, timeout=30)
            r.raise_for_status()
            cl = r.headers.get("Content-Length")
            if cl is None:
                raise RuntimeError(f"Geen Content-Length voor {url}")
            sizes.append(int(cl))
        self.part_sizes = sizes
        self.total_size = sum(sizes)

    def _fetch_range(self, start, length):
        self._resolve_sizes()
        result = bytearray()
        remaining = length
        pos = start
        cumulative = 0
        for url, sz in zip(self.urls, self.part_sizes):
            if remaining == 0:
                break
            if pos >= cumulative + sz:
                cumulative += sz
                continue
            local_off = pos - cumulative
            to_read = min(sz - local_off, remaining)
            end = local_off + to_read - 1
            r = self.session.get(url, headers={"Range": f"bytes={local_off}-{end}"},
                                 timeout=120)
            if r.status_code not in (200, 206):
                raise RuntimeError(f"Range request gaf {r.status_code} voor {url}")
            result.extend(r.content)
            pos += to_read
            remaining -= to_read
            cumulative += sz
        return bytes(result)

    def _open_stream(self, log_progress_every=None):
        """Sequential read-only file-like over the (possibly split) tar."""
        return _RangeStream(self, log_func=self.log,
                            progress_every_mb=(log_progress_every or 0) // (1024*1024))

    def find_files(self, target_paths, log_progress_every=None,
                   cancel_event=None, observed_subdirs=None,
                   sample_names=None):
        """Walk the tar with Python's tarfile module (handles pax / GNU
        long-name extensions correctly) and return {name: data_bytes}
        for any target_paths that show up. Optionally collects up to 30
        encountered file paths into sample_names for diagnostic output."""
        self._resolve_sizes()
        targets = set(target_paths)
        found = {}
        stream = self._open_stream(log_progress_every)
        try:
            tf = tarfile.open(fileobj=stream, mode="r|")
        except tarfile.TarError as e:
            self.log(f"  ! tarfile open faalde: {e}")
            return found
        try:
            for member in tf:
                if cancel_event and cancel_event.is_set():
                    self.log("  scan geannuleerd")
                    break
                name = member.name
                if name.startswith("./"):
                    name = name[2:]
                if observed_subdirs is not None:
                    m = re.match(r"traces/([0-9a-f]{2})/", name)
                    if m:
                        observed_subdirs.add(m.group(1))
                if (sample_names is not None and member.isfile()
                        and len(sample_names) < 30):
                    sample_names.append(name)
                if name in targets and member.isfile():
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    data = extracted.read()
                    found[name] = data
                    targets.discard(name)
                    self.log(f"  + gevonden: {name} ({len(data):,} bytes)")
                    if not targets:
                        break
        except tarfile.TarError as e:
            self.log(f"  ! tarfile fout tijdens scan: {e}")
        except Exception as e:
            self.log(f"  ! onverwachte fout tijdens scan: {e}")
        finally:
            try:
                tf.close()
            except Exception:
                pass
        return found


class _RangeStream(RawIOBase):
    """File-like wrapper that turns a MultiPartTarReader into a forward-only
    byte stream for tarfile. Buffers in 8 MB blocks and logs progress."""

    FETCH_CHUNK = 8 * 1024 * 1024

    def __init__(self, reader, log_func=None, progress_every_mb=100):
        self.reader = reader
        self.pos = 0
        self.total = reader.total_size
        self.log = log_func or (lambda m: None)
        self.progress_every_mb = progress_every_mb or 100
        self._buf = b""
        self._buf_pos = 0
        self._next_progress_mb = self.progress_every_mb

    def readable(self):
        return True

    def seekable(self):
        return False

    def read(self, size=-1):
        if size is None or size < 0:
            chunks = []
            while True:
                c = self.read(self.FETCH_CHUNK)
                if not c:
                    break
                chunks.append(c)
            return b"".join(chunks)
        out = bytearray()
        while size > 0 and self.pos < self.total:
            if self._buf_pos >= len(self._buf):
                to_fetch = min(self.FETCH_CHUNK, self.total - self.pos)
                self._buf = self.reader._fetch_range(self.pos, to_fetch)
                self._buf_pos = 0
                cur_mb = self.pos // (1024 * 1024)
                if cur_mb >= self._next_progress_mb:
                    pct = self.pos / self.total * 100
                    self.log(
                        f"  scannen... {pct:.0f}% "
                        f"({cur_mb}/{self.total // (1024*1024)} MB)"
                    )
                    self._next_progress_mb = cur_mb + self.progress_every_mb
            take = min(size, len(self._buf) - self._buf_pos)
            out += self._buf[self._buf_pos : self._buf_pos + take]
            self._buf_pos += take
            self.pos += take
            size -= take
        return bytes(out)


# ===========================
# RELEASE URL ONTDEKKING
# ===========================
def build_release_urls(date_obj, variant, session):
    date_str = date_obj.strftime("%Y.%m.%d")
    year = date_obj.year
    base_name = f"v{date_str}-planes-readsb-{variant}"
    base_url = (f"https://github.com/adsblol/globe_history_{year}/releases/"
                f"download/{base_name}/{base_name}.tar")
    # Eerst proberen of er een single-part .tar is.
    try:
        r = session.head(base_url, allow_redirects=True, timeout=15)
        if r.status_code == 200:
            return [base_url]
    except Exception:
        pass
    # Anders gesplitste parts .aa, .ab, ...
    parts = []
    for suffix in SPLIT_SUFFIXES:
        url = f"{base_url}.{suffix}"
        try:
            r = session.head(url, allow_redirects=True, timeout=15)
            if r.status_code == 200:
                parts.append(url)
            else:
                break
        except Exception:
            break
    return parts if parts else None


def find_release_for_date(date_obj, session, log_func):
    for variant in RELEASE_VARIANTS:
        urls = build_release_urls(date_obj, variant, session)
        if urls:
            log_func(f"  release variant: {variant} ({len(urls)} part(s))")
            return variant, urls
    return None, None


# ===========================
# HISTORISCHE DATA OPHALEN
# ===========================
def hex_to_tar_paths(hex_codes):
    """Map hex -> list of acceptable paths inside the tar. adsb.lol used
    .json.gz historically but ships plain .json in current releases, so
    we accept both."""
    paths = {}
    for hex_code in hex_codes:
        h = hex_code.lower().strip()
        if len(h) < 2 or not re.fullmatch(r"[0-9a-f]+", h):
            continue
        subdir = h[-2:]
        paths[h] = [
            f"traces/{subdir}/trace_full_{h}.json",
            f"traces/{subdir}/trace_full_{h}.json.gz",
        ]
    return paths


def fetch_day(date_obj, hex_codes, cache_dir, log_func, cancel_event=None):
    date_str = date_obj.strftime("%Y-%m-%d")
    day_dir = Path(cache_dir) / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    paths_map = hex_to_tar_paths(hex_codes)
    reverse_map = {}
    for hex_code, path_list in paths_map.items():
        for p in path_list:
            reverse_map[p] = hex_code

    needed_paths = set()
    cached = {}
    for hex_code in paths_map:
        local_plain = day_dir / f"{hex_code}.json"
        local_gz = day_dir / f"{hex_code}.json.gz"
        local_marker = day_dir / f"{hex_code}.missing"
        if local_plain.exists():
            cached[hex_code] = local_plain
        elif local_gz.exists():
            cached[hex_code] = local_gz
        elif local_marker.exists():
            cached[hex_code] = None
        else:
            for p in paths_map[hex_code]:
                needed_paths.add(p)

    if not needed_paths:
        log_func(f"  [{date_str}] al compleet in cache - overgeslagen")
        return cached

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    log_func(f"  [{date_str}] zoek release op GitHub...")
    variant, urls = find_release_for_date(date_obj, session, log_func)
    if not urls:
        log_func(f"  [{date_str}] geen release gevonden "
                 "(mogelijk nog niet gepubliceerd)")
        return cached

    reader = MultiPartTarReader(urls, session=session, log_func=log_func)
    try:
        reader._resolve_sizes()
        log_func(f"  totale grootte: {reader.total_size // (1024*1024)} MB")
        to_fetch = len([h for h in paths_map if h not in cached])
        log_func(f"  scannen naar {to_fetch} vliegtuig(en)...")
        found = reader.find_files(needed_paths, log_progress_every=200*1024*1024,
                                  cancel_event=cancel_event)
    except Exception as e:
        log_func(f"  [{date_str}] FOUT tijdens scannen: {e}")
        return cached

    # Per hex: was at least one of its candidate paths found?
    hexes_found = {reverse_map[p] for p in found}
    for hex_code in paths_map:
        if hex_code in cached:
            continue  # already handled (cache hit or .missing marker)
        if hex_code not in hexes_found:
            (day_dir / f"{hex_code}.missing").touch()
            cached[hex_code] = None
            log_func(f"  - {hex_code}: niet aanwezig in deze dag")

    for tar_path, data in found.items():
        if cancel_event and cancel_event.is_set():
            log_func("  geannuleerd")
            return cached
        hex_code = reverse_map[tar_path]
        try:
            ext = ".json.gz" if tar_path.endswith(".json.gz") else ".json"
            local_file = day_dir / f"{hex_code}{ext}"
            local_file.write_bytes(data)
            cached[hex_code] = local_file
            log_func(f"  + {hex_code}: opgeslagen ({len(data):,} bytes)")
        except Exception as e:
            log_func(f"  ! {hex_code}: opslaan fout: {e}")

    return cached


# ===========================
# TRACE PARSER
# ===========================
def parse_trace_file(json_gz_path, hex_code, date_obj):
    # Sniff for gzip magic bytes; fall through to plain JSON if not gzipped.
    try:
        with open(json_gz_path, "rb") as f:
            magic = f.read(2)
        if magic == b"\x1f\x8b":
            with gzip.open(json_gz_path, "rt", encoding="utf-8") as f:
                data = json.load(f)
        else:
            with open(json_gz_path, "rt", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        return None, []

    base_timestamp = data.get("timestamp", 0)
    metadata = {
        "hex": data.get("icao", hex_code),
        "registration": data.get("r", ""),
        "type": data.get("t", ""),
        "description": data.get("desc", ""),
        "operator": data.get("ownOp", ""),
        "year": data.get("year", ""),
    }

    rows = []
    for entry in data.get("trace", []):
        if not entry or len(entry) < 4:
            continue
        try:
            sec_offset = entry[0]
            lat = entry[1]
            lon = entry[2]
            alt = entry[3]
            gs = entry[4] if len(entry) > 4 else None
            track = entry[5] if len(entry) > 5 else None
            details = entry[8] if len(entry) > 8 and isinstance(entry[8], dict) else {}
            ts = base_timestamp + sec_offset
            rows.append({
                "hex": metadata["hex"],
                "registration": metadata["registration"],
                "timestamp_utc": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "epoch": ts,
                "lat": lat,
                "lon": lon,
                "altitude_ft": alt if alt not in ("ground", None) else 0,
                "on_ground": 1 if alt == "ground" else 0,
                "ground_speed_kt": gs,
                "track": track,
                "flight_callsign": (details.get("flight", "").strip()
                                    if isinstance(details, dict) else ""),
                "source_date": date_obj.strftime("%Y-%m-%d"),
            })
        except (IndexError, TypeError, ValueError):
            continue
    return metadata, rows


# ===========================
# AFSTAND + LUCHTHAVEN MATCHING
# ===========================
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def nearest_airport(lat, lon, radius_km=AIRPORT_RADIUS_KM):
    if lat is None or lon is None:
        return ""
    best = None
    best_dist = radius_km
    for icao, _, alat, alon in AIRPORTS:
        try:
            d = haversine_km(lat, lon, alat, alon)
        except (TypeError, ValueError):
            continue
        if d < best_dist:
            best_dist = d
            best = icao
    return best or ""


# ===========================
# VLUCHTDETECTIE
# ===========================
def detect_flights(positions, gap_minutes=30):
    flights = []
    by_hex = {}
    for p in positions:
        by_hex.setdefault(p["hex"], []).append(p)

    for hex_code, points in by_hex.items():
        points.sort(key=lambda r: r["epoch"])
        current = None
        last_time = None
        last_on_ground = True
        for p in points:
            t = p["epoch"]
            on_ground = bool(p["on_ground"])
            airborne = not on_ground and (p["altitude_ft"] or 0) > 500
            if current is None:
                if airborne:
                    current = _start_flight(p)
                last_time = t
                last_on_ground = on_ground
                continue
            gap = t - last_time if last_time else 0
            if gap > gap_minutes * 60 or (last_on_ground and airborne and gap > 300):
                if current and current["n_positions"] >= 3:
                    flights.append(_close_flight(current))
                current = _start_flight(p) if airborne else None
            else:
                if current is not None:
                    _extend_flight(current, p)
            last_time = t
            last_on_ground = on_ground
        if current and current["n_positions"] >= 3:
            flights.append(_close_flight(current))

    flights.sort(key=lambda f: (f["hex"], f["departure_utc"]))
    return flights


def _start_flight(p):
    return {
        "hex": p["hex"], "registration": p["registration"],
        "date": p["source_date"],
        "departure_utc": p["timestamp_utc"], "arrival_utc": p["timestamp_utc"],
        "_dep_epoch": p["epoch"], "_arr_epoch": p["epoch"],
        "_dep_lat": p["lat"], "_dep_lon": p["lon"],
        "_arr_lat": p["lat"], "_arr_lon": p["lon"],
        "callsign": p["flight_callsign"], "max_alt_ft": p["altitude_ft"] or 0,
        "_last_lat": p["lat"], "_last_lon": p["lon"],
        "_total_dist": 0.0, "n_positions": 1,
    }


def _extend_flight(f, p):
    f["arrival_utc"] = p["timestamp_utc"]
    f["_arr_epoch"] = p["epoch"]
    f["_arr_lat"] = p["lat"]
    f["_arr_lon"] = p["lon"]
    if p["altitude_ft"] and p["altitude_ft"] > f["max_alt_ft"]:
        f["max_alt_ft"] = p["altitude_ft"]
    if not f["callsign"] and p["flight_callsign"]:
        f["callsign"] = p["flight_callsign"]
    if f["_last_lat"] is not None and p["lat"] is not None:
        try:
            f["_total_dist"] += haversine_km(f["_last_lat"], f["_last_lon"],
                                             p["lat"], p["lon"])
        except (TypeError, ValueError):
            pass
    f["_last_lat"] = p["lat"]
    f["_last_lon"] = p["lon"]
    f["n_positions"] += 1


def _close_flight(f):
    duration = (f["_arr_epoch"] - f["_dep_epoch"]) / 60.0
    return {
        "hex": f["hex"], "registration": f["registration"], "date": f["date"],
        "departure_utc": f["departure_utc"], "arrival_utc": f["arrival_utc"],
        "duration_min": round(duration, 1), "callsign": f["callsign"],
        "departure_lat": f["_dep_lat"], "departure_lon": f["_dep_lon"],
        "arrival_lat": f["_arr_lat"], "arrival_lon": f["_arr_lon"],
        "departure_airport": nearest_airport(f["_dep_lat"], f["_dep_lon"]),
        "arrival_airport": nearest_airport(f["_arr_lat"], f["_arr_lon"]),
        "max_alt_ft": int(f["max_alt_ft"] or 0),
        "distance_km": round(f["_total_dist"], 1), "n_positions": f["n_positions"],
    }


# ===========================
# EXCEL OUTPUT
# ===========================
def build_excel(cache_dir, hex_codes, excel_path, log_func):
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        log_func("  nog geen cache data")
        return 0, 0

    all_positions = []
    metadata_per_hex = {}

    for day_dir in sorted(cache_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            date_obj = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        for hex_code in hex_codes:
            file_path = day_dir / f"{hex_code}.json"
            if not file_path.exists():
                file_path = day_dir / f"{hex_code}.json.gz"
            if not file_path.exists():
                continue
            meta, rows = parse_trace_file(file_path, hex_code, date_obj)
            if meta and not metadata_per_hex.get(hex_code, {}).get("registration"):
                metadata_per_hex[hex_code] = meta
            all_positions.extend(rows)

    all_positions.sort(key=lambda r: (r["hex"], r["epoch"]))
    flights = detect_flights(all_positions)

    existing_live_rows = []
    if excel_path.exists():
        try:
            old_wb = load_workbook(excel_path, read_only=True)
            if "Live" in old_wb.sheetnames:
                old_ws = old_wb["Live"]
                for row in old_ws.iter_rows(values_only=True):
                    existing_live_rows.append(row)
            old_wb.close()
        except Exception:
            pass

    wb = Workbook()
    ws_f = wb.active
    ws_f.title = "Vluchten"
    flight_headers = ["hex", "registration", "date", "departure_utc",
                      "arrival_utc", "duration_min", "callsign",
                      "departure_lat", "departure_lon",
                      "arrival_lat", "arrival_lon",
                      "departure_airport", "arrival_airport",
                      "max_alt_ft", "distance_km", "n_positions"]
    ws_f.append(flight_headers)
    for fl in flights:
        ws_f.append([fl.get(h, "") for h in flight_headers])

    ws_p = wb.create_sheet("Posities")
    pos_headers = ["hex", "registration", "timestamp_utc", "epoch", "lat", "lon",
                   "altitude_ft", "on_ground", "ground_speed_kt", "track",
                   "flight_callsign", "source_date"]
    ws_p.append(pos_headers)
    for p in all_positions:
        ws_p.append([p.get(h, "") for h in pos_headers])

    ws_v = wb.create_sheet("Vloot")
    fleet_headers = ["hex", "registration", "type", "description",
                     "operator", "year"]
    ws_v.append(fleet_headers)
    for hex_code, meta in metadata_per_hex.items():
        ws_v.append([meta.get(h, "") for h in fleet_headers])

    if existing_live_rows:
        ws_l = wb.create_sheet("Live")
        for row in existing_live_rows:
            ws_l.append(row)

    for ws in wb.worksheets:
        for col in ws.columns:
            max_len = max((len(str(c.value)) for c in col if c.value is not None),
                          default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 25)

    try:
        wb.save(excel_path)
    except PermissionError:
        raise PermissionError(
            f"Kan {excel_path.name} niet opslaan - sluit Excel als het bestand open is."
        )

    return len(all_positions), len(flights)


def append_live_to_excel(excel_path, data, log_func):
    try:
        if excel_path.exists():
            wb = load_workbook(excel_path)
        else:
            wb = Workbook()
            wb.active.title = "Vluchten"
            wb.create_sheet("Posities")
            wb.create_sheet("Vloot")

        if "Live" in wb.sheetnames:
            ws = wb["Live"]
        else:
            ws = wb.create_sheet("Live")
            ws.append(["timestamp_utc", "hex", "registration", "callsign",
                       "lat", "lon", "altitude_ft", "ground_speed_kt"])

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for ac in data.get("ac", []):
            if ac.get("lat") is None:
                continue
            ws.append([
                ts,
                (ac.get("hex") or "").lower(),
                ac.get("r", ""),
                (ac.get("flight") or "").strip(),
                ac.get("lat"),
                ac.get("lon"),
                ac.get("alt_baro"),
                ac.get("gs"),
            ])
        wb.save(excel_path)
    except PermissionError:
        log_func("  ! Kan Live tab niet schrijven - sluit Excel eerst")
    except Exception as e:
        log_func(f"  ! Live append fout: {e}")


# ===========================
# VLOOT AUTO-DETECT VIA tar1090-db
# ===========================
def discover_fleet_from_tar1090(log_func):
    """Haal de canonieke hex<->reg database op en filter op Air Haifa."""
    log_func("  download tar1090-db CSV...")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    r = session.get(TAR1090_DB_URL, timeout=90)
    r.raise_for_status()
    text = gzip.decompress(r.content).decode("utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    log_func(f"  database geladen: {len(rows):,} aircraft records")

    found = []
    for row in rows:
        if len(row) < 5:
            continue
        hex_code = (row[0] or "").lower().strip()
        registration = (row[1] or "").upper().strip()
        type_code = (row[2] or "").upper().strip()
        operator = (row[6] or "").strip() if len(row) > 6 else ""
        op_norm = operator.replace(" ", "").lower()
        is_air_haifa_op = "haifa" in op_norm and "air" in op_norm
        is_israeli_atr = (
            registration.startswith("4X-IH") or registration.startswith("4X-IZ")
        ) and type_code.startswith("AT7")
        if is_air_haifa_op or is_israeli_atr:
            found.append((hex_code, registration, type_code, operator))
    return found


# ===========================
# DIAGNOSE EEN SPECIFIEKE DAG
# ===========================
CONTROL_HEXES = ["738100", "738101", "738102", "738103"]  # 4X-ERA..4X-ERD El Al 788


def diagnose_one_day(date_obj, hex_codes, log_func, cancel_event=None):
    """Probeer alle release-varianten, scan met controle-hex codes erbij,
    rapporteer wat er echt aan de hand is."""
    log_func(f"=== Diagnose {date_obj} ===")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    log_func("Probeer alle release-varianten...")
    candidates = []
    for variant in RELEASE_VARIANTS:
        urls = build_release_urls(date_obj, variant, session)
        if not urls:
            log_func(f"  - {variant}: geen .tar / .tar.aa")
            continue
        try:
            total = 0
            sizes = []
            for u in urls:
                r = session.head(u, allow_redirects=True, timeout=20)
                sz = int(r.headers.get("Content-Length", "0"))
                sizes.append(sz)
                total += sz
            log_func(f"  + {variant}: {len(urls)} part(s), {total/1024/1024:.1f} MB")
            candidates.append((variant, urls, sizes, total))
        except Exception as e:
            log_func(f"  ! {variant}: fout {e}")

    if not candidates:
        log_func("Geen release gevonden voor deze datum. Kies een andere dag.")
        return

    variant, urls, sizes, total = candidates[0]
    log_func(f"Scan variant: {variant} ({total/1024/1024:.1f} MB)")

    target_map = {}
    for h in hex_codes:
        for ext in (".json", ".json.gz"):
            target_map[f"traces/{h[-2:]}/trace_full_{h}{ext}"] = ("haifa", h)
    for h in CONTROL_HEXES:
        for ext in (".json", ".json.gz"):
            target_map[f"traces/{h[-2:]}/trace_full_{h}{ext}"] = ("control", h)

    reader = MultiPartTarReader(urls, session=session, log_func=log_func)
    reader.part_sizes = sizes
    reader.total_size = total
    observed = set()
    samples = []
    try:
        found = reader.find_files(target_map.keys(),
                                  log_progress_every=200*1024*1024,
                                  cancel_event=cancel_event,
                                  observed_subdirs=observed,
                                  sample_names=samples)
    except Exception as e:
        log_func(f"FOUT tijdens scan: {e}")
        return

    log_func(f"Tar-subdirs bezocht: {len(observed)} (steekproef: "
             f"{sorted(observed)[:15]})")
    if samples:
        log_func("Eerste bestanden in tar:")
        for s in samples[:10]:
            log_func(f"    {s}")
    expected = sorted({h[-2:] for h in list(hex_codes) + CONTROL_HEXES})
    missing_dirs = [d for d in expected if d not in observed]
    if missing_dirs:
        log_func(f"WAARSCHUWING: subdirs niet gezien: {missing_dirs}")

    haifa_found, ctrl_found = set(), set()
    for path, (tag, h) in target_map.items():
        if path in found:
            (haifa_found if tag == "haifa" else ctrl_found).add(h)
    for h in hex_codes:
        marker = "+" if h in haifa_found else "-"
        log_func(f"  {marker} haifa   {h}: "
                 f"{'GEVONDEN' if h in haifa_found else 'ontbreekt'}")
    for h in CONTROL_HEXES:
        marker = "+" if h in ctrl_found else "-"
        log_func(f"  {marker} control {h}: "
                 f"{'GEVONDEN' if h in ctrl_found else 'ontbreekt'}")

    log_func("Oordeel:")
    if not haifa_found and not ctrl_found:
        log_func("  -> Scan vond niets. Release lijkt leeg of corrupt.")
    elif not haifa_found and ctrl_found:
        log_func("  -> Scan werkt (El Al gevonden), maar Air Haifa was die dag")
        log_func("     niet zichtbaar. Probeer andere datum.")
    else:
        log_func(f"  -> Air Haifa: {len(haifa_found)}/{len(hex_codes)} gevonden,")
        log_func(f"     control: {len(ctrl_found)}/{len(CONTROL_HEXES)}.")


# ===========================
# GUI
# ===========================
class AirHaifaTracker(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Air Haifa Flight Tracker")
        self.geometry("1000x760")

        self.settings = self._load_settings()
        self.hex_codes = list(self.settings.get("hex_codes", DEFAULT_HEXES))

        self.live_thread = None
        self.live_running = False
        self.live_log_enabled = False

        self.hist_cancel = threading.Event()
        self.hist_thread = None

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._build_ui()

    def _load_settings(self):
        if SETTINGS_PATH.exists():
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings(self):
        s = {"hex_codes": self.hex_codes}
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            print(f"Kon instellingen niet opslaan: {e}")

    def _build_ui(self):
        banner = ttk.Frame(self)
        banner.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(banner, text=f"Excel: {EXCEL_PATH}",
                  font=("TkDefaultFont", 9),
                  foreground="#555").pack(side="left")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.live_tab = ttk.Frame(nb)
        self.hist_tab = ttk.Frame(nb)
        self.settings_tab = ttk.Frame(nb)
        nb.add(self.live_tab, text="  Live")
        nb.add(self.hist_tab, text="  Historie")
        nb.add(self.settings_tab, text="  Instellingen")

        self._build_live_tab()
        self._build_hist_tab()
        self._build_settings_tab()

    def _build_live_tab(self):
        top = ttk.Frame(self.live_tab)
        top.pack(fill="x", padx=10, pady=10)

        self.live_status_label = ttk.Label(top, text="Niet actief",
                                           font=("TkDefaultFont", 10, "bold"))
        self.live_status_label.pack(side="left")

        self.live_btn = ttk.Button(top, text="Start live tracking",
                                   command=self._toggle_live)
        self.live_btn.pack(side="right", padx=4)

        self.live_log_btn = ttk.Button(top, text="Log naar Excel: UIT",
                                       command=self._toggle_live_log)
        self.live_log_btn.pack(side="right", padx=4)

        ttk.Button(top, text="Nu verversen",
                   command=self._live_refresh_once).pack(side="right", padx=4)

        cols = ("hex", "reg", "callsign", "status", "alt", "speed",
                "lat", "lon", "seen")
        self.live_tree = ttk.Treeview(self.live_tab, columns=cols,
                                      show="headings", height=10)
        headings = {
            "hex": ("Hex", 70),
            "reg": ("Registratie", 100),
            "callsign": ("Callsign", 90),
            "status": ("Status", 170),
            "alt": ("Hoogte (ft)", 90),
            "speed": ("Snelheid (kt)", 100),
            "lat": ("Lat", 90),
            "lon": ("Lon", 90),
            "seen": ("Laatst gezien", 110),
        }
        for c, (label, w) in headings.items():
            self.live_tree.heading(c, text=label)
            self.live_tree.column(c, width=w, anchor="w")
        self.live_tree.pack(fill="both", expand=True, padx=10, pady=5)

        ttk.Label(self.live_tab, text="Activiteit:").pack(anchor="w", padx=10)
        self.live_log = scrolledtext.ScrolledText(self.live_tab, height=8,
                                                  font=("Consolas", 9))
        self.live_log.pack(fill="both", expand=True, padx=10, pady=5)

    def _build_hist_tab(self):
        top = ttk.LabelFrame(self.hist_tab, text="Periode")
        top.pack(fill="x", padx=10, pady=10)

        ttk.Label(top, text="Van (YYYY-MM-DD):").grid(row=0, column=0,
                                                     sticky="w", padx=5, pady=5)
        self.date_from = ttk.Entry(top, width=15)
        default_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        self.date_from.insert(0, default_from)
        self.date_from.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(top, text="Tot (YYYY-MM-DD):").grid(row=0, column=2,
                                                     sticky="w", padx=5, pady=5)
        self.date_to = ttk.Entry(top, width=15)
        default_to = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.date_to.insert(0, default_to)
        self.date_to.grid(row=0, column=3, padx=5, pady=5)

        ttk.Label(top, text="Tip: adsb.lol publiceert een dag pas na afloop "
                            "(ongeveer 24u vertraging)",
                  foreground="gray").grid(row=1, column=0, columnspan=4,
                                          sticky="w", padx=5)

        self.force_redownload = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Forceer opnieuw downloaden",
                        variable=self.force_redownload).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=5, pady=5)

        btnframe = ttk.Frame(self.hist_tab)
        btnframe.pack(fill="x", padx=10, pady=5)

        self.hist_btn = ttk.Button(btnframe, text="Start download",
                                   command=self._start_hist_download)
        self.hist_btn.pack(side="left", padx=4)

        ttk.Button(btnframe, text="Update t/m gisteren",
                   command=self._update_until_yesterday).pack(side="left", padx=4)
        ttk.Button(btnframe, text="Diagnoseer 'Van'-datum",
                   command=self._diagnose_from_date).pack(side="left", padx=4)
        ttk.Button(btnframe, text="Exporteer naar Excel",
                   command=self._export_excel_only).pack(side="left", padx=4)
        ttk.Button(btnframe, text="Open Excel",
                   command=self._open_excel).pack(side="left", padx=4)
        ttk.Button(btnframe, text="Maak cache leeg",
                   command=self._clear_cache).pack(side="left", padx=4)

        self.hist_cancel_btn = ttk.Button(btnframe, text="Stop",
                                          command=self._cancel_hist,
                                          state="disabled")
        self.hist_cancel_btn.pack(side="right", padx=4)

        self.hist_progress = ttk.Progressbar(self.hist_tab, mode="determinate")
        self.hist_progress.pack(fill="x", padx=10, pady=5)

        self.hist_status = ttk.Label(self.hist_tab, text="Klaar")
        self.hist_status.pack(anchor="w", padx=10)

        ttk.Label(self.hist_tab, text="Voortgang:").pack(anchor="w",
                                                        padx=10, pady=(10, 0))
        self.hist_log = scrolledtext.ScrolledText(self.hist_tab, height=18,
                                                  font=("Consolas", 9))
        self.hist_log.pack(fill="both", expand=True, padx=10, pady=5)

    def _build_settings_tab(self):
        frm = ttk.Frame(self.settings_tab)
        frm.pack(fill="both", expand=True, padx=15, pady=15)

        ttk.Label(frm, text="Hex codes (een per regel):",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.hex_text = tk.Text(frm, width=20, height=10, font=("Consolas", 10))
        self.hex_text.pack(anchor="w", pady=5)
        self.hex_text.insert("1.0", "\n".join(self.hex_codes))

        btns = ttk.Frame(frm)
        btns.pack(anchor="w", pady=10)
        ttk.Button(btns, text="Hex codes opslaan",
                   command=self._save_settings_from_ui).pack(side="left", padx=4)
        ttk.Button(btns, text="Auto-detect vloot (tar1090-db)",
                   command=self._auto_detect_fleet).pack(side="left", padx=4)

        ttk.Label(frm, text="Log:").pack(anchor="w", pady=(10, 0))
        self.settings_log = scrolledtext.ScrolledText(frm, height=6,
                                                     font=("Consolas", 9))
        self.settings_log.pack(fill="x", pady=5)

        info = ttk.LabelFrame(frm, text="Hoe werkt dit?")
        info.pack(fill="x", pady=15)
        info_text = (
            "OUTPUT:\n"
            f"  Een Excel-bestand: {EXCEL_PATH.name}\n"
            "  Tabs: Vluchten / Posities / Vloot / Live\n"
            "\n"
            "AUTO-DETECT VLOOT:\n"
            "  Haalt de canonieke tar1090-db CSV van GitHub op en filtert\n"
            "  op operator 'airHaifa' + Israelische ATR-72's. Geen API key.\n"
            "\n"
            "HISTORIE:\n"
            "  adsb.lol publiceert dagelijks (~3.6 GB per dag).\n"
            "  HTTP range requests halen alleen jouw vliegtuigen op.\n"
            "  'Diagnoseer' toetst een datum met El Al controle-hexes.\n"
        )
        ttk.Label(info, text=info_text, justify="left",
                  font=("Consolas", 9)).pack(padx=10, pady=10, anchor="w")

    # ---------- LOG HELPERS ----------
    def _log_live(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.live_log.insert("end", f"[{ts}] {msg}\n")
        self.live_log.see("end")

    def _log_hist(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.hist_log.insert("end", f"[{ts}] {msg}\n")
        self.hist_log.see("end")
        try:
            self.hist_log.update_idletasks()
        except tk.TclError:
            pass

    def _log_settings(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.settings_log.insert("end", f"[{ts}] {msg}\n")
        self.settings_log.see("end")
        try:
            self.settings_log.update_idletasks()
        except tk.TclError:
            pass

    # ---------- SETTINGS ----------
    def _save_settings_from_ui(self):
        raw = self.hex_text.get("1.0", "end").strip()
        hexes = [line.strip().lower() for line in raw.splitlines() if line.strip()]
        valid = []
        for h in hexes:
            if re.fullmatch(r"[0-9a-f]{6}", h):
                valid.append(h)
            else:
                messagebox.showwarning(
                    "Ongeldige hex",
                    f"'{h}' is geen geldige 6-cijferige hex code. "
                    "Wordt overgeslagen.")
        if not valid:
            messagebox.showerror("Fout", "Geen geldige hex codes")
            return
        self.hex_codes = valid
        self._save_settings()
        messagebox.showinfo("Opgeslagen", f"{len(valid)} hex codes opgeslagen.")

    def _auto_detect_fleet(self):
        def run():
            try:
                self._log_settings("Auto-detect gestart...")
                found = discover_fleet_from_tar1090(self._log_settings)
                if not found:
                    self._log_settings("Geen Air Haifa vliegtuigen gevonden.")
                    return
                self._log_settings(f"Gevonden: {len(found)} vliegtuigen")
                for hex_code, reg, type_code, op in found:
                    self._log_settings(
                        f"  {hex_code.upper()} {reg:<10} {type_code:<6} {op}")
                hexes = sorted({h for h, *_ in found})
                self.after(0, lambda: self._apply_detected_hexes(hexes))
            except Exception as e:
                self._log_settings(f"FOUT: {e}")
        threading.Thread(target=run, daemon=True).start()

    def _apply_detected_hexes(self, hexes):
        if not messagebox.askyesno(
                "Hex codes overschrijven?",
                f"Auto-detect vond {len(hexes)} hex codes:\n\n"
                + "\n".join(hexes)
                + "\n\nVervangen door deze lijst?"):
            return
        self.hex_codes = hexes
        self.hex_text.delete("1.0", "end")
        self.hex_text.insert("1.0", "\n".join(hexes))
        self._save_settings()
        self._log_settings(f"Opgeslagen: {len(hexes)} hex codes.")

    # ---------- EXCEL ----------
    def _open_excel(self):
        if not EXCEL_PATH.exists():
            messagebox.showinfo(
                "Nog geen Excel",
                f"{EXCEL_PATH.name} bestaat nog niet.\n\n"
                "Download eerst wat data via de Historie tab.")
            return
        if sys.platform == "win32":
            os.startfile(str(EXCEL_PATH))
        elif sys.platform == "darwin":
            os.system(f'open "{EXCEL_PATH}"')
        else:
            os.system(f'xdg-open "{EXCEL_PATH}"')

    def _clear_cache(self):
        if not CACHE_DIR.exists() or not any(CACHE_DIR.iterdir()):
            messagebox.showinfo("Cache leeg", "Er is geen cache om te wissen.")
            return
        if not messagebox.askyesno(
                "Cache wissen?",
                "Dit verwijdert alle gedownloade ruwe data uit .cache/.\n\n"
                "De Excel blijft staan. Doorgaan?"):
            return
        try:
            import shutil
            shutil.rmtree(CACHE_DIR)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._log_hist("Cache gewist.")
            messagebox.showinfo("Klaar", "Cache is leeggemaakt.")
        except Exception as e:
            messagebox.showerror("Fout", f"Kon cache niet wissen: {e}")

    # ---------- LIVE ----------
    def _toggle_live(self):
        if self.live_running:
            self.live_running = False
            self.live_btn.config(text="Start live tracking")
            self.live_status_label.config(text="Gestopt")
            self._log_live("Live tracking gestopt")
        else:
            self.live_running = True
            self.live_btn.config(text="Stop live tracking")
            self.live_status_label.config(text="Actief (ververst elke 30s)")
            self.live_thread = threading.Thread(target=self._live_loop, daemon=True)
            self.live_thread.start()

    def _toggle_live_log(self):
        self.live_log_enabled = not self.live_log_enabled
        self.live_log_btn.config(
            text=f"Log naar Excel: {'AAN' if self.live_log_enabled else 'UIT'}"
        )

    def _live_refresh_once(self):
        threading.Thread(target=self._do_one_live_fetch, daemon=True).start()

    def _do_one_live_fetch(self):
        try:
            hexes_param = ",".join(self.hex_codes)
            url = ADSB_FI_API + hexes_param
            r = requests.get(url, timeout=15,
                             headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            data = r.json()
            self.after(0, lambda: self._update_live_table(data))
            if self.live_log_enabled:
                append_live_to_excel(
                    EXCEL_PATH, data,
                    log_func=lambda m: self.after(0, lambda: self._log_live(m)))
            self.after(0, lambda: self._log_live(
                f"Update: {data.get('total', 0)} actief "
                f"van {len(self.hex_codes)} vliegtuigen"))
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda m=err_msg: self._log_live(f"FOUT: {m}"))

    def _live_loop(self):
        while self.live_running:
            self._do_one_live_fetch()
            for _ in range(LIVE_REFRESH_SEC):
                if not self.live_running:
                    break
                time.sleep(1)

    def _update_live_table(self, data):
        for item in self.live_tree.get_children():
            self.live_tree.delete(item)

        active_hexes = {}
        for ac in data.get("ac", []):
            h = (ac.get("hex") or "").lower()
            if h:
                active_hexes[h] = ac

        for hex_code in self.hex_codes:
            ac = active_hexes.get(hex_code.lower())
            if ac:
                alt = ac.get("alt_baro", "")
                lat = ac.get("lat")
                lon = ac.get("lon")
                if alt == "ground":
                    status = "Op grond"
                    alt_str = "ground"
                elif lat is None or lon is None:
                    status = "Transponder aan, geen GPS"
                    alt_str = str(alt) if alt not in (None, "") else ""
                else:
                    status = "In de lucht"
                    alt_str = str(alt) if alt not in (None, "") else ""
                seen = ac.get("seen")
                seen_str = f"{seen:.0f}s geleden" if isinstance(seen, (int, float)) else ""
                values = (
                    (ac.get("hex") or "").upper(),
                    ac.get("r") or "",
                    (ac.get("flight") or "").strip(),
                    status,
                    alt_str,
                    ac.get("gs") if ac.get("gs") is not None else "",
                    f"{lat:.4f}" if isinstance(lat, (int, float)) else "",
                    f"{lon:.4f}" if isinstance(lon, (int, float)) else "",
                    seen_str,
                )
            else:
                values = (hex_code.upper(), "", "", "Geen signaal",
                          "", "", "", "", "")
            self.live_tree.insert("", "end", values=values)

    # ---------- HISTORISCH ----------
    def _parse_date_field(self, entry):
        try:
            return datetime.strptime(entry.get().strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    def _start_hist_download(self):
        if self.hist_thread and self.hist_thread.is_alive():
            messagebox.showwarning("Bezig", "Download is al bezig.")
            return
        d_from = self._parse_date_field(self.date_from)
        d_to = self._parse_date_field(self.date_to)
        if not d_from or not d_to:
            messagebox.showerror("Datumfout", "Gebruik formaat YYYY-MM-DD.")
            return
        if d_from > d_to:
            messagebox.showerror("Datumfout",
                                 "Van-datum moet voor tot-datum liggen.")
            return
        self._launch_hist(d_from, d_to, self.force_redownload.get())

    def _update_until_yesterday(self):
        if self.hist_thread and self.hist_thread.is_alive():
            messagebox.showwarning("Bezig", "Download is al bezig.")
            return
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        existing = set()
        if CACHE_DIR.exists():
            for d in CACHE_DIR.iterdir():
                if d.is_dir():
                    try:
                        existing.add(datetime.strptime(d.name, "%Y-%m-%d").date())
                    except ValueError:
                        pass
        if not existing:
            messagebox.showinfo(
                "Geen cache",
                "Cache is leeg. Doe eerst een gewone download voor een "
                "periode, daarna kun je deze knop gebruiken om bij te werken.")
            return
        d_from = min(existing) + timedelta(days=1)
        d_to = yesterday
        if d_from > d_to:
            self._log_hist("Cache is al actueel t/m gisteren.")
            return
        self._log_hist(f"Bijwerken: {d_from} t/m {d_to}")
        self._launch_hist(d_from, d_to, force=False)

    def _launch_hist(self, d_from, d_to, force):
        self.hist_cancel.clear()
        self.hist_cancel_btn.config(state="normal")
        self.hist_btn.config(state="disabled")
        self.hist_thread = threading.Thread(
            target=self._run_hist_download,
            args=(d_from, d_to, force),
            daemon=True
        )
        self.hist_thread.start()

    def _cancel_hist(self):
        self.hist_cancel.set()
        self._log_hist("Annulatie aangevraagd...")

    def _run_hist_download(self, d_from, d_to, force):
        try:
            days = (d_to - d_from).days + 1
            self._log_hist("=== Start download ===")
            self._log_hist(f"Periode: {d_from} t/m {d_to} ({days} dagen)")
            self._log_hist(f"Vliegtuigen: {', '.join(self.hex_codes)}")
            self.hist_progress.config(maximum=days, value=0)
            for i in range(days):
                if self.hist_cancel.is_set():
                    self._log_hist("Geannuleerd door gebruiker.")
                    break
                date_obj = d_from + timedelta(days=i)
                self.hist_status.config(text=f"Bezig met {date_obj}...")
                if force:
                    day_dir = CACHE_DIR / date_obj.strftime("%Y-%m-%d")
                    if day_dir.exists():
                        for f in day_dir.iterdir():
                            try:
                                f.unlink()
                            except Exception:
                                pass
                fetch_day(date_obj, self.hex_codes, CACHE_DIR,
                          log_func=self._log_hist,
                          cancel_event=self.hist_cancel)
                self.hist_progress.config(value=i + 1)
            self._log_hist("=== Download klaar. Bouwen Excel... ===")
            self.hist_status.config(text="Excel bouwen...")
            n_pos, n_flights = build_excel(CACHE_DIR, self.hex_codes,
                                           EXCEL_PATH, self._log_hist)
            self._log_hist(f"Klaar: {n_pos:,} posities, "
                           f"{n_flights} vluchten -> {EXCEL_PATH.name}")
            self.hist_status.config(text=f"Klaar: {n_pos:,} posities, "
                                         f"{n_flights} vluchten.")
        except Exception as e:
            self._log_hist(f"FOUT: {e}")
            import traceback
            self._log_hist(traceback.format_exc())
        finally:
            self.hist_btn.config(state="normal")
            self.hist_cancel_btn.config(state="disabled")

    def _diagnose_from_date(self):
        if self.hist_thread and self.hist_thread.is_alive():
            messagebox.showwarning("Bezig", "Download is al bezig.")
            return
        d = self._parse_date_field(self.date_from)
        if not d:
            messagebox.showerror("Datumfout", "Gebruik formaat YYYY-MM-DD.")
            return
        self.hist_cancel.clear()
        self.hist_cancel_btn.config(state="normal")
        self.hist_btn.config(state="disabled")

        def run():
            try:
                diagnose_one_day(d, self.hex_codes, self._log_hist,
                                 cancel_event=self.hist_cancel)
            except Exception as e:
                self._log_hist(f"FOUT: {e}")
            finally:
                self.hist_btn.config(state="normal")
                self.hist_cancel_btn.config(state="disabled")

        self.hist_thread = threading.Thread(target=run, daemon=True)
        self.hist_thread.start()

    def _export_excel_only(self):
        self._log_hist("Excel bouwen van lokale cache...")
        try:
            n_pos, n_flights = build_excel(CACHE_DIR, self.hex_codes,
                                           EXCEL_PATH, self._log_hist)
            self._log_hist(f"Klaar: {n_pos:,} posities, "
                           f"{n_flights} vluchten -> {EXCEL_PATH.name}")
            self.hist_status.config(text=f"Klaar: {n_pos:,} posities, "
                                         f"{n_flights} vluchten.")
        except Exception as e:
            self._log_hist(f"FOUT: {e}")
            messagebox.showerror("Excel fout", str(e))


def main():
    app = AirHaifaTracker()
    app.mainloop()


if __name__ == "__main__":
    main()
