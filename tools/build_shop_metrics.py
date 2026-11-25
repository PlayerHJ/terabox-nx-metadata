"""Publish a compact, title-ID keyed IGDB supplement for NX Store."""

from collections import defaultdict
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from build_dlc_names import request
from build_shop_catalog import MIB, RAW_LIMIT, application_id, atomic_write


NSUID = re.compile(r"700100[0-9]{8}")
NINTENDO_HOSTS = {"www.nintendo.com", "www.nintendo.co.jp", "store-jp.nintendo.com",
                  "ec.nintendo.com", "www.nintendo.co.uk", "www.nintendo.de", "www.nintendo.fr",
                  "www.nintendo.co.kr", "store.nintendo.co.kr"}
LOCALES = ("KR.ko.json", "US.en.json", "GB.en.json", "JP.ja.json", "HK.zh.json")
EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
FEED_PATH = "data/shop-metrics.json.gz"
STATUS_PATH = "data/shop-metrics-status.json"
GAME_FIELDS = ("id,name,rating,rating_count,alternative_names.name,game_localizations.name,"
               "release_dates.platform,release_dates.date,"
               "involved_companies.publisher,involved_companies.company.name,"
               "websites.url,external_games.platform,external_games.url,external_games.uid,"
               "external_games.external_game_source")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Igdb:
    def __init__(self, client_id, secret):
        self.opener = build_opener(NoRedirect())
        self.next_request = 0
        body = urlencode(dict(client_id=client_id, client_secret=secret,
                              grant_type="client_credentials")).encode("ascii")
        token = self.post("https://id.twitch.tv/oauth2/token", body,
                          {"Content-Type": "application/x-www-form-urlencoded"}).get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("Twitch did not return an access token")
        self.headers = {"Client-ID": client_id, "Authorization": "Bearer " + token,
                        "Accept": "application/json", "Content-Type": "text/plain"}

    def post(self, url, body, headers):
        for attempt in range(4):
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 0.28
            try:
                with self.opener.open(Request(url, body, headers), timeout=25) as response:
                    payload = response.read(12 * MIB + 1)
                if len(payload) > 12 * MIB:
                    raise ValueError("IGDB response exceeds the size limit")
                return json.loads(payload)
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if attempt == 3 or code not in (429, 500, 502, 503, 504):
                    raise ValueError(f"Metadata API returned HTTP {code}") from None
            except (URLError, TimeoutError):
                if attempt == 3:
                    raise ValueError("Metadata API connection failed") from None
            time.sleep(2 ** attempt)

    def query(self, endpoint, query):
        rows = self.post("https://api.igdb.com/v4/" + endpoint, query.encode("ascii"), self.headers)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("Unexpected IGDB response")
        return rows

    def pages(self, endpoint, fields, where):
        cursor = 0
        for _ in range(120):
            rows = self.query(endpoint, f"fields {fields}; where ({where}) & id > {cursor}; "
                              "sort id asc; limit 500;")
            for row in rows:
                current = row.get("id")
                if type(current) is not int or current <= cursor:
                    raise ValueError("IGDB pagination did not advance")
                cursor = current
                yield row
            if len(rows) < 500:
                return
        raise ValueError("IGDB pagination exceeded its maximum")


def records(value):
    return (row for row in value if isinstance(row, dict)) if isinstance(value, list) else ()


def name_key(value):
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value.translate(str.maketrans("", "", "®™©"))).casefold()
    value = re.sub("[’'‘]", "", value)
    return " ".join("".join(char if char.isalnum() else " " for char in value).split())


def calendar_date(value):
    if type(value) is int:
        try:
            return datetime.strptime(str(value), "%Y%m%d").date() if len(str(value)) == 8 else None
        except ValueError:
            pass
    return None


