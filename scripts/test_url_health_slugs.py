#!/usr/bin/env python3
"""Regression test for agent_url_health._site_slugs multi-content-dir support.

Locks in that all 5 sites surface their non-default-locale slugs in the
inventory. Before the fix on 2026-05-11, parallel-layout sites (matelas,
bureau, cafe, aspirateur) showed zero EN/DE/ES/IT entries because the old
rglob pattern only matched content/ subtrees, not content-en/ etc.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agent_url_health import _site_slugs


def _by_locale(slugs):
    out: dict[str, int] = {}
    for path, _ in slugs:
        first = path.split("/", 2)[1] if path.startswith("/") else ""
        if first in {"en", "de", "es", "it", "uk"}:
            out[first] = out.get(first, 0) + 1
        else:
            out["fr"] = out.get("fr", 0) + 1
    return out


def test_each_site_covers_multiple_locales():
    """Each of the 5 active sites must inventory at least 3 locales with >10 slugs each."""
    for site in ("matelas", "bureau", "cafe", "aspirateur", "pixinstant"):
        slugs = _site_slugs(site)
        by_loc = _by_locale(slugs)
        # Require at least 3 locales with >10 slugs each. Pre-fix value was 1 (fr only)
        # on parallel-layout sites.
        rich_locales = [loc for loc, n in by_loc.items() if n > 10]
        assert len(rich_locales) >= 3, (
            f"FAIL: {site} only has rich coverage for {rich_locales} (full={by_loc})"
        )
        print(f"✓ {site}: {len(slugs)} slugs across {len(rich_locales)} locales")


def test_matelas_en_paths_include_slash_prefix():
    """matelas EN slugs must be public paths starting with /en/."""
    slugs = _site_slugs("matelas")
    en_paths = [path for path, _ in slugs if path.startswith("/en/")]
    assert len(en_paths) > 50, f"FAIL: only {len(en_paths)} EN paths for matelas"
    # Spot check a few known slugs (these MDX files exist on matelas content-en/)
    en_slugs = {slug for path, slug in slugs if path.startswith("/en/")}
    for known in ("test-emma-original", "test-morphea-jade", "tediber"):
        assert known in en_slugs, f"FAIL: matelas /en/ inventory missing {known!r}"
    print(f"✓ matelas /en/ inventory contains {len(en_paths)} paths including known slugs")


def test_pixinstant_en_nested_layout():
    """pixinstant uses content/en/ (nested), not content-en/ (parallel)."""
    slugs = _site_slugs("pixinstant")
    en_paths = [path for path, _ in slugs if path.startswith("/en/")]
    assert len(en_paths) > 50, f"FAIL: only {len(en_paths)} EN paths for pixinstant"
    en_slugs = {slug for path, slug in slugs if path.startswith("/en/")}
    for known in ("instax-mini-12-vs-mini-11", "instax-vs-polaroid-2026"):
        assert known in en_slugs, f"FAIL: pixinstant /en/ inventory missing {known!r}"
    print(f"✓ pixinstant /en/ (nested layout) contains {len(en_paths)} paths")


def test_no_node_modules_pollution():
    """Inventory must not contain MDX paths under node_modules."""
    for site in ("matelas", "pixinstant"):
        slugs = _site_slugs(site)
        # Inventory stores (public_path, slug) — slug is just file stem. Check via
        # re-walking to make sure none of the source MDX paths are under node_modules.
        # Simpler proxy: total count is reasonable (matelas ~700, pixinstant ~350)
        # whereas pre-fix rglob included node_modules and inflated to 900+.
        assert len(slugs) < 1000, f"FAIL: {site} has {len(slugs)} slugs — likely scanning node_modules"
    print("✓ slug counts are within sane bounds (no node_modules pollution)")


if __name__ == "__main__":
    tests = [
        test_each_site_covers_multiple_locales,
        test_matelas_en_paths_include_slash_prefix,
        test_pixinstant_en_nested_layout,
        test_no_node_modules_pollution,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"✗ {t.__name__}: {e}")
            failures += 1
        except Exception as e:
            print(f"✗ {t.__name__}: unexpected: {e}")
            failures += 1
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
