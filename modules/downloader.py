import csv
import io
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from tqdm import tqdm

DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
TEMP_DOWNLOAD_DIR = os.path.join(DATA_DIR, ".download")

CSV_META_FILE_NAME = "csv_meta.json"
CSV_META_FILE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", CSV_META_FILE_NAME))

GITHUB_TAG_REPO = "tantinevincent/tagdb-updater"
GITHUB_TAG_BRANCH = "main"
GITHUB_DIST_BASE = f"https://raw.githubusercontent.com/{GITHUB_TAG_REPO}/{GITHUB_TAG_BRANCH}/dist"
OUTPUT_CSV_NAME = "danbooru_tags.csv"
USER_AGENT = "Autocomplete-Plus/1.11"

DEFAULT_CSV_METADATA = {
    "version": 2,
    "check_updates_on_startup": True,
    "github_tag_source": {
        "repo": GITHUB_TAG_REPO,
        "branch": GITHUB_TAG_BRANCH,
        "files": ["danbooru.csv", "danbooru-ja.csv"],
        "output": OUTPUT_CSV_NAME,
        "last_generated_at": None,
        "last_download": None,
        "last_remote_check_timestamp": None,
    },
}


def get_file_path(file_name: str) -> str:
    return os.path.join(DATA_DIR, file_name)


def get_temp_download_path(file_name: str) -> str:
    return os.path.join(TEMP_DOWNLOAD_DIR, file_name)


def check_file_valid(file_path):
    return os.path.exists(file_path) and os.path.getsize(file_path) > 0


def normalize_tag_name(name: str) -> str:
    name = (name or "").strip().replace(" ", "_")
    out = []
    i = 0
    while i < len(name):
        if name[i] == "\\" and i + 1 < len(name):
            out.append(name[i : i + 2])
            i += 2
            continue
        if name[i] in "()":
            out.append("\\" + name[i])
        else:
            out.append(name[i])
        i += 1
    return "".join(out)


def split_aliases(value: str) -> list:
    if not value:
        return []
    parts = []
    for item in csv.reader([value]).__next__():
        text = item.strip()
        if text:
            parts.append(text)
    return parts


def merge_alias_lists(*groups) -> str:
    seen = set()
    ordered = []
    for group in groups:
        for alias in group:
            if alias not in seen:
                seen.add(alias)
                ordered.append(alias)
    return ",".join(ordered)


def parse_tag_rows(text: str, has_japanese_column: bool) -> dict:
    rows = {}
    reader = csv.reader(io.StringIO(text))
    for raw in reader:
        if not raw or not raw[0].strip():
            continue
        if raw[0].strip().lower() in ("tag", "t"):
            continue
        if len(raw) < 3:
            continue

        tag = normalize_tag_name(raw[0])
        if not tag:
            continue

        try:
            category = int(raw[1].strip()) if raw[1].strip() else 0
        except ValueError:
            category = 0

        try:
            count = int(raw[2].strip()) if raw[2].strip() else 0
        except ValueError:
            count = 0

        aliases = split_aliases(raw[3]) if len(raw) > 3 else []
        japanese = split_aliases(raw[4]) if has_japanese_column and len(raw) > 4 else []

        existing = rows.get(tag)
        if existing is None:
            rows[tag] = {
                "tag": tag,
                "category": category,
                "count": count,
                "aliases": aliases,
                "japanese": japanese,
            }
            continue

        existing["count"] = max(existing["count"], count)
        existing["aliases"] = split_aliases(merge_alias_lists(existing["aliases"], aliases))
        existing["japanese"] = split_aliases(merge_alias_lists(existing["japanese"], japanese))
        if existing["category"] == 0 and category:
            existing["category"] = category

    return rows


