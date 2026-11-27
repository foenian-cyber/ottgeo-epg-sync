import os
import json
import gzip
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# EPG წყაროები (შეგიძლია დაამატო ან შეცვალო სურვილისამებრ)
EPG_SOURCES = [
    "http://only4.tv/epg/epg.xml",
    "http://epg.team/3.1.xml.gz"
]

OUT_DIR = "epg_data"
KEEP_PAST_DAYS = 4
KEEP_FUTURE_DAYS = 3


def parse_xmltv_time(ts_str: str) -> int:
    if not ts_str or len(ts_str) < 14:
        return 0
    try:
        dt = datetime.strptime(ts_str[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        rest = ts_str[14:].strip()
        if len(rest) >= 5 and rest[0] in ("+", "-"):
            sign = 1 if rest[0] == "+" else -1
            hours = int(rest[1:3])
            mins = int(rest[3:5])
            offset = timedelta(hours=hours, minutes=mins) * sign
            dt = dt - offset
        return int(dt.timestamp())
    except Exception:
        return 0


def sanitize_filename(ch_id: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in ch_id.strip())
    return safe[:120]


def download_stream(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "OTT-Play/1.0"})
    resp = urllib.request.urlopen(req, timeout=45)
    if url.lower().endswith(".gz"):
        return gzip.GzipFile(fileobj=resp)
    return resp


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now_utc = int(time.time())
    min_stop = now_utc - KEEP_PAST_DAYS * 86400
    max_start = now_utc + KEEP_FUTURE_DAYS * 86400

    channels_programs = {}
    channel_aliases = {}

    for src in EPG_SOURCES:
        print(f"Downloading {src} ...")
        try:
            stream = download_stream(src)
            context = ET.iterparse(stream, events=("end",))
            for event, elem in context:
                tag = elem.tag.split("}")[-1]
                if tag == "channel":
                    cid = (elem.attrib.get("id") or "").strip()
                    if cid:
                        aliases = {cid, cid.lower()}
                        for dn in elem.findall("display-name"):
                            if dn.text and dn.text.strip():
                                name_clean = dn.text.strip()
                                aliases.add(name_clean)
                                aliases.add(name_clean.lower())
                        channel_aliases[cid] = aliases
                    elem.clear()

                elif tag == "programme":
                    cid = (elem.attrib.get("channel") or "").strip()
                    start = parse_xmltv_time(elem.attrib.get("start", ""))
                    stop = parse_xmltv_time(elem.attrib.get("stop", ""))

                    if cid and start > 0 and stop > start and stop >= min_stop and start <= max_start:
                        title = ""
                        for t in elem.findall("title"):
                            if t.text and t.text.strip():
                                lang = (t.attrib.get("lang") or "").lower()
                                if lang == "ka" or not title:
                                    title = t.text.strip()
                                    if lang == "ka":
                                        break
                        if title:
                            channels_programs.setdefault(cid, []).append([start, stop, title])
                    elem.clear()
        except Exception as e:
            print(f"Error processing {src}: {e}")

    # ძველი ფაილების გასუფთავება
    for old_f in os.listdir(OUT_DIR):
        if old_f.endswith(".json"):
            try:
                os.remove(os.path.join(OUT_DIR, old_f))
            except Exception:
                pass

    written = 0
    for cid, progs in channels_programs.items():
        if not progs:
            continue
        progs.sort(key=lambda x: x[0])
        # დუბლიკატების მოშორება
        deduped = []
        last_start = -1
        for p in progs:
            if p[0] != last_start:
                deduped.append(p)
                last_start = p[0]

        fname = sanitize_filename(cid) + ".json"
        out_path = os.path.join(OUT_DIR, fname)
        payload = json.dumps(deduped, ensure_ascii=False, separators=(",", ":"))
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(payload)
        written += 1

        # ასევე ვინახავთ lowercase ვერსიას თუ განსხვავდება
        low_fname = sanitize_filename(cid.lower()) + ".json"
        if low_fname != fname:
            with open(os.path.join(OUT_DIR, low_fname), "w", encoding="utf-8") as f:
                f.write(payload)

    print(f"Done! Written {written} channel JSON files into {OUT_DIR}/ (0 KV writes used).")


if __name__ == "__main__":
    main()