def nintendo_keys(value):
    if not isinstance(value, str):
        return set()
    try:
        url = urlsplit(value)
        host = url.hostname
    except ValueError:
        return set()
    if url.scheme not in ("http", "https") or host not in NINTENDO_HOSTS or url.netloc != host:
        return set()
    path = url.path.lower().rstrip("/")
    if match := re.search(r"/(700100[0-9]{8})$", path):
        return {("nsu", match[1])}
    if re.fullmatch(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?store/products/[a-z0-9-]+-switch", path):
        return {("url", host + path)}
    if re.fullmatch(r"/titles/[a-z0-9-]+", path) and host == "store.nintendo.co.kr":
        return {("url", host + path)}
    return set()


def title_db_records(root, active):
    status = json.loads((root / "data/shop-refresh-status.json").read_text(encoding="utf-8"))
    sha = status["revisions"]["title_db"]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Missing TitleDB revision")
    for locale in LOCALES:
        source = f"https://raw.githubusercontent.com/borntohonk/titledb/{sha}/{locale}"
        document = json.loads(request(source, 125 * MIB))
        if not isinstance(document, dict):
            raise ValueError("Unexpected TitleDB catalog")
        for row in document.values():
            if isinstance(row, dict) and isinstance(title := row.get("id"), str) and title.upper() in active:
                yield title.upper(), row


def active_titles(root):
    with gzip.open(root / "data/shop-catalog.jsonl.gz", "rb") as source:
        header = json.loads(source.readline(4096))
        if header.get("schema") != 1 or not 1000 <= header.get("count", 0) <= 18000:
            raise ValueError("Unexpected shop catalog header")
        titles = set()
        total = 0
        while line := source.readline(256 * 1024 + 1):
            total += len(line)
            if total > RAW_LIMIT or len(line) > 256 * 1024:
                raise ValueError("Shop catalog exceeds its size limit")
            title = json.loads(line)["title"]
            if not application_id(title) or title in titles:
                raise ValueError("Unexpected shop title ID")
            titles.add(title)
    if len(titles) != header["count"]:
        raise ValueError("Incomplete shop catalog")
    return titles


def match_games(rows, games, platform, nintendo_sources):
    by_product = defaultdict(set)
    by_name = defaultdict(set)
    properties = {}
    for game in games:
        ident = game["id"]
        for site in records(game.get("websites")):
            for key in nintendo_keys(site.get("url")):
                by_product[key].add(ident)
        for external in records(game.get("external_games")):
            if external.get("platform") not in (None, platform):
                continue
            for key in nintendo_keys(external.get("url")):
                by_product[key].add(ident)
            uid = external.get("uid", "")
            if (external.get("external_game_source") in nintendo_sources and isinstance(uid, str)
                    and NSUID.fullmatch(uid)):
                by_product["nsu", uid].add(ident)
        names = [game.get("name")]
        for field in ("alternative_names", "game_localizations"):
            names.extend(row.get("name") for row in records(game.get(field)))
        for name in map(name_key, names):
            if name:
                by_name[name].add(ident)
        companies = {name_key(row["company"].get("name")) for row in records(game.get("involved_companies"))
                     if row.get("publisher") is True and isinstance(row.get("company"), dict)} - {""}
        dates = {date.fromordinal(EPOCH_ORDINAL + row["date"] // 86400)
                 for row in records(game.get("release_dates")) if row.get("platform") == platform
                 and type(row.get("date")) is int and 0 <= row["date"] <= 4102444800}
        properties[ident] = dates, companies

    strict = defaultdict(set)
    fallback = defaultdict(set)
    for title, row in rows:
        keys = nintendo_keys(row.get("url")) | nintendo_keys(row.get("eshopUrl"))
        if NSUID.fullmatch(nsu := str(row.get("nsuId", ""))):
            keys.add(("nsu", nsu))
        for key in keys:
            strict[title].update(by_product.get(key, ()))
        released = calendar_date(row.get("releaseDate"))
        if released is None:
            continue
        publisher = name_key(row.get("publisher"))
        for ident in by_name.get(name_key(row.get("name")), ()):
            dates, companies = properties[ident]
            if any(abs((candidate - released).days) <= 7 or
                   (candidate.year == released.year and publisher in companies) for candidate in dates):
                fallback[title].add(ident)

    matches = {}
    for title in strict.keys() | fallback.keys():
        candidates = strict[title] or fallback[title]
        if len(candidates) == 1:
            matches[title] = next(iter(candidates))
    return matches


def minutes(seconds):
    if type(seconds) is int and 0 < seconds <= 600000 * 60:
        return max(1, (seconds + 30) // 60)
    return 0


def encode_metrics(matches, games, times):
    rows = []
    for title, ident in sorted(matches.items()):
        game = games[ident]
        score, votes = game.get("rating"), game.get("rating_count")
        if type(score) not in (int, float) or not 0 <= score <= 100 or type(votes) is not int or not 0 < votes < 2 ** 32:
            score = votes = 0
        main, sides = times.get(ident, (0, 0))
        if votes or main or sides:
            rows.append([title, round(score), votes, main, sides])
    payload = json.dumps([1, rows], separators=(",", ":")).encode("ascii")
    wire = gzip.compress(payload, compresslevel=9, mtime=0)
    if not rows or len(rows) > 18000 or len(payload) > MIB or len(wire) > 384 * 1024:
        raise ValueError("Metrics feed is empty or exceeds its limits")
    return wire, len(rows)


def refresh(root, api):
    active = active_titles(root)
    platforms = api.query("platforms", 'fields id; where name = "Nintendo Switch"; limit 2;')
    if len(platforms) != 1 or type(platform := platforms[0].get("id")) is not int:
        raise ValueError("IGDB Nintendo Switch platform is unavailable")
    sources = api.query("external_game_sources", 'fields id; where name ~ *"Nintendo"*; limit 100;')
    source_ids = {row["id"] for row in sources}
    games = {row["id"]: row for row in api.pages("games", GAME_FIELDS, f"platforms = ({platform})")}
    if len(games) < 1000:
        raise ValueError("IGDB platform catalog is incomplete")
    matches = match_games(title_db_records(root, active), games.values(), platform, source_ids)
    ids = sorted(set(matches.values()))
    times = {}
    for start in range(0, len(ids), 200):
        batch = ",".join(map(str, ids[start:start + 200]))
        for row in api.pages("game_time_to_beats", "id,game_id,hastily,normally,count,updated_at",
                             f"game_id = ({batch}) & count > 0"):
            ident = row["game_id"]
            count = row["count"]
            rank = (count, row.get("updated_at", 0))
            if rank > times.get(ident, ((0, 0), ()))[0]:
                times[ident] = rank, (minutes(row.get("hastily")), minutes(row.get("normally")))
    wire, count = encode_metrics(matches, games, {key: values for key, (_, values) in times.items()})
    output = root / FEED_PATH
    if not output.exists() or output.read_bytes() != wire:
        atomic_write(output, wire)
    print(f"IGDB supplement: {count} title IDs, {len(wire)} compressed bytes")
    return wire


def succeeded_this_hour(root, hour):
    try:
        status = json.loads((root / STATUS_PATH).read_text(encoding="utf-8"))
        return (isinstance(status, dict) and status.get("last_success_hour_utc") == hour
                and status.get("metrics_sha256") == hashlib.sha256((root / FEED_PATH).read_bytes()).hexdigest())
    except (OSError, ValueError):
        return False


def refresh_when_due(root, create_api, *, periodic, now):
    hour = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%HZ")
    if periodic and succeeded_this_hour(root, hour):
        print(f"IGDB supplement already refreshed for {hour}")
        return
    wire = refresh(root, create_api())
    status = {"last_success_hour_utc": hour, "metrics_sha256": hashlib.sha256(wire).hexdigest()}
    atomic_write(root / STATUS_PATH, (json.dumps(status, indent=2) + "\n").encode("utf-8"))


def authenticated_api():
    client_id = os.environ.get("IGDB_CLIENT_ID")
    secret = os.environ.get("IGDB_CLIENT_SECRET")
    if not client_id or not secret:
        raise ValueError("IGDB credentials are required for a live refresh")
    return Igdb(client_id, secret)


if __name__ == "__main__":
    try:
        refresh_when_due(Path(sys.argv[1]), authenticated_api, periodic="--periodic" in sys.argv[2:],
                         now=datetime.now(timezone.utc))
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as error:
        print(f"IGDB refresh failed ({type(error).__name__}): {error}", file=sys.stderr)
        sys.exit(1)
