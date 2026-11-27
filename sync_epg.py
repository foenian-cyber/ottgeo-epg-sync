import gzip
import io
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
CF_KV_NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]

EPG_SOURCES = [
    os.environ.get("EPG_PRIMARY_URL", "http://only4.tv/epg/epg.xml"),
    os.environ.get("EPG_SECONDARY_URL", "http://epg.team/3.1.xml.gz"),
]
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "https://tv.ottgeo.net/x/ottgeo?auth=OTTGeo_Local_2026")
USER_AGENT = "OTT-Play/1.0"


def norm_name(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\b(hd|fhd|sd|4k|tv|ge|geo)\b", "", s)
    s = re.sub(r"[\s\-_.:[\]()+]+", "", s)
    return s.strip()


def parse_xmltv_time(s: str) -> int:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:\s*([+-])(\d{2})(\d{2}))?", (s or "").strip())
    if not m:
        return 0
    dt = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]), tzinfo=timezone.utc)
    if m[7]:
        sign = 1 if m[7] == "+" else -1
        offset = timedelta(hours=int(m[8]), minutes=int(m[9])) * sign
        dt = dt - offset
    return int(dt.timestamp())


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_playlist_targets():
    ids = set()
    names = set()
    try:
        raw = fetch_bytes(PLAYLIST_URL, timeout=15).decode("utf-8", errors="ignore")
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("#EXTINF:"):
                continue
            for pat in (r'tvg-id="([^"]+)"', r'x-tvg-id="([^"]+)"'):
                m = re.search(pat, line, re.I)
                if m and m.group(1).strip():
                    ids.add(m.group(1).strip().lower())
            m_tn = re.search(r'tvg-name="([^"]+)"', line, re.I)
            if m_tn and m_tn.group(1).strip():
                n = norm_name(m_tn.group(1))
                if n:
                    names.add(n)
            if "," in line:
                ch_name = line.rsplit(",", 1)[1].strip()
                n = norm_name(ch_name)
                if n:
                    names.add(n)
        print(f"Playlist targets loaded: {len(ids)} ids, {len(names)} normalized names")
    except Exception as e:
        print(f"Warning: could not fetch playlist ({e}); syncing all channels with fresh EPG")
    return ids, names


def parse_epg_source(url: str, wanted_ids: set, wanted_names: set, now_utc: int):
    print(f"Fetching EPG: {url}")
    data = fetch_bytes(url, timeout=45)
    if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)

    min_utc = now_utc - 4 * 86400
    max_utc = now_utc + 3 * 86400

    # Map xmltv channel_id -> list of normalized display names
    ch_names_map = {}
    matched_cids = set()
    programs_by_cid = {}

    context = ET.iterparse(io.BytesIO(data), events=("end",))
    for _, elem in context:
        tag = elem.tag.lower()
        if tag == "channel":
            cid = (elem.attrib.get("id") or "").strip().lower()
            if cid:
                dns = []
                for dn in elem.findall("display-name"):
                    if dn.text:
                        nn = norm_name(dn.text)
                        if nn:
                            dns.append(nn)
                is_wanted = (not wanted_ids and not wanted_names) or (cid in wanted_ids) or any(n in wanted_names for n in dns)
                if is_wanted:
                    matched_cids.add(cid)
                    ch_names_map[cid] = dns
            elem.clear()

        elif tag == "programme":
            cid = (elem.attrib.get("channel") or "").strip().lower()
            if cid and (cid in matched_cids or not wanted_ids):
                start = parse_xmltv_time(elem.attrib.get("start", ""))
                stop = parse_xmltv_time(elem.attrib.get("stop", ""))
                if start > 0 and stop > start and stop >= min_utc and start <= max_utc:
                    best_title = ""
                    for t in elem.findall("title"):
                        txt = (t.text or "").strip()
                        if not txt:
                            continue
                        if (t.attrib.get("lang") or "").lower() == "ka":
                            best_title = txt
                            break
                        if not best_title:
                            best_title = txt
                    if best_title:
                        programs_by_cid.setdefault(cid, []).append([start, stop, best_title])
            elem.clear()

    # Keep only channels that actually have fresh (last 24h or future) programs
    fresh_cids = {}
    for cid, progs in programs_by_cid.items():
        if any(p[1] > now_utc - 86400 for p in progs):
            progs.sort(key=lambda x: x[0])
            fresh_cids[cid] = progs

    print(f"Parsed {url}: {len(fresh_cids)} fresh channels")
    return fresh_cids, ch_names_map


def cf_kv_bulk_write(items: list):
    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/bulk"
    for i in range(0, len(items), 1000):
        batch = items[i:i + 1000]
        payload = json.dumps(batch, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="PUT",
            headers={
                "Authorization": f"Bearer {CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if not res.get("success"):
                raise RuntimeError(f"Cloudflare KV bulk write failed: {res}")
    print(f"Uploaded {len(items)} KV keys successfully.")


def main():
    now_utc = int(time.time())
    wanted_ids, wanted_names = get_playlist_targets()

    merged_programs = {}
    lookup_map = {}

    for src in EPG_SOURCES:
        if not src:
            continue
        try:
            fresh_cids, ch_names_map = parse_epg_source(src, wanted_ids, wanted_names, now_utc)
            for cid, progs in fresh_cids.items():
                # Primary source wins if it already has fresh programs for this channel
                if cid not in merged_programs:
                    merged_programs[cid] = progs
                    for nn in ch_names_map.get(cid, []):
                        lookup_map.setdefault(nn, cid)
        except Exception as e:
            print(f"Error processing {src}: {e}", file=sys.stderr)

    if not merged_programs:
        print("No EPG programs extracted, skipping KV upload.")
        sys.exit(1)

    kv_items = [
        {
            "key": "epg_lookup",
            "value": json.dumps(lookup_map, ensure_ascii=False, separators=(",", ":")),
            "expiration_ttl": 60 * 60 * 24 * 14,
        }
    ]
    for cid, progs in merged_programs.items():
        kv_items.append({
            "key": f"epg_ch:{cid}",
            "value": json.dumps(progs, ensure_ascii=False, separators=(",", ":")),
            "expiration_ttl": 60 * 60 * 24 * 14,
        })

    cf_kv_bulk_write(kv_items)


if __name__ == "__main__":
    main()
