"""OpenDota / Valve replay helpers (stdlib only). Everything is cached in ~/.cache/dota-coach."""
import bz2
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.opendota.com/api"
CACHE = os.path.expanduser(os.environ.get("DOTA_COACH_CACHE", "~/.cache/dota-coach"))
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSER_DIR = os.path.join(SKILL_DIR, "parser")
PARSER_BIN = os.path.join(CACHE, "bin", "dotaparse")
IDS_PER_DAY = 1_550_000  # approx growth of match_id per day in 2026


def _p(*parts):
    path = os.path.join(CACHE, *parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


_RATE_LOCK = threading.Lock()
_LAST_CALL = [0.0]


def _throttle(url):
    if not url.startswith(API):
        return
    with _RATE_LOCK:
        wait = _LAST_CALL[0] + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.time()


STATS = {"requests": 0, "rate_limited": 0}  # OpenDota HTTP requests made by this process (incl. retries)


def http(url, method="GET", tries=5, timeout=60):
    for i in range(tries):
        _throttle(url)
        if url.startswith(API):
            with _RATE_LOCK:
                STATS["requests"] += 1
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": "dota-coach"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and url.startswith(API):
                with _RATE_LOCK:
                    STATS["rate_limited"] += 1
            if e.code in (400, 404) or i == tries - 1:
                raise
            time.sleep(30 * (i + 1) if e.code == 429 else 3 * (i + 1))
        except (urllib.error.URLError, TimeoutError, OSError):
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def env_setting(name):
    """`name` from the environment, else from an `export NAME=...` line in ~/.zshrc."""
    v = os.environ.get(name)
    if v:
        return v.strip()
    try:
        with open(os.path.expanduser("~/.zshrc")) as f:
            for line in f:
                m = re.match(r"\s*(?:export\s+)?" + re.escape(name) + r"=(['\"]?)([^'\"\s#]+)\1", line)
                if m:
                    return m.group(2)
    except OSError:
        pass
    return None


def _api_key():
    return env_setting("OPENDOTA_API_KEY")


API_KEY = _api_key()
MIN_INTERVAL = 0.05 if API_KEY else 1.05  # keyed: generous rate limit; free tier: 60 calls/min


def api(path, method="GET", **params):
    if API_KEY:
        params["api_key"] = API_KEY
    q = ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
    return json.loads(http(f"{API}/{path}{q}", method=method))


def explorer(sql):
    try:
        d = api("explorer", sql=sql)
    except urllib.error.HTTPError as e:
        d = json.loads(e.read() or b"{}") or {"err": str(e)}
    if d.get("err"):
        raise RuntimeError(d["err"])
    return d["rows"]


def constants(name):
    """heroes / items / ability_ids, cached for 7 days."""
    path = _p("constants", f"{name}.json")
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 7 * 86400:
        data = api(f"constants/{name}")
        with open(path, "w") as f:
            json.dump(data, f)
    with open(path) as f:
        return json.load(f)


def hero_names():
    return {int(v["id"]): v["localized_name"] for v in constants("heroes").values()}


def match(match_id, want_parsed=False, wait=300):
    """Match JSON from OpenDota (basic data: result, players, replay salt).
    want_parsed=True additionally waits for OpenDota's own full parse (slow, rarely needed now)."""
    path = _p("matches", f"{match_id}.json")
    if os.path.exists(path):
        with open(path) as f:
            m = json.load(f)
        if (m.get("version") or not want_parsed) and m.get("replay_salt"):
            return m
    m = api(f"matches/{match_id}")
    if not m.get("replay_salt") or (want_parsed and not m.get("version")):
        try:
            api(f"request/{match_id}", method="POST")
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < (wait if want_parsed else min(wait, 60)):
            time.sleep(4)
            m2 = api(f"matches/{match_id}")
            if m2.get("replay_salt"):
                m = m2
                if not want_parsed or m.get("version"):
                    break
    if m.get("replay_salt"):
        with open(path, "w") as f:
            json.dump(m, f)
    return m


_BUILD = threading.Lock()


def match_basic(match_id):
    """Match JSON without asking OpenDota for anything (no parse request, no replay salt wait)."""
    for path in (_p("matches", f"{match_id}.json"), _p("matches", "basic", f"{match_id}.json")):
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    m = api(f"matches/{match_id}")
    with open(_p("matches", "basic", f"{match_id}.json"), "w") as f:
        json.dump(m, f)
    return m


def ensure_parser():
    with _BUILD:
        src = os.path.join(PARSER_DIR, "main.go")
        if os.path.exists(PARSER_BIN) and os.path.getmtime(PARSER_BIN) >= os.path.getmtime(src):
            return PARSER_BIN
        os.makedirs(os.path.dirname(PARSER_BIN), exist_ok=True)
        subprocess.run(["go", "build", "-o", PARSER_BIN, "."], cwd=PARSER_DIR, check=True)
        return PARSER_BIN


PARSE_VERSION = 5  # bump when parser/main.go output changes; cached parses are redone from kept replays


def replay(match_id, m=None):
    """Download (and keep, compressed), decompress and parse the .dem; returns the parsed JSON (cached)."""
    out = _p("parsed", f"{match_id}.json")
    if os.path.exists(out):
        with open(out) as f:
            d = json.load(f)
        if d.get("meta", {}).get("version", 1) >= PARSE_VERSION:
            return d
    raw_path = _p("replays", f"{match_id}.dem.zst")
    if os.path.exists(raw_path):
        with open(raw_path, "rb") as f:
            raw = f.read()
    else:
        m = m or match(match_id)
        url = m.get("replay_url")
        if not url and m.get("replay_salt") and m.get("cluster"):
            url = f"http://replay{m['cluster']}.valve.net/570/{match_id}_{m['replay_salt']}.dem.bz2"
        if not url:
            raise RuntimeError(f"no replay url for {match_id} (OpenDota couldn't get the replay salt)")
        try:
            raw = http(url, tries=2, timeout=45)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"replay download failed ({e.code}); Valve keeps replays ~2 weeks") from e
        with open(raw_path + ".part", "wb") as f:
            f.write(raw)
        os.replace(raw_path + ".part", raw_path)
    if raw[:4] == b"\x28\xb5\x2f\xfd":  # zstd: what Valve serves in 2026 despite the .bz2 name
        if not shutil.which("zstd"):
            raise RuntimeError("zstd binary needed: brew install zstd")
        data = subprocess.run(["zstd", "-dc"], input=raw, capture_output=True, check=True).stdout
    elif raw[:3] == b"BZh":
        data = bz2.decompress(raw)
    else:
        data = raw
    dem = _p("tmp", f"{match_id}.{os.getpid()}.dem")
    try:
        with open(dem, "wb") as f:
            f.write(data)
        res = subprocess.run([ensure_parser(), dem], capture_output=True, check=True)
    finally:
        if os.path.exists(dem):
            os.remove(dem)
    err = res.stderr.decode(errors="replace")
    if "parse error" in err:  # manta recovers callback panics; output would be silently truncated
        raise RuntimeError(f"replay {match_id} failed to parse: {err.strip()[-300:]}")
    with open(out, "wb") as f:
        f.write(res.stdout)
    return json.loads(res.stdout)


def latest_match_id():
    return max(r["match_id"] for r in api("publicMatches"))
