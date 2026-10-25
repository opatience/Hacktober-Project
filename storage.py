"""Scoped application persistence; one process serializes every mutation."""

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from snowflake_client import LOCK, SnowflakeUnavailable, configured, get_client


ZONE = "America/Indiana/Indianapolis"
JSON_FIELDS = {"interest_tag_ids", "excluded_tag_ids", "media_urls", "raw_payload", "extracted_payload", "evidence"}
BOOL_FIELDS = {"is_active", "is_verified", "is_primary", "followed_only", "free_only", "is_free"}
TAG_SEED = [
    ("artificial-intelligence", "Artificial intelligence", "topic"),
    ("robotics", "Robotics", "topic"), ("arts", "Arts", "topic"),
    ("sports", "Sports", "topic"), ("volunteering", "Volunteering", "topic"),
    ("workshop", "Workshop", "activity"), ("social", "Social", "activity"),
    ("career", "Career", "activity"), ("beginner-friendly", "Beginner friendly", "audience"),
    ("virtual", "Virtual", "format"),
]
CLUB_SEED = [("purduehackers", "Purdue Hackers"), ("purdueusb", "CS Undergraduate Student Board"), ("buildpurdue", "Build Purdue"), ("helloworldpurdue", "Hello World Purdue")]


class StoreError(RuntimeError):
    pass


class ValidationError(ValueError):
    pass


