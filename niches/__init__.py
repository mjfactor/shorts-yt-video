"""Niche registry: auto-discover niches/* subpackages.

Each niche is a subpackage (e.g. niches/story/) exposing a ``niche``
object (or module) compatible with ``core.run(niche)``. Private
packages (leading ``_``, e.g. ``_template``) are skipped.
Core never imports this package; pipeline.py wires ``load_niche()``.
"""

import importlib
import os
import pkgutil

__all__ = ["available_niches", "load_niche"]

DEFAULT_NICHE = "story"


def available_niches() -> list[str]:
    """Sorted names of discoverable niche subpackages."""
    names = [
        m.name for m in pkgutil.iter_modules(__path__)
        if m.ispkg and not m.name.startswith("_")
    ]
    return sorted(names)


def load_niche(name: str | None = None):
    """Import and return the requested niche (NICHE env, default story).

    Returns the subpackage's ``niche`` attribute when present, else the
    module itself. Raises ImportError naming available niches.
    """
    want = (name or os.getenv("NICHE", DEFAULT_NICHE) or DEFAULT_NICHE)
    want = want.strip().lower() or DEFAULT_NICHE
    try:
        mod = importlib.import_module(f"{__name__}.{want}")
    except ImportError:
        avail = available_niches()
        known = ", ".join(avail) if avail else "(none yet)"
        raise ImportError(
            f"Unknown niche '{want}'. Available niches: {known} "
            f"(set NICHE env var)."
        ) from None
    return getattr(mod, "niche", mod)
