"""Pure history migration: flat list <-> per-niche groups.

Used by core.load_history / save_history (migrate-on-load is best-effort).
All functions are pure (no file IO) and lossless: entries are copied with
dict(h) and never filtered, so any new per-entry key (e.g. premise,
key_words, skeleton) survives a migrate/flatten round-trip untouched.
"""

__all__ = ["migrate_history", "flatten_history", "duplicate_reason"]


def migrate_history(flat: list[dict],
                    default_niche: str = "story") -> dict[str, list[dict]]:
    """Group a flat history.json list into {niche: [entries]}."""
    grouped: dict[str, list[dict]] = {}
    for h in flat or []:
        if not isinstance(h, dict):
            continue
        key = str(h.get("niche", default_niche) or default_niche)
        grouped.setdefault(key, []).append(dict(h))
    return grouped


def flatten_history(grouped: dict[str, list[dict]]) -> list[dict]:
    """Merge per-niche groups back to a flat list ordered by date."""
    flat: list[dict] = []
    for entries in (grouped or {}).values():
        flat.extend(dict(h) for h in entries if isinstance(h, dict))
    flat.sort(key=lambda h: str(h.get("date", "")))
    return flat


def duplicate_reason(entry: dict, niche_history: list[dict]) -> str | None:
    """Per-niche dupe check: same title/topic (casefolded), else None."""
    title = str(entry.get("title", "")).strip().casefold()
    topic = str(entry.get("topic", "")).strip().casefold()
    for h in niche_history or []:
        if not isinstance(h, dict):
            continue
        if title and str(h.get("title", "")).strip().casefold() == title:
            return f"title '{entry.get('title')}' already used"
        if topic and str(h.get("topic", "")).strip().casefold() == topic:
            return f"topic '{entry.get('topic')}' already used"
    return None
