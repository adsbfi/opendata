"""Diagnose why a specific adsb.lol day yields no Air Haifa traces.

For a given date this script:

  1. Probes every known release variant (prod-0, prod-1, staging-0,
     mlatonly-0) plus every split part (.tar, .tar.aa..tar.af) and
     reports which ones actually return 200.
  2. Resolves total Content-Length across all parts.
  3. Scans the chosen release with the same MultiPartTarReader algorithm
     Airtracker.py uses, looking for two sets of trace files:
        * Air Haifa fleet      739600..739605  (4X-IHA..4X-IHF, ATR-72)
        * Control aircraft     738100..738103  (4X-ERA..4X-ERD, El Al
                                                787 — flies daily, same
                                                tar subdirs 00..03)
  4. Reports total bytes scanned, elapsed time, and which target paths
     were and were not found.

The control set shares the last-2-hex subdirs (00..05) with Air Haifa,
so if those tar directories exist in this release we will pass them
during the scan. Conclusions from the report:

  * Air Haifa missing AND control missing -> release was empty or scan
    is broken (network/HEAD failure, bad URL).
  * Air Haifa missing AND control present -> fleet just wasn't broadcast
    that day. Try a different date.
  * Both present -> Airtracker.py should also find them; the bug is
    elsewhere in the pipeline (path mapping, exception swallowing).

Usage:
    python diagnose_day.py                # yesterday UTC
    python diagnose_day.py 2026-06-19     # specific date
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. pip install requests")
    sys.exit(1)

USER_AGENT = "air-haifa-diagnose/1.0"
RELEASE_VARIANTS = ["prod-0", "prod-1", "staging-0", "mlatonly-0"]
SPLIT_SUFFIXES = ["", "aa", "ab", "ac", "ad", "ae", "af"]

AIR_HAIFA_HEXES = ["739600", "739601", "739602", "739603", "739604", "739605"]
CONTROL_HEXES = [
    "738100",  # 4X-ERA El Al B788
    "738101",  # 4X-ERB El Al B788
    "738102",  # 4X-ERC El Al B788
    "738103",  # 4X-ERD El Al B788
]


def hex_to_tar_path(hex_code: str) -> str:
    h = hex_code.lower().strip()
    return f"traces/{h[-2:]}/trace_full_{h}.json.gz"


@dataclass
class ReleaseInfo:
    variant: str
    urls: list[str]
    sizes: list[int]

    @property
    def total(self) -> int:
        return sum(self.sizes)


def probe_release_urls(date_obj, session) -> list[ReleaseInfo]:
    date_str = date_obj.strftime("%Y.%m.%d")
    year = date_obj.year
    results: list[ReleaseInfo] = []

    print(f"Probing release variants for {date_obj}…")
    for variant in RELEASE_VARIANTS:
        base = f"v{date_str}-planes-readsb-{variant}"
        url_prefix = (
            f"https://github.com/adsblol/globe_history_{year}/releases/"
            f"download/{base}/{base}.tar"
        )
        found_urls: list[str] = []
        found_sizes: list[int] = []
        for suffix in SPLIT_SUFFIXES:
            url = url_prefix if suffix == "" else f"{url_prefix}.{suffix}"
            try:
                r = session.head(url, allow_redirects=True, timeout=20)
            except requests.RequestException as e:
                print(f"  ~ {variant} {suffix or '(whole)'} -> error: {e}")
                continue
            if r.status_code == 200:
                cl = int(r.headers.get("Content-Length", "0"))
                found_urls.append(url)
                found_sizes.append(cl)
                print(f"  + {variant} .{suffix or 'tar'} -> 200 ({cl/1024/1024:.1f} MB)")
                if suffix == "":
                    break  # single-part release; no .aa..af
            else:
                if suffix in ("", "aa"):
                    # silent on later misses, but log first two
                    print(f"  - {variant} .{suffix or 'tar'} -> {r.status_code}")
                if suffix in ("ac", "ad", "ae", "af"):
                    break
        if found_urls:
            results.append(ReleaseInfo(variant, found_urls, found_sizes))
    return results


class MultiPartTarReader:
    HEADER_SIZE = 512
    BLOCK_SIZE = 512
    SCAN_CHUNK = 4 * 1024 * 1024

    def __init__(self, urls, sizes, session):
        self.urls = urls
        self.part_sizes = sizes
        self.total_size = sum(sizes)
        self.session = session

    def _fetch_range(self, start, length):
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
            r = self.session.get(
                url, headers={"Range": f"bytes={local_off}-{end}"}, timeout=120
            )
            if r.status_code not in (200, 206):
                raise RuntimeError(f"Range request gave {r.status_code} for {url}")
            result.extend(r.content)
            pos += to_read
            remaining -= to_read
            cumulative += sz
        return bytes(result)

    @staticmethod
    def _parse_header(block):
        if len(block) < 512 or block == b"\x00" * 512:
            return None
        name = block[0:100].rstrip(b"\x00").decode("utf-8", errors="replace")
        if not name:
            return None
        size_field = block[124:136].rstrip(b"\x00 ").decode("ascii", errors="replace")
        try:
            size = int(size_field, 8) if size_field else 0
        except ValueError:
            size = 0
        typeflag = block[156:157].decode("ascii", errors="replace")
        prefix = block[345:500].rstrip(b"\x00").decode("utf-8", errors="replace")
        if prefix:
            name = prefix + "/" + name
        return name, size, typeflag

    def find_files(self, target_paths, observed_subdirs, log_every_mb=100):
        targets = set(target_paths)
        found = {}
        pos = 0
        last_log = 0
        log_every = log_every_mb * 1024 * 1024
        while pos < self.total_size and targets:
            to_read = min(self.SCAN_CHUNK, self.total_size - pos)
            data = self._fetch_range(pos, to_read)
            if pos - last_log >= log_every:
                pct = pos / self.total_size * 100
                print(
                    f"  scanning… {pct:5.1f}% "
                    f"({pos // (1024*1024)}/{self.total_size // (1024*1024)} MB)"
                )
                last_log = pos
            offset_in_chunk = 0
            chunk_consumed = False
            while offset_in_chunk + self.HEADER_SIZE <= len(data) and targets:
                header_block = data[offset_in_chunk : offset_in_chunk + self.HEADER_SIZE]
                parsed = self._parse_header(header_block)
                if parsed is None:
                    offset_in_chunk += self.HEADER_SIZE
                    continue
                name, size, typeflag = parsed
                file_data_offset = pos + offset_in_chunk + self.HEADER_SIZE
                norm_name = name[2:] if name.startswith("./") else name
                # Track every traces/<subdir>/ we touch — even non-matches —
                # so we can prove we walked the right directories.
                m = re.match(r"traces/([0-9a-f]{2})/", norm_name)
                if m:
                    observed_subdirs.add(m.group(1))
                if norm_name in targets:
                    found[norm_name] = (file_data_offset, size)
                    targets.discard(norm_name)
                    print(f"  + FOUND: {norm_name} ({size:,} bytes)")
                if typeflag in ("0", "", "\x00"):
                    data_blocks = (size + self.BLOCK_SIZE - 1) // self.BLOCK_SIZE
                    skip = self.HEADER_SIZE + data_blocks * self.BLOCK_SIZE
                else:
                    skip = self.HEADER_SIZE
                next_offset = offset_in_chunk + skip
                if next_offset + self.HEADER_SIZE > len(data):
                    pos = pos + offset_in_chunk + skip
                    chunk_consumed = True
                    break
                else:
                    offset_in_chunk = next_offset
            if not chunk_consumed:
                pos += len(data)
            if not targets:
                break
        return found


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main() -> int:
    if len(sys.argv) > 1:
        try:
            date_obj = parse_date(sys.argv[1]).date()
        except ValueError:
            print(f"Bad date: {sys.argv[1]}. Use YYYY-MM-DD.")
            return 2
    else:
        date_obj = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    releases = probe_release_urls(date_obj, session)
    if not releases:
        print("\nNo release variant returned 200. adsb.lol may not have published")
        print("this date yet, or all variants failed. Try another date.")
        return 1

    chosen = releases[0]
    print(
        f"\nUsing variant {chosen.variant!r} "
        f"({len(chosen.urls)} part(s), total {chosen.total/1024/1024:.1f} MB)"
    )

    targets = {hex_to_tar_path(h): h for h in AIR_HAIFA_HEXES + CONTROL_HEXES}
    print("\nLooking for:")
    for path, hex_code in targets.items():
        tag = "(control)" if hex_code in CONTROL_HEXES else "(air haifa)"
        print(f"  {tag} {hex_code} -> {path}")

    reader = MultiPartTarReader(chosen.urls, chosen.sizes, session)
    observed_subdirs: set[str] = set()
    started = time.monotonic()
    try:
        found = reader.find_files(targets.keys(), observed_subdirs)
    except Exception as e:
        print(f"\nFATAL during scan: {e}")
        return 1
    elapsed = time.monotonic() - started

    print(f"\n--- Scan complete in {elapsed:.1f}s ---")
    print(f"Tar subdirs we passed through: {sorted(observed_subdirs)[:30]}"
          f"{' …' if len(observed_subdirs) > 30 else ''}"
          f"  (total {len(observed_subdirs)})")

    expected_subdirs = sorted({h[-2:] for h in AIR_HAIFA_HEXES + CONTROL_HEXES})
    missing_subdirs = [s for s in expected_subdirs if s not in observed_subdirs]
    if missing_subdirs:
        print(f"WARNING: never saw subdirs {missing_subdirs} — scan stopped early?")
    else:
        print(f"All expected subdirs {expected_subdirs} were walked.")

    print("\nResults:")
    for path, hex_code in targets.items():
        tag = "control " if hex_code in CONTROL_HEXES else "haifa  "
        if path in found:
            offset, size = found[path]
            print(f"  + {tag} {hex_code}: FOUND ({size:,} bytes)")
        else:
            print(f"  - {tag} {hex_code}: missing from this release")

    haifa_found = [h for h in AIR_HAIFA_HEXES if hex_to_tar_path(h) in found]
    control_found = [h for h in CONTROL_HEXES if hex_to_tar_path(h) in found]
    print("\nVerdict:")
    if not haifa_found and not control_found:
        print("  Both Air Haifa AND control aircraft missing.")
        print("  -> the scan is broken OR this release is empty/corrupt.")
        print("     Try another date, or check the part URLs above for issues.")
    elif not haifa_found and control_found:
        print(f"  Control aircraft found ({len(control_found)}/{len(CONTROL_HEXES)}),")
        print("  but Air Haifa fleet absent from this release.")
        print("  -> the scan works. Air Haifa simply did not broadcast that day,")
        print("     or wasn't seen by any feeder. Try a different date (e.g. a")
        print("     weekday outside Shabbat hours).")
    elif haifa_found and not control_found:
        print(f"  Air Haifa found ({len(haifa_found)}/{len(AIR_HAIFA_HEXES)}) but")
        print("  control set missing. Surprising but the pipeline works for")
        print("  Air Haifa on this date — proceed to Excel build.")
    else:
        print(f"  Air Haifa: {len(haifa_found)}/{len(AIR_HAIFA_HEXES)} found.")
        print(f"  Control:   {len(control_found)}/{len(CONTROL_HEXES)} found.")
        print("  -> Airtracker.py should also succeed. If it doesn't, the bug")
        print("     is elsewhere (cache lookup, exception swallowing, etc.).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
