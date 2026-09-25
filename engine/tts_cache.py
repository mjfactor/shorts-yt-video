"""Content-addressed file cache for TTS chunks; resume partial runs."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


def hash_key(text: str, voice: str, style: str = "", speed: float = 1.0) -> str:
    """Stable sha1 over the normalized (text, voice, style, speed) tuple."""
    canon = "|".join([text.strip(), voice.strip(), style.strip(), f"{float(speed):.4f}"])
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]


def path_for(cache_dir: str | Path, key: str, suffix: str = ".mp3") -> Path:
    """Cache slot for `key` (dir created on demand by store_*)."""
    return Path(cache_dir) / f"{key}{suffix}"


def is_cached(cache_dir: str | Path, key: str, suffix: str = ".mp3") -> bool:
    """True when a non-empty file already occupies the slot."""
    p = path_for(cache_dir, key, suffix)
    return p.exists() and p.stat().st_size > 0


def store_file(src: str | Path, cache_dir: str | Path, key: str,
               suffix: str = ".mp3") -> Path:
    """Copy `src` into its cache slot; returns the slot path."""
    dest = path_for(cache_dir, key, suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest


def store_bytes(data: bytes, cache_dir: str | Path, key: str,
                suffix: str = ".mp3") -> Path:
    """Write bytes into their cache slot; returns the slot path."""
    dest = path_for(cache_dir, key, suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def chunk_name(prefix: str, index: int, suffix: str = ".mp3") -> str:
    """Chunk filename for beat/segment `index` under run `prefix`."""
    return f"{prefix}_{index:02d}{suffix}"


def cached_indices(cache_dir: str | Path, prefix: str, total: int,
                   suffix: str = ".mp3") -> list[int]:
    """Indices already on disk (resume: synthesize only the rest)."""
    d = Path(cache_dir)
    return [i for i in range(total)
            if (d / chunk_name(prefix, i, suffix)).exists()]


def missing_indices(cache_dir: str | Path, prefix: str, total: int,
                    suffix: str = ".mp3") -> list[int]:
    """Indices still needing synthesis."""
    have = set(cached_indices(cache_dir, prefix, total, suffix))
    return [i for i in range(total) if i not in have]
