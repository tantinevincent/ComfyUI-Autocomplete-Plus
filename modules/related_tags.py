import hashlib
import json
import os
import time

from aiohttp import ClientSession, ClientTimeout

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
RELATED_TAGS_CACHE_DIR = os.path.join(DATA_DIR, "related-tags")

DANBOORU_RELATED_TAGS_URL = "https://danbooru.donmai.us/related_tag.json"
USER_AGENT = "ComfyUI-Autocomplete-Plus"
CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000
CACHE_FETCH_LIMIT = 100
REQUEST_TIMEOUT_SECONDS = 10


def _cache_path(query: str) -> str:
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return os.path.join(RELATED_TAGS_CACHE_DIR, f"{digest}.json")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_cache_dir() -> None:
    os.makedirs(RELATED_TAGS_CACHE_DIR, exist_ok=True)


def _read_cache(query: str):
    path = _cache_path(query)
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


def _write_cache(query: str, tags: list) -> None:
    _ensure_cache_dir()
    path = _cache_path(query)
    payload = {
        "query": query,
        "fetchedAt": _now_ms(),
        "tags": tags,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except OSError as e:
        print(f"[Autocomplete-Plus] Failed to write related tags cache for '{query}': {e}")


def _similarity_from_item(item: dict) -> float:
    for key in ("cosine_similarity", "jaccard_similarity", "frequency"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def normalize_related_tags_payload(payload) -> list:
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
            similarity = _similarity_from_item(item)
        elif isinstance(tag_obj, str):
            name = tag_obj
            category = item.get("category", 0)
            count = item.get("post_count", item.get("count", 0))
            similarity = _similarity_from_item(item)
        else:
            name = item.get("name")
            category = item.get("category", 0)
            count = item.get("post_count", item.get("count", 0))
            similarity = _similarity_from_item(item)

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


async def fetch_related_tags_from_danbooru(query: str) -> list:
    timeout = ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    params = {"query": query, "limit": CACHE_FETCH_LIMIT}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    async with ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(DANBOORU_RELATED_TAGS_URL, params=params) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)

    return normalize_related_tags_payload(payload)


async def get_related_tags(query: str, limit: int) -> list:
    cached = _read_cache(query)
    if cached is not None:
        return cached[:limit]

    tags = await fetch_related_tags_from_danbooru(query)
    _write_cache(query, tags)
    return tags[:limit]
