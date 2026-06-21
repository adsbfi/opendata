"""Discover Air Haifa's Mode-S hex codes from public databases.

Primary source: tar1090-db (wiedehopf/tar1090-db on GitHub), a community-
maintained CSV that maps Mode-S hex code -> registration / type / operator.
It's the same database that powers tar1090, the readsb web UI, and most
self-hosted feeders. It's authoritative for established airline fleets and
is updated weekly. Format (semicolons):

    hex;registration;type_icao;flags;description;owner_short;operator;[extra]

Optional secondary check: adsb.fi /v2/hex/<hex> to verify each candidate is
currently broadcasting. Disabled when --no-live is passed; useful only on
machines where opendata.adsb.fi is reachable.

Run from the Air Haifa tracker directory:

    python find_airhaifa_hexes.py             # discovery + live check
    python find_airhaifa_hexes.py --no-live   # discovery only (no API calls)

Output: a Python literal that drops straight into DEFAULT_HEXES in
Airtracker.py, plus a human-readable table.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

TAR1090_DB_URL = (
    "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"
)
ADSB_FI_HEX = "https://opendata.adsb.fi/api/v2/hex/{hex}"
USER_AGENT = "air-haifa-fleet-discovery/2.0"
ADSB_FI_RATE_S = 1.05  # public limit is 1 req/s


@dataclass
class FleetMember:
    hex_code: str
    registration: str = ""
    type_code: str = ""
    description: str = ""
    operator: str = ""
    sources: set[str] = field(default_factory=set)
    live_seen: bool | None = None  # None = not checked, True/False after live probe

    def merge(self, other: "FleetMember") -> None:
        for attr in ("registration", "type_code", "description", "operator"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))
        self.sources |= other.sources
        if self.live_seen is None:
            self.live_seen = other.live_seen


def _http_get(url: str, timeout: float = 30.0) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        print(f"  ! HTTP {e.code} for {url}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"  ! Network error for {url}: {e.reason}", file=sys.stderr)
    return None


def fetch_tar1090_db() -> list[list[str]]:
    print(f"Fetching tar1090-db CSV ({TAR1090_DB_URL.split('/')[-1]})…")
    raw = _http_get(TAR1090_DB_URL, timeout=90)
    if not raw:
        raise SystemExit("Could not download tar1090-db. Check connectivity.")
    text = gzip.decompress(raw).decode("utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    print(f"  loaded {len(rows):,} aircraft records")
    return rows


def find_air_haifa(rows: list[list[str]]) -> dict[str, FleetMember]:
    """Match by operator string or by registration prefix + ATR type."""
    found: dict[str, FleetMember] = {}
    for row in rows:
        if len(row) < 5:
            continue
        hex_code = (row[0] or "").lower().strip()
        registration = (row[1] or "").upper().strip()
        type_code = (row[2] or "").upper().strip()
        description = row[4] if len(row) > 4 else ""
        operator = row[6] if len(row) > 6 else ""
        if not hex_code:
            continue

        op_norm = operator.replace(" ", "").lower()
        is_air_haifa_operator = op_norm in {"airhaifa", "airhaifaltd"} or (
            "haifa" in op_norm and "air" in op_norm
        )
        is_israeli_atr = (
            registration.startswith("4X-IH") or registration.startswith("4X-IZ")
        ) and type_code.startswith("AT7")

        if not (is_air_haifa_operator or is_israeli_atr):
            continue

        member = FleetMember(
            hex_code=hex_code,
            registration=registration,
            type_code=type_code,
            description=description,
            operator=operator,
            sources={"tar1090-db"},
        )
        if hex_code in found:
            found[hex_code].merge(member)
        else:
            found[hex_code] = member
    return found


def live_check(fleet: dict[str, FleetMember]) -> None:
    """Hit adsb.fi /v2/hex/<hex> for each candidate. Best-effort, skipped on failure."""
    print("\nVerifying with adsb.fi /v2/hex/ (1 req/s)…")
    last = 0.0
    any_ok = False
    for hex_code, member in fleet.items():
        wait = ADSB_FI_RATE_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        url = ADSB_FI_HEX.format(hex=hex_code)
        raw = _http_get(url, timeout=15)
        last = time.monotonic()
        if raw is None:
            member.live_seen = None
            print(f"  ? {hex_code} {member.registration:<10} live check failed")
            continue
        any_ok = True
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            member.live_seen = None
            continue
        ac_list = payload.get("ac") or payload.get("aircraft") or []
        member.live_seen = bool(ac_list)
        flag = "broadcasting" if member.live_seen else "silent right now"
        print(f"  - {hex_code} {member.registration:<10} {flag}")
    if not any_ok:
        print("  (no successful live calls — likely network/egress blocked.")
        print("   That's OK: tar1090-db is authoritative for fleet composition.)")


def print_report(fleet: dict[str, FleetMember]) -> None:
    print("\n" + "=" * 70)
    print(f"Air Haifa fleet: {len(fleet)} aircraft")
    print("=" * 70)
    if not fleet:
        print("No matches. Either Air Haifa isn't in tar1090-db yet, or the")
        print("operator string changed. Re-run after the next weekly update.")
        return

    ordered = sorted(fleet.values(), key=lambda m: m.registration or m.hex_code)
    live_flag = any(m.live_seen is not None for m in ordered)
    cols = ["HEX", "REG", "TYPE", "OPERATOR"]
    if live_flag:
        cols.append("LIVE")
    header = f"{'HEX':<8} {'REG':<10} {'TYPE':<6} {'OPERATOR':<20}"
    if live_flag:
        header += " LIVE"
    print(header)
    print("-" * 70)
    for m in ordered:
        line = f"{m.hex_code.upper():<8} {m.registration:<10} {m.type_code:<6} {m.operator:<20}"
        if live_flag:
            if m.live_seen is True:
                line += " yes"
            elif m.live_seen is False:
                line += " no"
            else:
                line += " ?"
        print(line)

    print("\nDrop-in for DEFAULT_HEXES in Airtracker.py:")
    print("DEFAULT_HEXES = [")
    for m in ordered:
        comment = m.registration or "unknown reg"
        if m.type_code:
            comment += f" / {m.type_code}"
        print(f'    "{m.hex_code.lower()}",  # {comment}')
    print("]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--no-live",
        action="store_true",
        help="Skip adsb.fi liveness check (useful behind restrictive egress).",
    )
    args = parser.parse_args()

    rows = fetch_tar1090_db()
    fleet = find_air_haifa(rows)
    if not args.no_live and fleet:
        live_check(fleet)
    print_report(fleet)
    return 0 if fleet else 1


if __name__ == "__main__":
    sys.exit(main())
