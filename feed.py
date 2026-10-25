"""Deterministic upcoming-event eligibility and preference-based ranking."""

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


PURDUE_ZONE = "America/Indiana/Indianapolis"
ALIASES = {
    "artificial-intelligence": ("artificial intelligence", "ai", "machine learning", "ml"),
    "robotics": ("robotics", "robots"),
    "arts": ("arts", "art", "creative"),
    "sports": ("sports", "athletics"),
    "volunteering": ("volunteering", "volunteer", "community service"),
    "workshop": ("workshop", "workshops", "hands on"),
    "social": ("social", "meet people", "mixer"),
    "career": ("career", "careers", "networking", "internships"),
    "beginner-friendly": ("beginner friendly", "beginner", "beginners"),
    "virtual": ("virtual", "online", "remote"),
}
STOP_WORDS = set("i im i'm am a an the to of for in on at and or but with that this these those my me "
                 "want would like interested interest interests events event clubs club about please "
                 "looking prefer include avoid exclude not no dont don't only more less be is are "
                 "it as any some all this week tomorrow today next free cheap under dollars budget".split())


def _words(value):
    return set(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _instant(value, zone):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.min)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=zone) if result.tzinfo is None else result


def _switch(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false", "1", "0"}:
        return value.lower() in {"true", "1"}
    raise ValueError("Filters must be true or false.")


def _semantic_scores(semantic_ids):
    scores = {}
    for rank, item in enumerate(semantic_ids or [], start=1):
        event_id = item.get("event_id", item.get("EVENT_ID")) if isinstance(item, dict) else item
        if event_id is not None and rank <= 100:
            scores.setdefault(str(event_id), (101 - rank) / 100)
    return scores


def build_feed(events, preferences, follows, query="", filters=None, semantic_ids=None):
    preferences, filters = preferences or {}, filters or {}
    now = datetime.now(timezone.utc)
    user_zone = ZoneInfo(preferences.get("timezone") or PURDUE_ZONE)
    today = now.astimezone(user_zone).date()
    horizon = int(filters.get("horizon_days", preferences.get("horizon_days", 30)))
    if not 1 <= horizon <= 90:
        raise ValueError("Choose a horizon between 1 and 90 days.")
    from_day = _day(filters["from_date"]) if filters.get("from_date") else today
    through_day = _day(filters["through_date"]) if filters.get("through_date") else today + timedelta(days=horizon)
    if through_day < from_day or (through_day - from_day).days > 90:
        raise ValueError("Choose an ordered date range of at most 90 days.")
    include_past = _switch(filters.get("include_past", False))
    free_only = _switch(filters.get("free_only", preferences.get("free_only", False)))
    followed_only = _switch(filters.get("followed_only", preferences.get("followed_only", False)))
    interest_ids = set(preferences.get("interest_tag_ids") or [])
    excluded_ids = set(preferences.get("excluded_tag_ids") or [])
    followed_accounts = {str(item.get("account_id")) for item in follows or [] if item.get("account_id")}
    followed_clubs = {str(item.get("club_id")) for item in follows or [] if item.get("club_id")}
    search_text = " ".join(part for part in (query, preferences.get("preference_text", "")) if part).lower()
    query_words = _words(search_text) - STOP_WORDS
    semantic = _semantic_scores(semantic_ids)
    personalized = bool(followed_accounts or interest_ids or search_text.strip())
    ranked, seen = [], set()
    for original in events or []:
        event = dict(original)
        event_id = str(event.get("event_id", ""))
        if not event_id or event_id in seen or event.get("status", "published") != "published":
            continue
        if event.get("club_active") is False:
            continue
        seen.add(event_id)
        event_zone = ZoneInfo(event.get("event_timezone") or PURDUE_ZONE)
        precision = event.get("date_precision", "exact")
        try:
            start = _instant(event.get("start_at") or event.get("local_start_date"), event_zone)
            local_day = _day(event["local_start_date"]) if event.get("local_start_date") else start.astimezone(event_zone).date()
        except (TypeError, ValueError):
            continue
        if precision not in {"exact", "date_only"} or not from_day <= local_day <= through_day:
            continue
        event["is_past"] = start < now if precision == "exact" else local_day < now.astimezone(event_zone).date()
        if event["is_past"] and not include_past:
            continue
        tags = event.get("tags") or []
        event_tag_ids = set(event.get("tag_ids") or [tag["tag_id"] for tag in tags])
        if event_tag_ids & excluded_ids:
            continue
        source_posts = event.get("source_posts") or []
        source_accounts = {str(source.get("account_id")) for source in source_posts if source.get("account_id")}
        if event.get("account_id"):
            source_accounts.add(str(event["account_id"]))
        followed = bool(followed_accounts & source_accounts or str(event.get("club_id")) in followed_clubs)
        if followed_only and not followed:
            continue
        if free_only and event.get("is_free") is not True:
            continue
        matched_tags = interest_ids & event_tag_ids
        tag_fraction = len(matched_tags) / len(interest_ids) if interest_ids else 0
        searchable = " ".join(str(event.get(field) or "") for field in ("title", "description", "club_name", "location_name"))
        searchable += " " + " ".join(str(tag.get("label") or tag.get("tag_id") or "") for tag in tags)
        lexical = len(query_words & _words(searchable)) / len(query_words) if query_words else 0
        days_until = max(0, (local_day - today).days)
        date_preference = 0
        if "tomorrow" in search_text and local_day == today + timedelta(days=1):
            date_preference = 1
        elif "today" in search_text and local_day == today:
            date_preference = 1
        elif "this week" in search_text and 0 <= (local_day - today).days <= 6 - today.weekday():
            date_preference = 0.9
        elif "next week" in search_text and 7 - today.weekday() <= (local_day - today).days <= 13 - today.weekday():
            date_preference = 0.9
        budget_preference = 0
        if re.search(r"\b(?:free|cheap|inexpensive)\b", search_text) and event.get("is_free") is True:
            budget_preference = 0.8
        budget = re.search(r"\b(?:under|below|less than)\s*\$?\s*(\d+(?:\.\d{1,2})?)", search_text)
        if budget and event.get("price_amount") is not None and float(event["price_amount"]) < float(budget.group(1)):
            budget_preference = 0.8
        relevance = max(semantic.get(event_id, 0), lexical, date_preference, budget_preference)
        score = 50 * followed + 30 * tag_fraction + 15 * relevance + 5 / (1 + days_until)
        reasons = []
        if followed:
            reasons.append("followed_account")
        if matched_tags:
            reasons.append("interest_tag")
        if semantic.get(event_id):
            reasons.append("semantic_match")
        elif lexical:
            reasons.append("text_match")
        if date_preference:
            reasons.append("preferred_date")
        if budget_preference:
            reasons.append("preferred_price")
        if not reasons:
            reasons.append("upcoming")
        primary = next((source for source in source_posts if source.get("is_primary")), source_posts[0] if source_posts else {})
        event.update(
            start_at=start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if precision == "exact" else None,
            local_start_date=local_day.isoformat(), event_timezone=str(event_zone),
            start=local_day.isoformat() if precision == "date_only" else start.isoformat(),
            location=event.get("location_name") or "Location not announced",
            url=primary.get("url") or event.get("registration_url") or "",
            username=event.get("username") or primary.get("username") or "",
            reasons=reasons, score=round(score, 4),
        )
        if event.get("price_amount") is not None:
            event["price_amount"] = f"{float(event['price_amount']):.2f}"
        ranked.append((event, start))
    ranked.sort(key=lambda pair: (-pair[0]["score"] if personalized else 0, pair[1], pair[0]["event_id"]))
    limit = int(filters.get("limit", 50))
    if not 1 <= limit <= 50:
        raise ValueError("Choose a limit between 1 and 50.")
    items = [item for item, _ in ranked[:limit]]
    warnings = []
    if not items:
        warnings.append("No upcoming events match your current filters. Refresh followed accounts or adjust your controls.")
    return {"events": items, "items": items, "next_cursor": None,
            "meta": {"generated_at": now.isoformat().replace("+00:00", "Z"),
                     "preference_version": preferences.get("version", 1),
                     "search_degraded": bool(search_text.strip()) and semantic_ids is None,
                     "total_matches": len(ranked), "warnings": warnings}, "warnings": warnings}


def preview_preferences(text, tags):
    if not isinstance(text, str) or len(text) > 2000:
        raise ValueError("Preferences must be at most 2,000 characters.")
    lowered = text.lower().replace("-", " ")
    included, excluded, matched_words = [], [], set()
    for tag in tags or []:
        tag_id = tag.get("tag_id")
        phrases = set(ALIASES.get(tag.get("slug", tag_id), ()))
        phrases.update(str(tag.get(field) or "").lower().replace("-", " ") for field in ("slug", "label"))
        for phrase in sorted(phrases, key=len, reverse=True):
            if not phrase:
                continue
            match = re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", lowered)
            if not match:
                continue
            prefix = lowered[max(0, match.start() - 35):match.start()]
            negated = bool(re.search(r"(?:avoid|exclude|no|not interested in|don't want|dont want)\s+(?:\w+\s+){0,2}$", prefix))
            (excluded if negated else included).append(tag_id)
            matched_words.update(_words(phrase))
            break
    excluded = list(dict.fromkeys(excluded))
    included = [tag_id for tag_id in dict.fromkeys(included) if tag_id not in excluded]
    unmatched = sorted(_words(text) - matched_words - STOP_WORDS)
    return {"proposed": {"interest_tag_ids": included, "excluded_tag_ids": excluded,
                         "followed_only": False, "free_only": False, "horizon_days": 30},
            "unmatched_terms": unmatched,
            "warnings": ["Free-text dates and budget requests affect ranking. Use the controls for strict filters."]}