def merge_tag_sources(english_csv: str, japanese_csv: str) -> list:
    english_rows = parse_tag_rows(english_csv, has_japanese_column=False)
    japanese_rows = parse_tag_rows(japanese_csv, has_japanese_column=True)

    for tag, row in japanese_rows.items():
        existing = english_rows.get(tag)
        if existing is None:
            english_rows[tag] = row
            continue
        existing["count"] = max(existing["count"], row["count"])
        existing["aliases"] = split_aliases(merge_alias_lists(existing["aliases"], row["aliases"]))
        existing["japanese"] = split_aliases(merge_alias_lists(existing["japanese"], row["japanese"]))
        if existing["category"] == 0 and row["category"]:
            existing["category"] = row["category"]

    merged = []
    for row in english_rows.values():
        alias = merge_alias_lists(row["aliases"], row["japanese"])
        merged.append((row["tag"], row["category"], row["count"], alias))

    merged.sort(key=lambda item: (-item[2], item[0]))
    return merged


def write_danbooru_tags_csv(path: str, rows: list) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["tag", "category", "count", "alias"])
        writer.writerows(rows)


class Downloader:
    """Download Danbooru tag CSVs from GitHub and merge them for autocomplete."""

    def __init__(self):
        self.csv_meta_file_exists_at_start = False
        self._ensure_directories_exist()
        self.metadata = self._load_metadata()

    def get_default_csv_metadata(self):
        return json.loads(json.dumps(DEFAULT_CSV_METADATA))

    def _load_metadata(self) -> dict:
        default_metadata = self.get_default_csv_metadata()

        if not os.path.exists(CSV_META_FILE):
            print(f"[Autocomplete-Plus] Metadata file not found: {CSV_META_FILE}. Using default metadata.")
            return default_metadata

        try:
            with open(CSV_META_FILE, "r", encoding="utf-8") as f:
                metadata = json.load(f)

            if not isinstance(metadata, dict) or metadata.get("version") != DEFAULT_CSV_METADATA["version"]:
                print(
                    f"[Autocomplete-Plus] Metadata version mismatch. Expected {DEFAULT_CSV_METADATA['version']}, "
                    f"found {metadata.get('version') if isinstance(metadata, dict) else None}. Using default metadata."
                )
                if isinstance(metadata, dict) and "check_updates_on_startup" in metadata:
                    default_metadata["check_updates_on_startup"] = metadata["check_updates_on_startup"]
                return default_metadata

            self.csv_meta_file_exists_at_start = True
            if "github_tag_source" not in metadata:
                metadata["github_tag_source"] = default_metadata["github_tag_source"]
            return metadata

        except (OSError, json.JSONDecodeError) as e:
            print(f"[Autocomplete-Plus] Error loading metadata from {CSV_META_FILE}: {e}. Using default metadata.")
            return default_metadata

    def _save_metadata(self):
        try:
            os.makedirs(os.path.dirname(CSV_META_FILE), exist_ok=True)
            with open(CSV_META_FILE, "w", encoding="utf-8") as f:
                json.dump(self.metadata, f, indent=2)
        except OSError as e:
            print(f"[Autocomplete-Plus] Error saving metadata to {CSV_META_FILE}: {e}")

    def _github_source(self) -> dict:
        source = self.metadata.setdefault("github_tag_source", self.get_default_csv_metadata()["github_tag_source"])
        return source

    def _dist_url(self, filename: str) -> str:
        return f"{GITHUB_DIST_BASE}/{filename}"

    def _http_get(self, url: str, timeout: int = 30):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def _fetch_remote_generated_at(self) -> str | None:
        url = self._dist_url("meta.json")
        try:
            with self._http_get(url, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            generated_at = payload.get("generated_at")
            if isinstance(generated_at, str) and generated_at.strip():
                return generated_at.strip()
            print(f"[Autocomplete-Plus] 'generated_at' missing in {url}")
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            print(f"[Autocomplete-Plus] Failed to read GitHub tag meta.json: {e}")
            return None

    def _download_text_with_progress(self, filename: str) -> str | None:
        url = self._dist_url(filename)
        temp_path = get_temp_download_path(filename)
        print(f"[Autocomplete-Plus] Attempting to download {filename} from {url}")

        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)

            with self._http_get(url, timeout=120) as response:
                total_size_str = response.getheader("Content-Length")
                total_size = int(total_size_str) if total_size_str else None
                with (
                    open(temp_path, "wb") as f_out,
                    tqdm(
                        total=total_size,
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                        desc=f"[Autocomplete-Plus] Downloading {filename}",
                        leave=False,
                        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]",
                    ) as pbar,
                ):
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        f_out.write(chunk)
                        pbar.update(len(chunk))

            sys.stdout.write("\r" + " " * 100 + "\r")
            sys.stdout.flush()

            with open(temp_path, "r", encoding="utf-8") as handle:
                return handle.read()
        except (urllib.error.URLError, OSError, TimeoutError, UnicodeDecodeError) as e:
            sys.stdout.write("\n")
            sys.stdout.flush()
            print(f"[Autocomplete-Plus] Error downloading {filename}: {e}")
            return None
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _should_refresh(self, remote_generated_at: str | None, force_check: bool) -> str | None:
        output_path = get_file_path(OUTPUT_CSV_NAME)
        source = self._github_source()

        if not check_file_valid(output_path):
            return f"{OUTPUT_CSV_NAME} is missing or empty locally."

        if not self.csv_meta_file_exists_at_start:
            return f"{CSV_META_FILE_NAME} was not found or schema changed. Forcing download."

        if force_check:
            return "Manual CSV update check requested."

        last_generated = source.get("last_generated_at")
        if remote_generated_at and last_generated != remote_generated_at:
            return f"Remote tag dump is newer ({remote_generated_at} vs {last_generated})."

        return None

    def _download_and_merge(self, remote_generated_at: str | None) -> bool:
        english_csv = self._download_text_with_progress("danbooru.csv")
        japanese_csv = self._download_text_with_progress("danbooru-ja.csv")
        if not english_csv or not japanese_csv:
            print("[Autocomplete-Plus] Skipping merge because one or more GitHub CSV downloads failed.")
            return False

        print("[Autocomplete-Plus] Merging danbooru.csv and danbooru-ja.csv into danbooru_tags.csv...")
        rows = merge_tag_sources(english_csv, japanese_csv)
        if not rows:
            print("[Autocomplete-Plus] Merge produced no tag rows.")
            return False

        output_path = get_file_path(OUTPUT_CSV_NAME)
        temp_output = get_temp_download_path(OUTPUT_CSV_NAME)
        try:
            write_danbooru_tags_csv(temp_output, rows)
            shutil.move(temp_output, output_path)
        except OSError as e:
            print(f"[Autocomplete-Plus] Failed to write {OUTPUT_CSV_NAME}: {e}")
            if os.path.exists(temp_output):
                try:
                    os.remove(temp_output)
                except OSError:
                    pass
            return False

        now_utc = datetime.now(timezone.utc).isoformat()
        source = self._github_source()
        source["last_download"] = now_utc
        if remote_generated_at:
            source["last_generated_at"] = remote_generated_at
        print(f"[Autocomplete-Plus] Wrote {len(rows)} tags to {output_path}.")
        return True

    def _ensure_directories_exist(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

    def run_check_and_download(self, force_check: bool = False):
        if not (self.metadata.get("check_updates_on_startup", True) or force_check):
            print('[Autocomplete-Plus] "check_updates_on_startup" is disabled. Skipping CSV update check and download.')
            return

        now_utc = datetime.now(timezone.utc)
        source = self._github_source()
        print(f"[Autocomplete-Plus] Checking GitHub {GITHUB_TAG_REPO} for tag CSV updates...")

        remote_generated_at = self._fetch_remote_generated_at()
        source["last_remote_check_timestamp"] = now_utc.isoformat()

        reason = self._should_refresh(remote_generated_at, force_check)
        if reason:
            print(f"[Autocomplete-Plus] Queuing tag CSV refresh: {reason}")
            self._download_and_merge(remote_generated_at)
        else:
            print("[Autocomplete-Plus] Local danbooru_tags.csv is up to date.")

        self._save_metadata()