class VersionConflict(ValidationError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _uuid():
    return str(uuid.uuid4())


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_username(value):
    value = str(value).strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", value):
        raise ValidationError("Use an Instagram handle containing letters, numbers, periods, or underscores.")
    return value


def _normal(row):
    result = {}
    for key, value in row.items():
        key = key.lower()
        if key in JSON_FIELDS and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = [] if key.endswith("ids") or key == "media_urls" else {}
        if key in BOOL_FIELDS and value is not None:
            value = bool(value)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = float(value)
        result[key] = value
    return result


class Store:
    def __init__(self):
        backend = os.getenv("STORAGE_BACKEND", "").lower()
        if backend not in {"", "sqlite", "snowflake"}:
            raise StoreError("STORAGE_BACKEND must be sqlite or snowflake.")
        self.backend = backend or ("snowflake" if configured() else "sqlite")
        self.connection = None
        if self.backend == "sqlite":
            path = os.getenv("LOCAL_DATABASE_PATH", str(Path(__file__).parent / ".local" / "feed.sqlite3"))
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self._initialize_local()

    def _initialize_local(self):
        # The checked-in provisioning contract is the single column definition source.
        ddl = (Path(__file__).parent / "snowflake" / "002_schema.sql").read_text()
        statements = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\) DATA_RETENTION_TIME_IN_DAYS = 1(?: CHANGE_TRACKING = TRUE)?;", ddl, re.S)
        if len(statements) != 11:
            raise StoreError("The local database needs the complete 11-table schema file.")
        with LOCK, self.connection:
            for table, body in statements:
                body = body.replace("DEFAULT ARRAY_CONSTRUCT()", "DEFAULT '[]'")
                body = body.replace("DEFAULT CURRENT_TIMESTAMP()", "DEFAULT CURRENT_TIMESTAMP")
                self.connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ({body})")
            for tag_id, label, category in TAG_SEED:
                self.connection.execute("INSERT OR IGNORE INTO TAGS (TAG_ID, SLUG, LABEL, CATEGORY) VALUES (?, ?, ?, ?)", (tag_id, tag_id, label, category))
            for username, name in CLUB_SEED:
                self.connection.execute("INSERT OR IGNORE INTO CLUBS (club_id,slug,name) VALUES (?,?,?)", ("club-" + username, username, name))
                self.connection.execute("INSERT OR IGNORE INTO INSTAGRAM_ACCOUNTS (account_id,username,club_id,display_name,profile_url) VALUES (?,?,?,?,?)", ("account-" + username, username, "club-" + username, name, f"https://www.instagram.com/{username}/"))
                self.connection.execute("INSERT OR IGNORE INTO CLUB_TAGS (club_id,tag_id,source) VALUES (?,?,'manual')", ("club-" + username, "workshop"))

    @contextmanager
    def _unit(self, write=False, ingest=False):
        with LOCK:
            if self.backend == "sqlite":
                try:
                    yield self.connection
                    if write:
                        self.connection.commit()
                except Exception:
                    if write:
                        self.connection.rollback()
                    raise
            else:
                role = os.getenv("SNOWFLAKE_INGEST_ROLE", "HACKATHON_INGEST") if ingest else None
                try:
                    with get_client().session(transaction=write, role=role) as connection:
                        yield connection
                except (ValidationError, VersionConflict):
                    raise
                except Exception as exc:
                    raise StoreError("Snowflake could not complete this request. Check connection, provisioning, and runtime grants.") from exc

    def _query(self, connection, sql, params=()):
        cursor = connection.cursor()
        try:
            cursor.execute(sql.replace("?", "%s") if self.backend == "snowflake" else sql, tuple(params))
            if not cursor.description:
                return []
            names = [column[0].lower() for column in cursor.description]
            return [_normal(dict(zip(names, row))) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _insert(self, connection, table, values):
        fields = list(values)
        params = [json.dumps(values[field], ensure_ascii=False) if field in JSON_FIELDS and values[field] is not None else values[field] for field in fields]
        markers = ["PARSE_JSON(?)" if self.backend == "snowflake" and field in JSON_FIELDS else "?" for field in fields]
        source = "SELECT " + ",".join(markers) if self.backend == "snowflake" else "VALUES (" + ",".join(markers) + ")"
        return self._query(connection, f"INSERT INTO {table} ({','.join(fields)}) {source}", params)

    def _update(self, connection, table, values, where, params=()):
        assignments = [field + ("=PARSE_JSON(?)" if self.backend == "snowflake" and field in JSON_FIELDS else "=?") for field in values]
        bound = [json.dumps(value, ensure_ascii=False) if field in JSON_FIELDS and value is not None else value for field, value in values.items()]
        return self._query(connection, f"UPDATE {table} SET {','.join(assignments)} WHERE {where}", bound + list(params))

    def create_account(self, email, password_hash):
        """Email/password sign-up; creates USERS + default USER_PREFERENCES together. None if the email is taken."""
        with self._unit(write=True) as connection:
            if self._query(connection, "SELECT user_id FROM USERS WHERE email=?", (email,)):
                return None
            user = {"user_id": _uuid(), "auth_subject": "local:" + email, "email": email, "password_hash": password_hash, "display_name": email.split("@")[0], "timezone": ZONE, "created_at": _now(), "updated_at": _now()}
            self._insert(connection, "USERS", user)
            self._insert(connection, "USER_PREFERENCES", {"user_id": user["user_id"], "preference_text": "", "interest_tag_ids": [], "excluded_tag_ids": [], "followed_only": False, "free_only": False, "horizon_days": 30, "version": 1, "updated_at": _now()})
            return user

    def user_by_email(self, email):
        with self._unit() as connection:
            rows = self._query(connection, "SELECT * FROM USERS WHERE email=? AND deleted_at IS NULL", (email,))
            return rows[0] if rows else None

    def user_by_id(self, user_id):
        with self._unit() as connection:
            rows = self._query(connection, "SELECT user_id,email,display_name,timezone FROM USERS WHERE user_id=? AND deleted_at IS NULL", (user_id,))
            return rows[0] if rows else None

    def current_user(self, subject, display_name="Student"):
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise ValidationError("A valid server session is required.")
        with self._unit(write=True) as connection:
            rows = self._query(connection, "SELECT * FROM USERS WHERE auth_subject=?", (subject,))
            if rows:
                if rows[0].get("deleted_at"):
                    raise ValidationError("This profile is no longer active.")
                return rows[0]
            user = {"user_id": _uuid(), "auth_subject": subject, "display_name": display_name, "timezone": ZONE, "created_at": _now(), "updated_at": _now()}
            self._insert(connection, "USERS", user)
            self._insert(connection, "USER_PREFERENCES", {"user_id": user["user_id"], "preference_text": "", "interest_tag_ids": [], "excluded_tag_ids": [], "followed_only": False, "free_only": False, "horizon_days": 30, "version": 1, "updated_at": _now()})
            return user

    def preferences(self, user_id):
        with self._unit() as connection:
            rows = self._query(connection, "SELECT * FROM USER_PREFERENCES WHERE user_id=?", (user_id,))
            if not rows:
                raise ValidationError("Profile not found.")
            return rows[0]

    def save_preferences(self, user_id, data):
        text = data.get("preference_text", "")
        if not isinstance(text, str) or len(text) > 2000:
            raise ValidationError("Preferences must be at most 2,000 characters.")
        known = {tag["tag_id"] for tag in self.tags()}
        arrays = {}
        for field in ("interest_tag_ids", "excluded_tag_ids"):
            values = data.get(field, [])
            if not isinstance(values, list) or any(not isinstance(value, str) or value not in known for value in values):
                raise ValidationError("Select tags from the available interests.")
            arrays[field] = list(dict.fromkeys(values))
        if set(arrays["interest_tag_ids"]) & set(arrays["excluded_tag_ids"]):
            raise ValidationError("A tag cannot be both included and excluded.")
        for field in ("followed_only", "free_only"):
            if not isinstance(data.get(field, False), bool):
                raise ValidationError("Preference switches must be true or false.")
        horizon = data.get("horizon_days", 30)
        expected = data.get("expected_version")
        if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 90:
            raise ValidationError("Choose a horizon between 1 and 90 days.")
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise ValidationError("An expected preference version is required.")
        with self._unit(write=True) as connection:
            rows = self._query(connection, "SELECT * FROM USER_PREFERENCES WHERE user_id=?", (user_id,))
            if not rows:
                raise ValidationError("Profile not found.")
            if rows[0]["version"] != expected:
                raise VersionConflict("Preferences changed in another tab. Reload before saving.")
            values = {"preference_text": text, **arrays, "followed_only": data.get("followed_only", False), "free_only": data.get("free_only", False), "horizon_days": horizon, "version": expected + 1, "updated_at": _now()}
            self._update(connection, "USER_PREFERENCES", values, "user_id=?", (user_id,))
            return {"user_id": user_id, **values}

    def tags(self):
        with self._unit() as connection:
            return self._query(connection, "SELECT tag_id,slug,label,category FROM TAGS WHERE is_active=TRUE ORDER BY category,label")

    def clubs(self):
        with self._unit() as connection:
            clubs = self._query(connection, "SELECT * FROM CLUBS WHERE is_active=TRUE ORDER BY name")
            accounts = self._query(connection, "SELECT account_id,username,club_id,profile_url,ingestion_status,last_success_at FROM INSTAGRAM_ACCOUNTS WHERE is_active=TRUE")
            tags = self._query(connection, "SELECT ct.club_id,t.tag_id,t.slug,t.label,t.category FROM CLUB_TAGS ct JOIN TAGS t ON ct.tag_id=t.tag_id WHERE t.is_active=TRUE")
            for club in clubs:
                club["accounts"] = [account for account in accounts if account["club_id"] == club["club_id"]]
                club["tags"] = [tag for tag in tags if tag["club_id"] == club["club_id"]]
            return clubs

    def follows(self, user_id):
        with self._unit() as connection:
            return self._query(connection, "SELECT a.* FROM USER_FOLLOWS f JOIN INSTAGRAM_ACCOUNTS a ON a.account_id=f.account_id WHERE f.user_id=? ORDER BY a.username", (user_id,))

    def _account(self, connection, username):
        rows = self._query(connection, "SELECT * FROM INSTAGRAM_ACCOUNTS WHERE username=?", (username,))
        if rows:
            return rows[0]
        account = {"account_id": _uuid(), "username": username, "profile_url": f"https://www.instagram.com/{username}/", "is_active": True, "ingestion_status": "pending", "created_at": _now(), "updated_at": _now()}
        self._insert(connection, "INSTAGRAM_ACCOUNTS", account)
        return account

    def follow(self, user_id, username):
        username = normalize_username(username)
        with self._unit(write=True) as connection:
            account = self._account(connection, username)
            rows = self._query(connection, "SELECT user_id FROM USER_FOLLOWS WHERE user_id=? AND account_id=?", (user_id, account["account_id"]))
            if not rows:
                self._insert(connection, "USER_FOLLOWS", {"user_id": user_id, "account_id": account["account_id"], "created_at": _now()})
            return {**account, "created": not bool(rows)}

    def unfollow(self, user_id, account_id):
        with self._unit(write=True) as connection:
            self._query(connection, "DELETE FROM USER_FOLLOWS WHERE user_id=? AND account_id=?", (user_id, account_id))

    def posts(self, user_id, days=30):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._unit() as connection:
            rows = self._query(connection, "SELECT p.*,a.username,a.club_id FROM POSTS p JOIN INSTAGRAM_ACCOUNTS a ON a.account_id=p.account_id JOIN USER_FOLLOWS f ON f.account_id=p.account_id WHERE f.user_id=? AND p.deleted_at IS NULL AND p.posted_at>=? ORDER BY p.posted_at DESC,p.post_id", (user_id, cutoff))
            for row in rows:
                row["account"] = {"account_id": row["account_id"], "username": row["username"], "club_id": row.get("club_id")}
                payload = row.get("extracted_payload") or {}
                row["summary"] = payload.get("summary")
                row["timestamp"] = row["posted_at"]
                row["url"] = row["permalink"]
                row["image"] = (row.get("media_urls") or [None])[0]
                row["source_mode"] = (row.get("raw_payload") or {}).get("source_mode", "live")
            return rows

    def events(self):
        with self._unit() as connection:
            events = self._query(connection, "SELECT e.*,c.name AS club_name,c.is_active AS club_active FROM EVENTS e JOIN CLUBS c ON c.club_id=e.club_id WHERE e.status IN ('published','cancelled') AND c.is_active=TRUE ORDER BY e.start_at,e.event_id")
            links = self._query(connection, "SELECT s.*,p.permalink,p.account_id,a.username,p.raw_payload FROM EVENT_SOURCES s JOIN POSTS p ON p.post_id=s.post_id JOIN INSTAGRAM_ACCOUNTS a ON a.account_id=p.account_id WHERE p.deleted_at IS NULL AND s.source_content_hash=p.content_hash")
            event_tags = self._query(connection, "SELECT et.event_id,t.tag_id,t.slug,t.label,t.category FROM EVENT_TAGS et JOIN TAGS t ON t.tag_id=et.tag_id WHERE t.is_active=TRUE")
            club_tags = self._query(connection, "SELECT ct.club_id,t.tag_id,t.slug,t.label,t.category FROM CLUB_TAGS ct JOIN TAGS t ON t.tag_id=ct.tag_id WHERE t.is_active=TRUE")
            result = []
            for event in events:
                sources = [link for link in links if link["event_id"] == event["event_id"]]
                if not any(source["is_primary"] for source in sources):
                    continue
                event["club"] = {"club_id": event["club_id"], "name": event["club_name"]}
                event["source_posts"] = [{"post_id": source["post_id"], "url": source["permalink"], "is_primary": source["is_primary"], "account_id": source["account_id"], "username": source["username"]} for source in sources]
                event["tags"] = [{**tag, "inherited": False} for tag in event_tags if tag["event_id"] == event["event_id"]]
                seen = {tag["tag_id"] for tag in event["tags"]}
                event["tags"].extend({**tag, "inherited": True} for tag in club_tags if tag["club_id"] == event["club_id"] and tag["tag_id"] not in seen)
                event["tag_ids"] = [tag["tag_id"] for tag in event["tags"]]
                primary = next(source for source in sources if source["is_primary"])
                event["account_id"] = primary["account_id"]
                event["username"] = primary["username"]
                modes = {(source.get("raw_payload") or {}).get("source_mode", "live") for source in sources}
                event["source_mode"] = next(iter(modes)) if len(modes) == 1 else "mixed"
                result.append(event)
            return result

    def ingest_posts(self, posts, source_mode="live"):
        if not isinstance(posts, list) or len(posts) > 100:
            raise ValidationError("Ingest at most 100 posts at a time.")
        report = {"inserted": 0, "updated": 0, "unchanged": 0, "events": 0, "warnings": []}
        # Account registration uses the API role; all shared-content writes use INGEST.
        with self._unit(write=True) as connection:
            for post in posts:
                self._account(connection, normalize_username(post.get("username", "")))
        with self._unit(write=True, ingest=True) as connection:
            accounts = {row["username"]: row for row in self._query(connection, "SELECT * FROM INSTAGRAM_ACCOUNTS")}
            clubs = {row["club_id"]: row for row in self._query(connection, "SELECT * FROM CLUBS")}
            for post in posts:
                account = accounts[normalize_username(post["username"])]
                url = post.get("permalink") or post.get("url") or account["profile_url"]
                match = re.search(r"/(?:p|reel)/([^/?#]+)", url)
                shortcode = post.get("shortcode") or (match.group(1) if match else "local_" + _hash(account["account_id"] + str(post.get("timestamp")) + post.get("caption", ""))[:40])
                post_id = _hash("instagram:post:" + shortcode)
                caption = post.get("caption") or ""
                media = post.get("media_urls") or ([post["image"]] if post.get("image") else [])
                canonical = {"caption": caption, "media_ids": post.get("media_ids") or [shortcode], "ocr_text": post.get("ocr_text")}
                content_hash = _hash(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
                payload = {field: post.get(field) for field in ("summary", "event", "events", "extraction_mode", "extraction_warnings") if field in post}
                values = {"account_id": account["account_id"], "shortcode": shortcode, "permalink": url, "posted_at": post.get("posted_at") or post.get("timestamp") or _now(), "caption": caption, "media_type": post.get("media_type", "image"), "media_urls": media, "ocr_text": post.get("ocr_text"), "raw_payload": {"source_mode": post.get("source_mode") or source_mode, "timestamp_precision": post.get("timestamp_precision", "exact"), "username": account["username"]}, "content_hash": content_hash, "extraction_status": post.get("extraction_status", "processed"), "extraction_version": post.get("extraction_version", "caption-v1"), "extracted_payload": payload, "source_seen_at": _now(), "updated_at": _now()}
                old = self._query(connection, "SELECT * FROM POSTS WHERE post_id=?", (post_id,))
                retain_extraction = bool(old and old[0]["content_hash"] == content_hash and post.get("extraction_status") == "failed")
                if retain_extraction:
                    values["extracted_payload"] = {**(old[0].get("extracted_payload") or {}), "latest_extraction_warnings": post.get("extraction_warnings", [])}
                    values["extraction_version"] = old[0].get("extraction_version")
                if old:
                    report["unchanged" if old[0]["content_hash"] == content_hash else "updated"] += 1
                    self._update(connection, "POSTS", values, "post_id=?", (post_id,))
                else:
                    self._insert(connection, "POSTS", {"post_id": post_id, **values})
                    report["inserted"] += 1
                self._update(connection, "INSTAGRAM_ACCOUNTS", {"ingestion_status": "ok", "last_attempt_at": _now(), "last_success_at": _now(), "last_error": None, "updated_at": _now()}, "account_id=?", (account["account_id"],))
                club = clubs.get(account.get("club_id"))
                if club is None and (post.get("events") or post.get("event")):
                    club = self._club_for(connection, account, clubs)
                if club and club.get("is_active") and not retain_extraction:
                    report["events"] += self._persist_events(connection, post_id, values, post, club, report["warnings"])
        return report

    def _club_for(self, connection, account, clubs):
        """Every account is its own club, created the first time it has an event."""
        club = next((row for row in clubs.values() if row["slug"] == account["username"]), None)
        if club is None:
            club = {"club_id": "club-" + account["username"], "slug": account["username"], "name": account.get("display_name") or account["username"], "is_active": True, "created_at": _now(), "updated_at": _now()}
            self._insert(connection, "CLUBS", club)
            clubs[club["club_id"]] = club
        self._update(connection, "INSTAGRAM_ACCOUNTS", {"club_id": club["club_id"], "updated_at": _now()}, "account_id=?", (account["account_id"],))
        account["club_id"] = club["club_id"]
        return club

    def _persist_events(self, connection, post_id, stored, post, club, warnings):
        proposals = post.get("events") or ([post["event"]] if post.get("event") else [])
        existing = self._query(connection, "SELECT s.*,e.dedupe_key,e.title,e.start_at FROM EVENT_SOURCES s JOIN EVENTS e ON e.event_id=s.event_id WHERE s.post_id=?", (post_id,))
        retained = set()
        count = 0
        for proposal in proposals:
            if not isinstance(proposal, dict) or not proposal.get("validated"):
                warnings.append("An event proposal remains unpublished until source evidence is validated.")
                continue
            try:
                local = datetime.fromisoformat(proposal.get("start_at") or proposal["start"])
                local = local.replace(tzinfo=ZoneInfo(ZONE)) if local.tzinfo is None else local.astimezone(ZoneInfo(ZONE))
            except (ValueError, KeyError, TypeError):
                continue
            title = str(proposal.get("title", "")).strip()[:512]
            if not title or not proposal.get("evidence"):
                continue
            precision = proposal.get("date_precision", "exact")
            if precision not in {"exact", "date_only"}:
                continue
            confidence = proposal.get("confidence")
            if confidence is not None and (not isinstance(confidence, (int, float)) or confidence < 0.8 or confidence > 1):
                continue
            evidence = proposal["evidence"]
            if not isinstance(evidence, dict) or not evidence.get("date"):
                continue
            canonical = {"club_id": club["club_id"], "local_start_date": local.date().isoformat(), "local_start_time": local.strftime("%H:%M") if precision == "exact" else "date-only", "normalized_title": re.sub(r"\s+", " ", unicodedata.normalize("NFC", title).lower()).strip()}
            dedupe = _hash(json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
            same = self._query(connection, "SELECT * FROM EVENTS WHERE dedupe_key=?", (dedupe,))
            source = next((item for item in existing if item["dedupe_key"] == dedupe), None)
            # A single supported edit can keep its identity when title or date anchors it.
            if not source and len(existing) == len(proposals) == 1:
                old = existing[0]
                same_title = re.sub(r"\s+", " ", old["title"].lower()).strip() == canonical["normalized_title"]
                same_day = str(old.get("start_at", ""))[:10] == local.date().isoformat()
                if same_title or same_day:
                    source = old
                else:
                    warnings.append("Ambiguous event edit needs review; its previous source has been retired.")
                    continue
            event_id = same[0]["event_id"] if same else (source["event_id"] if source else _uuid())
            retained.add(event_id)
            location = proposal.get("location_name") or proposal.get("location") or None
            is_free = proposal.get("is_free") if proposal.get("is_free") in (True, False) else None
            search_text = " | ".join(filter(None, [event_id, title, post.get("summary"), club["name"], local.date().isoformat(), location]))
            values = {"dedupe_key": dedupe, "club_id": club["club_id"], "title": title, "description": proposal.get("description") or post.get("summary"), "start_at": local.astimezone(timezone.utc).isoformat(), "local_start_date": local.date().isoformat(), "event_timezone": ZONE, "date_precision": precision, "location_name": location, "is_free": is_free, "price_amount": 0 if is_free is True else None, "price_currency": "USD", "status": "published", "confidence": confidence, "extraction_version": stored["extraction_version"], "search_text": search_text[:16000], "updated_at": _now()}
            current = self._query(connection, "SELECT revision FROM EVENTS WHERE event_id=?", (event_id,))
            if current:
                values["revision"] = current[0]["revision"] + 1
                self._update(connection, "EVENTS", values, "event_id=?", (event_id,))
            else:
                self._insert(connection, "EVENTS", {"event_id": event_id, **values})
            link = self._query(connection, "SELECT source_event_key FROM EVENT_SOURCES WHERE event_id=? AND post_id=?", (event_id, post_id))
            other_primary = self._query(connection, "SELECT post_id FROM EVENT_SOURCES WHERE event_id=? AND is_primary=TRUE AND post_id<>?", (event_id, post_id))
            link_values = {"source_event_key": (link[0]["source_event_key"] if link else source["source_event_key"] if source else _uuid()), "is_primary": not bool(other_primary), "evidence": evidence, "source_content_hash": stored["content_hash"], "extraction_version": stored["extraction_version"], "updated_at": _now()}
            if link:
                self._update(connection, "EVENT_SOURCES", link_values, "event_id=? AND post_id=?", (event_id, post_id))
            else:
                self._insert(connection, "EVENT_SOURCES", {"event_id": event_id, "post_id": post_id, **link_values})
            self._query(connection, "DELETE FROM EVENT_TAGS WHERE event_id=? AND source='extracted'", (event_id,))
            known_tags = self._query(connection, "SELECT tag_id,slug,label FROM TAGS WHERE is_active=TRUE")
            content = (title + " " + stored["caption"]).lower()
            for tag in known_tags:
                if tag["slug"].replace("-", " ") in content or tag["label"].lower() in content:
                    prior = self._query(connection, "SELECT tag_id FROM EVENT_TAGS WHERE event_id=? AND tag_id=?", (event_id, tag["tag_id"]))
                    if not prior:
                        self._insert(connection, "EVENT_TAGS", {"event_id": event_id, "tag_id": tag["tag_id"], "source": "extracted", "confidence": confidence})
            count += 1
        for old in existing:
            if old["event_id"] not in retained:
                self._query(connection, "DELETE FROM EVENT_SOURCES WHERE event_id=? AND post_id=?", (old["event_id"], post_id))
                remaining = self._query(connection, "SELECT post_id FROM EVENT_SOURCES WHERE event_id=? ORDER BY created_at", (old["event_id"],))
                if remaining:
                    self._update(connection, "EVENT_SOURCES", {"is_primary": False}, "event_id=?", (old["event_id"],))
                    self._update(connection, "EVENT_SOURCES", {"is_primary": True}, "event_id=? AND post_id=?", (old["event_id"], remaining[0]["post_id"]))
                else:
                    self._update(connection, "EVENTS", {"status": "draft", "updated_at": _now()}, "event_id=?", (old["event_id"],))
        return count

    def semantic_search(self, query, limit=100):
        if self.backend != "snowflake" or not query.strip():
            return []
        return get_client().search(query, limit)

    def status(self):
        if self.backend == "snowflake":
            return {**get_client().status(), "source": "snowflake"}
        return {"backend": "sqlite", "source": "local", "connected": True, "error": None}


_store = None


def get_store():
    global _store
    with LOCK:
        if _store is None:
            _store = Store()
        return _store
