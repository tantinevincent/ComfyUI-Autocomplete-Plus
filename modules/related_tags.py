import hashlib
import json
import os
import time

from aiohttp import ClientError, ClientSession, ClientTimeout

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
RELATED_TAGS_CACHE_DIR = os.path.join(DATA_DIR, "related-tags")

DANBOORU_RELATED_TAGS_URL = "https://danbooru.donmai.us/related_tag.json"
# Danbooru/Cloudflare expect a Name/Version User-Agent.
USER_AGENT = "Autocomplete-Plus/1.11"
CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000
CACHE_FETCH_LIMIT = 100
REQUEST_TIMEOUT_SECONDS = 10
DEFAULT_ORDER = "jaccard"
ALLOWED_ORDERS = ("jaccard", "cosine", "frequency", "overlap")
ALLOWED_CATEGORIES = ("general", "artist", "copyright", "character", "meta")
SIMILARITY_KEYS = {
    "jaccard": "jaccard_similarity",
    "cosine": "cosine_similarity",
    "frequency": "frequency",
    "overlap": "overlap_coefficient",
}

_session = None


class DanbooruHttpError(Exception):
    def __init__(self, status, message=""):
        self.status = status
        super().__init__(message or f"Danbooru returned HTTP {status}")


async def _get_session():
    global _session
    if _session is None or _session.closed:
        _session = ClientSession(
            timeout=ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            trust_env=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
    return _session


def normalize_order(order) -> str:
    value = str(order or "").strip().lower()
    return value if value in ALLOWED_ORDERS else DEFAULT_ORDER


def normalize_category(category) -> str:
    value = str(category or "").strip().lower()
    return value if value in ALLOWED_CATEGORIES else ""


def _cache_path(query: str, category: str = "", order: str = DEFAULT_ORDER) -> str:
    key = f"{query}\0{category}\0{order}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(RELATED_TAGS_CACHE_DIR, f"{digest}.json")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_cache_dir() -> None:
    os.makedirs(RELATED_TAGS_CACHE_DIR, exist_ok=True)


def _read_cache(query: str, category: str = "", order: str = DEFAULT_ORDER):
    path = _cache_path(query, category, order)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[Autocomplete-Plus] Failed to read related tags cache for '{query}': {e}")
        return None

    fetched_at = payload.get("fetchedAt")
    tags = payload.get("tags")
    if not isinstance(fetched_at, (int, float)) or not isinstance(tags, list):
        return None

    if _now_ms() - int(fetched_at) >= CACHE_TTL_MS:
        try:
            os.remove(path)
        except OSError:
            pass
        return None

    return tags


def _write_cache(query: str, tags: list, category: str = "", order: str = DEFAULT_ORDER) -> None:
    _ensure_cache_dir()
    path = _cache_path(query, category, order)
    payload = {
        "query": query,
        "category": category,
        "order": order,
        "fetchedAt": _now_ms(),
        "tags": tags,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError as e:
        print(f"[Autocomplete-Plus] Failed to write related tags cache for '{query}': {e}")


def _similarity_from_item(item: dict, order: str = DEFAULT_ORDER) -> float:
    preferred = SIMILARITY_KEYS.get(order, "jaccard_similarity")
    keys = (preferred, "jaccard_similarity", "cosine_similarity", "overlap_coefficient", "frequency")
    seen = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def normalize_related_tags_payload(payload, order: str = DEFAULT_ORDER) -> list:
    """Parse Danbooru related_tag.json into a list of {tag, category, count, similarity}."""
    related = None
    if isinstance(payload, dict):
        related = payload.get("related_tags", payload.get("tags"))
    elif isinstance(payload, list):
        related = payload

    if related is None:
        return []

    results = []

    if isinstance(related, dict):
        for name, score in related.items():
            if not name:
                continue
            similarity = float(score) if isinstance(score, (int, float)) else 0.0
            results.append({"tag": str(name), "category": 0, "count": 0, "similarity": similarity})
        return results

    if not isinstance(related, list):
        return []

    for item in related:
        if isinstance(item, str):
            results.append({"tag": item, "category": 0, "count": 0, "similarity": 0.0})
            continue

        if not isinstance(item, dict):
            continue

        tag_obj = item.get("tag")
        if isinstance(tag_obj, dict):
            name = tag_obj.get("name")
            category = tag_obj.get("category", 0)
            count = tag_obj.get("post_count", tag_obj.get("count", 0))
            similarity = _similarity_from_item(item, order)
        elif isinstance(tag_obj, str):
            name = tag_obj
            category = item.get("category", 0)
            count = item.get("post_count", item.get("count", 0))
            similarity = _similarity_from_item(item, order)
        else:
            name = item.get("name")
            category = item.get("category", 0)
            count = item.get("post_count", item.get("count", 0))
            similarity = _similarity_from_item(item, order)

        if not name:
            continue

        try:
            category = int(category) if category is not None else 0
        except (TypeError, ValueError):
            category = 0

        try:
            count = int(count) if count is not None else 0
        except (TypeError, ValueError):
            count = 0

        results.append(
            {
                "tag": str(name),
                "category": category,
                "count": count,
                "similarity": similarity,
            }
        )

    return results


async def fetch_related_tags_from_danbooru(query: str, category: str = "", order: str = DEFAULT_ORDER) -> list:
    params = {
        "query": query,
        "order": order,
        "limit": str(CACHE_FETCH_LIMIT),
    }
    if category:
        params["category"] = category
    session = await _get_session()
    try:
        async with session.get(DANBOORU_RELATED_TAGS_URL, params=params) as response:
            # Drain the body before raising so SSL shutdown does not leave
            # APPLICATION_DATA_AFTER_CLOSE_NOTIFY as an unretrieved future.
            if response.status != 200:
                await response.read()
                raise DanbooruHttpError(response.status)
            payload = await response.json(content_type=None)
    except DanbooruHttpError:
        raise
    except (ClientError, TimeoutError) as error:
        raise DanbooruHttpError(0, str(error)) from error

    return normalize_related_tags_payload(payload, order)


async def get_related_tags(query: str, limit: int, category: str = "", order: str = DEFAULT_ORDER) -> list:
    category = normalize_category(category)
    order = normalize_order(order)
    cached = _read_cache(query, category, order)
    if cached is not None:
        return cached[:limit]

    tags = await fetch_related_tags_from_danbooru(query, category, order)
    _write_cache(query, tags, category, order)
    return tags[:limit]
