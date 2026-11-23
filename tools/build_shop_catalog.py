"""Build the public, metadata-only catalog consumed by the Switch."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
import hashlib
import io
from itertools import chain
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit

from build_dlc_names import request


SOURCES = {
    "catalog": ("Langegen/switch-game-collection", "main"),
    "title_db": ("borntohonk/titledb", "master"),
    "statistics": ("Langegen/switch-games", "main"),
}
MIB = 1024 * 1024
WIRE_LIMIT = 12 * MIB
RAW_LIMIT = 48 * MIB
TITLE = re.compile(r"0100[0-9a-f]{12}", re.IGNORECASE)
DIGITS = re.compile(r"[0-9]+")
OFFICIAL_ICON = re.compile(r"https://img-eshop\.cdn\.nintendo\.net/i/[0-9a-f]{64}\.jpg")
MEDIA_HOSTS = (
    "nintendo.net", "nintendo.com", "nintendo.co.jp", "ntower.de", "imgur.com",
    "radikal.ru", "rutracker.org", "fastpic.org", "postimg.cc", "ibb.co",
    "ixbt.com", "imageban.ru", "rutracker.cc", "i-ru.tl", "imagetwist.com",
    "rutracker.wiki", "raw.githubusercontent.com",
)
LANGUAGES = (
    ("ko", "korean", "kor"), ("en", "english", "eng"), ("ja", "japanese", "jpn"),
    ("zh", "chinese", "chi"), ("fr", "french", "fra"), ("de", "german", "ger"),
    ("es", "spanish", "spa"), ("it", "italian", "ita"), ("pt", "portuguese", "por"),
    ("ru", "russian", "rus"), ("nl", "dutch", "dut"), ("ar", "arabic", "ara"),
)
MONTHS = ("january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december")


def short(value, limit):
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")


def bounded_int(value, limit=2**32 - 1):
    return min(max(value, 0), limit) if type(value) is int else 0


def title_id(value):
    return int(value, 16) if isinstance(value, str) and TITLE.fullmatch(value) else 0


def application_id(value):
    title = title_id(value)
    return title if not title & 0x1fff and title != 0x01005442584f0000 else 0


def description(row):
    for key in ("description", "intro"):
        value = short(row.get(key), 4800).strip()
        if value.lower() not in ("", "n/a", "none", "-", "unknown"):
            return value
    return ""


def genres(row):
    categories = row.get("category")
    if not isinstance(categories, list):
        categories = []
    labels = categories + short(row.get("genre"), 480).split(",")
    return unique(label.strip() for label in labels if isinstance(label, str)
                  and label.strip() and len(label.strip().encode("utf-8", "replace")) <= 144)[:24]


def unique(items):
    return list(dict.fromkeys(items))


def has_hangul(value):
    return bool(re.search("[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]", value))


def media(value):
    if not isinstance(value, str) or not value:
        return ""
    proxy = "https://wsrv.nl/?"
    if value.startswith(proxy):
        value = next(iter(parse_qs(value[len(proxy):]).get("url", [])), "")
        if not value:
            return ""
        if "://" not in value:
            value = "https://" + value
    if not value.startswith("https://") or len(value.encode("utf-8", "replace")) > 2048:
        return ""
    if any(ord(ch) <= 32 or ord(ch) == 127 or ch in "#\\" for ch in value):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if (not parsed.path.startswith("/") or not re.fullmatch(r"[a-z0-9.-]+", parsed.netloc)
            or not any(parsed.netloc == host or parsed.netloc.endswith("." + host) for host in MEDIA_HOSTS)):
        return ""
    return value


def screenshots(row):
    values = row.get("screenshots")
    return [url for value in values if (url := media(value))][:12] if isinstance(values, list) else []


def offer_versions(row):
    entries = []
    for encoded in short(row.get("titles"), 16384).split(";")[:128]:
        fields = encoded.split(":", 2)
        title = application_id(fields[0])
        if title:
            numeric = fields[1] if len(fields) > 1 else ""
            version = min(int(numeric), 2**32 - 1) if len(numeric) <= 10 and DIGITS.fullmatch(numeric) else 0
            entries.append((title, version, short(fields[2], 40) if len(fields) > 2 else ""))
    primary = application_id(row.get("title_id"))
    if primary:
        version = bounded_int(row.get("update_version"))
        display = short(row.get("version"), 40)
        for index, (title, old, label) in enumerate(entries):
            if primary == title:
                entries[index] = (title, old or version, label or display)
                break
        else:
            entries.append((primary, version, display))
    return entries


def release_date(value):
    match = re.match(r"[0-9]{4}", value)
    if not match:
        return 0
    month = next((index for index, month in enumerate(MONTHS, 1) if month in value.lower()), 0)
    return int(match.group()) * 10000 + month * 100


def byte_size(value):
    match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(TB|GB|MB|KB|B)?", value, re.IGNORECASE)
    if not match:
        return 0
    scale = {"TB": MIB * MIB, "GB": MIB * 1024, "MB": MIB, "KB": 1024}
    return min(int(float(match[1]) * scale.get((match[2] or "").upper(), 1)), 2**63 - 1)


def date_stamp(value):
    digits = "".join(ch for ch in short(value, 24) if ch in "0123456789")
    return int(digits[:14]) if len(digits) >= 8 else 0


def base_game(row, name):
    multiplayer = short(row.get("multiplayer"), 100).lower()
    players = [int(n) for n in re.findall(r"(?<![a-z0-9_])[1-8](?![a-z0-9_])", multiplayer)]
    interface = short(row.get("interface_lang"), 220).lower()
    return {
        "name": name, "original": name, "developer": short(row.get("developer"), 180),
        "publisher": short(row.get("publisher"), 180), "description": description(row),
        "languages": [code for code, label, tag in LANGUAGES if label in interface or f"[{tag}]" in interface],
        "release": release_date(short(row.get("year"), 60)), "players": 1 if multiplayer == "no" else max(players, default=0),
        "genres": genres(row), "screenshots": screenshots(row),
        "cover": "", "fallback_cover": "", "offers": [],
    }


def catalog_rows(records, statistics):
    result = {}
    for row in records:
        if not isinstance(row, dict):
            continue
        name = short(row.get("title"), 360)
        searchable = (name + " " + short(row.get("genre"), 100)).lower()
        kind = short(row.get("image_format"), 80).lower()
        container = next((label.upper() for label in ("nsz", "nsp", "xcz", "xci") if label in kind), "")
        topic = short(row.get("topic_id"), 20)
        magnet = short(row.get("magnet"), 8192)
        if (not name or not container
                or "homebrew" in searchable or "[demo]" in searchable or not DIGITS.fullmatch(topic)
                or not magnet.startswith("magnet:?xt=urn:btih:") or "\n" in magnet or "\r" in magnet):
            continue
        stat = statistics.get(topic, {})
        if not isinstance(stat, dict):
            stat = {}
        for title, version, display in offer_versions(row):
            if title not in result:
                result[title] = base_game(row, name.split(" [", 1)[0])
            offers = result[title]["offers"]
            if len(offers) < 24 and all(offer[0] != topic for offer in offers):
                offers.append([topic, version, display, byte_size(short(row.get("size"), 32)),
                               bounded_int(stat.get("seeds")), bounded_int(stat.get("downloads")),
                               date_stamp(stat.get("registered_at")), date_stamp(stat.get("updated_at")), container])
    return result


def locale(records, catalog, *, icons_only=False):
    merged = {}
    for row in records.values():
        if not isinstance(row, dict):
            continue
        title = application_id(row.get("id"))
        if title not in catalog or row.get("isDemo") is True:
            continue
        game = merged.setdefault(title, {})
        icon = media(row.get("iconUrl"))
        if OFFICIAL_ICON.fullmatch(icon):
            game.setdefault("cover", icon)
        if icons_only:
            continue
        fields = row.get("languages")
        languages = unique(value.lower().split("-", 1)[0] for value in fields
                           if isinstance(value, str) and 0 < len(value) <= 24) if isinstance(fields, list) else []
        date = bounded_int(row.get("releaseDate"))
        for key, value in (
            ("name", short(row.get("name"), 360)), ("publisher", short(row.get("publisher"), 180)),
            ("description", description(row)), ("languages", [lang for lang in languages if lang]),
            ("release", date if 20000101 <= date <= 21001231 else 0),
            ("players", bounded_int(row.get("numberOfPlayers"), 99)), ("screenshots", screenshots(row)),
        ):
            if value:
                game.setdefault(key, value)
        game["genres"] = unique(game.get("genres", []) + genres(row))[:24]
    return merged


def version_rows(raw, catalog):
    header, *rows = raw.decode("ascii").splitlines()
    if header not in ("id|version", "id|rightsId|version"):
        raise ValueError("Invalid TitleDB versions header")
    result = {}
    for line in rows:
        if not line:
            continue
        fields = line.split("|")
        if len(fields) != header.count("|") + 1:
            raise ValueError("Invalid TitleDB version record")
        title = title_id(fields[0])
        base, offset = title & ~0x1fff, title & 0x1fff
        if base not in catalog or (offset not in (0, 0x800) and offset < 0x1000):
            continue
        value = fields[-1]
        versions = result.setdefault(base, {})
        if value:
            if not DIGITS.fullmatch(value) or int(value) >= 2**32:
                raise ValueError("Invalid TitleDB version")
            versions[offset] = max(versions.get(offset) or 0, int(value))
        else:
            versions.setdefault(offset, None)
    return result


def generate(source, korean, english, japanese, british, hong_kong, statistics, top, versions):
    if not isinstance(source, list) or not isinstance(top, list) or not top or not all(
        isinstance(dataset, dict) and dataset for dataset in (korean, english, japanese, british, hong_kong, statistics)
    ):
        raise ValueError("Invalid upstream datasets")
    catalog = catalog_rows(source, statistics)
    ko = locale(korean, catalog)
    en = locale(english, catalog)
    ja = locale(japanese, catalog, icons_only=True)
    gb = locale(british, catalog, icons_only=True)
    hk = locale(hong_kong, catalog, icons_only=True)
    release_versions = version_rows(versions, catalog)
    ranks = {}
    for rank, row in enumerate(top, 1):
        if isinstance(row, dict):
            ranks.setdefault(application_id(row.get("title_id")), rank)
    if not 1000 <= len(catalog) <= 18000 or min(len(ko), len(en)) < 100 or len(release_versions) < 1000:
        raise ValueError("Incomplete upstream datasets")
    yield {"schema": 1, "count": len(catalog), "official_icons": True}
    for title, game in catalog.items():
        english_fields = dict(en.get(title, {}))
        korean_fields = dict(ko.get(title, {}))
        icons = unique(url for fields in (korean_fields, english_fields, gb.get(title, {}), ja.get(title, {}), hk.get(title, {}))
                       if (url := fields.get("cover")))
        game["genres"] = unique(korean_fields.pop("genres", []) + english_fields.pop("genres", []) + game["genres"])[:24]
        for key in ("name", "description"):
            if key in english_fields and not has_hangul(korean_fields.get(key, "")):
                korean_fields.pop(key, None)
        game.update(english_fields)
        game.update(korean_fields)
        game["cover"], game["fallback_cover"] = (icons + ["", ""])[:2]
        game["title"] = f"{title:016X}"
        game["rank"] = min(ranks.get(title, 0), 1000)
        game["versions"] = sorted(release_versions.get(title, {}).items())
        game["offers"].sort(key=lambda row: (row[1], row[4], row[6]), reverse=True)
        yield game


def write_catalog(output, documents):
    wire = io.BytesIO()
    raw_size = 0
    with gzip.GzipFile(fileobj=wire, mode="wb", compresslevel=9, mtime=0) as compressed:
        for row in documents:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            raw_size += len(line)
            if len(line) > 256 * 1024 or raw_size > RAW_LIMIT:
                raise ValueError("Catalog exceeds decoded limits")
            compressed.write(line)
    data = wire.getvalue()
    if len(data) > WIRE_LIMIT:
        raise ValueError("Catalog exceeds compressed limit")
    previous = output.read_bytes() if output.exists() else b""
    if previous != data:
        atomic_write(output, data)
    return {"catalog_sha256": hashlib.sha256(data).hexdigest(), "gzip_bytes": len(data),
            "raw_bytes": raw_size, "changed": previous != data}


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def refresh(root, manual):
    output = root / "data/shop-catalog.jsonl.gz"
    status = root / "data/shop-refresh-status.json"
    now = datetime.now(timezone.utc)
    hour = now.strftime("%Y-%m-%dT%HZ")
    try:
        previous = json.loads(status.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    current = output.read_bytes() if output.exists() else b""
    valid = (bool(current) and previous.get("official_icons") is True
             and hashlib.sha256(current).hexdigest() == previous.get("catalog_sha256"))
    if valid and not manual and previous.get("last_success_hour_utc") == hour and previous.get("result") == "success":
        return
    record = dict(previous, last_attempt_utc=now.isoformat(timespec="seconds"))

    def revision(pair):
        repo, branch = pair
        data = json.loads(request(f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}", 65536))
        sha = data["object"]["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Invalid upstream revision")
        return sha

    def read(key, path, limit):
        repo = SOURCES[key][0]
        return request(f"https://raw.githubusercontent.com/{repo}/{heads[key]}/{path}", limit)

    for attempt in range(2):
        try:
            with ThreadPoolExecutor(max_workers=9) as pool:
                heads = dict(zip(SOURCES, pool.map(revision, SOURCES.values())))
                if not valid or heads != previous.get("revisions") or manual:
                    args = (("catalog", "EN_catalog.json", 42 * MIB), ("title_db", "KR.ko.json", 85 * MIB),
                            ("title_db", "US.en.json", 125 * MIB), ("title_db", "JP.ja.json", 125 * MIB),
                            ("title_db", "GB.en.json", 125 * MIB), ("title_db", "HK.zh.json", 125 * MIB),
                            ("statistics", "switch_games_stats.json", 4 * MIB),
                            ("catalog", "top_100.json", 4 * MIB), ("title_db", "versions.txt", 8 * MIB))
                    tasks = [pool.submit(read, *arg) for arg in args]
                    *datasets, versions = [task.result() for task in tasks]
                    data = generate(*(json.loads(value) for value in datasets), versions)
                    header = next(data)
                    record.update(write_catalog(output, chain((header,), data)), count=header["count"], official_icons=True)
                else:
                    record["changed"] = False
            record.update(result="success", revisions=heads, last_success_hour_utc=hour,
                          last_success_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            break
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            print(f"Catalog attempt {attempt + 1}: {type(error).__name__}: {error}", file=sys.stderr)
            record.update(result="failure", changed=False)
            if attempt == 0:
                time.sleep(5)
    atomic_write(status, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps(record))
    if record["result"] != "success":
        raise RuntimeError("Catalog refresh failed")


if __name__ == "__main__":
    refresh(Path(sys.argv[1]), "--manual" in sys.argv)
