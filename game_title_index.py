"""Incremental RJ-based bilingual title index for the game-search domain."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from urllib.parse import quote

RJ_RE = re.compile(r"(?<![A-Z0-9])RJ\d{4,8}(?!\d)", re.I)
CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
PRODUCT_INFO_URL = "https://www.dlsite.com/maniax/product/info/ajax"
SEARCH_URL = "https://www.dlsite.com/maniax/fsr/=/keyword/{query}/search/1/"


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def extract_rj_codes(*values: str) -> list[str]:
    found = []
    seen = set()
    for value in values:
        for match in RJ_RE.finditer(str(value or "")):
            code = match.group(0).upper()
            if code not in seen:
                found.append(code)
                seen.add(code)
    return found


def parse_dlsite_candidates(html: str, query: str, limit: int = 5) -> list[dict]:
    """Return only RJ search cards whose displayed title contains the query."""
    from bs4 import BeautifulSoup

    wanted = normalize_title(query)
    if not wanted:
        return []
    soup = BeautifulSoup(html or "", "html.parser")
    results = []
    seen = set()
    for card in soup.select("li[data-list_item_product_id]"):
        code = str(card.get("data-list_item_product_id") or "").upper()
        if not re.fullmatch(r"RJ\d{4,8}", code) or code in seen:
            continue
        title_node = card.select_one("thumb-with-ng-filter-block[alt]")
        title = str(title_node.get("alt") or "").strip() if title_node else ""
        if not title:
            title_node = card.select_one(".work_name, .work_name a, h3, h2")
            title = title_node.get_text(" ", strip=True) if title_node else ""
        if wanted not in normalize_title(title):
            continue
        results.append({"rj": code, "title": title})
        seen.add(code)
        if len(results) >= limit:
            break
    return results


class GameTitleIndex:
    def __init__(self, db_path: str | Path | None = None):
        configured = db_path or os.environ.get("GAME_TITLE_INDEX_DB")
        self.db_path = Path(configured) if configured else Path(__file__).with_name("game_title_index.sqlite3")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=8)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self):
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS aliases (
                canonical_rj TEXT NOT NULL,
                alias TEXT NOT NULL,
                alias_norm TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'dlsite',
                updated_at REAL NOT NULL,
                PRIMARY KEY(canonical_rj, alias_norm, kind)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS aliases_norm_idx ON aliases(alias_norm)")
            conn.execute("""CREATE TABLE IF NOT EXISTS query_cache (
                query_norm TEXT PRIMARY KEY,
                terms_json TEXT NOT NULL,
                checked_at REAL NOT NULL
            )""")

    def add_alias(self, canonical_rj: str, alias: str, language: str = "", kind: str = "dlsite"):
        code = str(canonical_rj or "").upper()
        text = str(alias or "").strip()
        norm = normalize_title(text)
        if not re.fullmatch(r"RJ\d{4,8}", code) or not norm:
            return
        with self._db() as conn:
            conn.execute("""INSERT INTO aliases(canonical_rj,alias,alias_norm,language,kind,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(canonical_rj,alias_norm,kind) DO UPDATE SET
                alias=excluded.alias, language=excluded.language, updated_at=excluded.updated_at""",
                (code, text, norm, str(language or "").upper(), kind, time.time()))

    def _cached_terms(self, query_norm: str) -> list[str] | None:
        if not query_norm:
            return None
        with self._db() as conn:
            row = conn.execute("SELECT terms_json,checked_at FROM query_cache WHERE query_norm=?", (query_norm,)).fetchone()
        if not row:
            return None
        ttl = 86400 if not json.loads(row["terms_json"]) else 1209600
        if time.time() - row["checked_at"] > ttl:
            return None
        return json.loads(row["terms_json"])

    def _save_query_cache(self, query_norm: str, terms: list[str]):
        if not query_norm:
            return
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO query_cache(query_norm,terms_json,checked_at) VALUES(?,?,?)",
                         (query_norm, json.dumps(terms, ensure_ascii=False), time.time()))

    def search_terms(self, query: str, limit: int = 3) -> list[str]:
        raw = str(query or "").strip()
        norm = normalize_title(raw)
        if not norm:
            return [raw] if raw else []
        with self._db() as conn:
            matches = conn.execute("SELECT DISTINCT canonical_rj FROM aliases WHERE alias_norm=?", (norm,)).fetchall()
            codes = [r["canonical_rj"] for r in matches]
            rows = []
            if codes:
                marks = ",".join("?" for _ in codes)
                rows = conn.execute(f"SELECT alias,alias_norm,language,kind FROM aliases WHERE canonical_rj IN ({marks})", codes).fetchall()
        if not codes:
            cached = self._cached_terms(norm)
            return (cached + [raw]) if cached else [raw]

        # The SoSo sites are English-oriented. Prefer their known title, then a
        # DLsite English edition; never translate or guess a missing title.
        rows = sorted(rows, key=lambda r: (0 if r["kind"] == "source" else
                                          1 if r["language"] == "ENG" else 2,
                                          r["alias_norm"]))
        terms, seen = [], set()
        for row in rows:
            term, term_norm = row["alias"].strip(), row["alias_norm"]
            if not term_norm or term_norm == norm or term_norm in seen:
                continue
            if row["kind"] != "source" and row["language"] != "ENG":
                continue
            terms.append(term)
            seen.add(term_norm)
            if len(terms) >= limit:
                break
        if raw:
            terms.append(raw)
        return terms or [raw]

    def resolve_search_terms(self, query: str) -> list[str]:
        raw = str(query or "").strip()
        terms = self.search_terms(raw)
        if len(terms) > 1 or not CJK_RE.search(raw):
            return terms
        norm = normalize_title(raw)
        if self._cached_terms(norm) is not None:
            return terms
        self._resolve_cjk_query(raw)
        return self.search_terms(raw)

    @staticmethod
    def _fetch_products(codes: list[str]) -> dict:
        codes = list(dict.fromkeys(str(x).upper() for x in codes if re.fullmatch(r"RJ\d{4,8}", str(x).upper())))[:24]
        if not codes:
            return {}
        from curl_cffi import requests
        response = requests.get(PRODUCT_INFO_URL, params={"product_id": ",".join(codes)},
            impersonate="chrome", timeout=8,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://www.dlsite.com/maniax/"})
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _fetch_search_html(query: str) -> str:
        from curl_cffi import requests
        url = SEARCH_URL.format(query=quote(query, safe=""))
        response = requests.get(url, impersonate="chrome", timeout=8,
                                headers={"Referer": "https://www.dlsite.com/maniax/"})
        response.raise_for_status()
        return response.text

    @staticmethod
    def _related_codes(records: dict) -> list[str]:
        related = []
        for record in records.values():
            info = record.get("translation_info") or {}
            related.extend([info.get("original_workno"), info.get("parent_workno")])
            children = info.get("child_worknos") or []
            if isinstance(children, list):
                related.extend(children)
        return list(dict.fromkeys(str(x).upper() for x in related if re.fullmatch(r"RJ\d{4,8}", str(x).upper())))[:24]

    def add_product_records(self, records: dict, source_aliases: dict | None = None):
        """Store DLsite titles under their original-work RJ, plus source titles."""
        if not isinstance(records, dict):
            return
        parent_of = {}
        for code, record in records.items():
            code = str(code).upper()
            if not isinstance(record, dict):
                continue
            info = record.get("translation_info") or {}
            parent = info.get("original_workno") or info.get("parent_workno")
            if parent and re.fullmatch(r"RJ\d{4,8}", str(parent).upper()):
                parent_of[code] = str(parent).upper()
            for child in info.get("child_worknos") or []:
                if re.fullmatch(r"RJ\d{4,8}", str(child).upper()):
                    parent_of[str(child).upper()] = code

        def canonical(code):
            seen = set()
            code = str(code).upper()
            while code in parent_of and code not in seen:
                seen.add(code)
                code = parent_of[code]
            return code

        for code, record in records.items():
            code = str(code).upper()
            if not isinstance(record, dict) or not re.fullmatch(r"RJ\d{4,8}", code):
                continue
            root = canonical(code)
            info = record.get("translation_info") or {}
            language = str(info.get("lang") or "").upper()
            for field in ("work_name", "work_name_en", "work_name_ja", "title_name", "title_en"):
                title = record.get(field)
                if isinstance(title, str) and title.strip() and title.strip().lower() != "none":
                    self.add_alias(root, title, language, "dlsite")
            for title in (source_aliases or {}).get(code, []):
                self.add_alias(root, title, "ENG", "source")

    def index_rj_codes(self, codes: list[str], source_aliases: dict | None = None):
        base = self._fetch_products(codes)
        if not base:
            return
        related = self._related_codes(base)
        missing = [code for code in related if code not in base]
        extra = self._fetch_products(missing)
        self.add_product_records({**base, **extra}, source_aliases)

    def learn_from_results(self, results: list[dict]):
        codes, aliases = [], {}
        for item in results or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            found = extract_rj_codes(title, item.get("thumb", ""), item.get("url", ""))
            for code in found:
                codes.append(code)
                if title:
                    aliases.setdefault(code, []).append(title)
        if codes:
            try:
                self.index_rj_codes(list(dict.fromkeys(codes))[:8], aliases)
            except Exception:
                # Index enrichment is best-effort; it must not break normal search.
                return

    def _resolve_cjk_query(self, query: str):
        try:
            candidates = parse_dlsite_candidates(self._fetch_search_html(query), query, limit=4)
            codes = [x["rj"] for x in candidates]
            if not codes:
                self._save_query_cache(normalize_title(query), [])
                return
            records = self._fetch_products(codes)
            related = self._related_codes(records)
            records.update(self._fetch_products([x for x in related if x not in records]))
            self.add_product_records(records)
            english_terms = []
            canonical_codes = list(dict.fromkeys(self._canonical_from_records(c, records) for c in codes))
            with self._db() as conn:
                for root in canonical_codes:
                    rows = conn.execute("SELECT alias,alias_norm,language,kind FROM aliases WHERE canonical_rj=?", (root,)).fetchall()
                    for row in rows:
                        if row["kind"] == "source" or row["language"] == "ENG":
                            english_terms.append(row["alias"])
            # Only bind a Chinese query when RJ metadata establishes an English/source title.
            english_terms = list(dict.fromkeys(x for x in english_terms if normalize_title(x) != normalize_title(query)))[:3]
            if english_terms:
                for root in canonical_codes:
                    self.add_alias(root, query, "QUERY", "query")
            self._save_query_cache(normalize_title(query), english_terms)
        except Exception:
            # A transient network/WAF failure is not a genuine no-match; do not cache it.
            return

    @staticmethod
    def _canonical_from_records(code: str, records: dict) -> str:
        parent_of = {}
        for item_code, record in records.items():
            if not isinstance(record, dict):
                continue
            info = record.get("translation_info") or {}
            parent = info.get("original_workno") or info.get("parent_workno")
            if parent:
                parent_of[str(item_code).upper()] = str(parent).upper()
            for child in info.get("child_worknos") or []:
                parent_of[str(child).upper()] = str(item_code).upper()
        seen = set()
        current = str(code).upper()
        while current in parent_of and current not in seen:
            seen.add(current)
            current = parent_of[current]
        return current


_DEFAULT_INDEX = None
_DEFAULT_LOCK = Lock()

def get_index() -> GameTitleIndex:
    global _DEFAULT_INDEX
    if _DEFAULT_INDEX is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_INDEX is None:
                _DEFAULT_INDEX = GameTitleIndex()
    return _DEFAULT_INDEX
