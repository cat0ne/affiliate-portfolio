#!/usr/bin/env python3
"""Regression tests for _find_mdx_for_url multi-content-dir support.

Live filesystem state verified 2026-05-11:
  - matelas/bureau/cafe:  parallel layout (content/ + content-<loc>/)
  - aspirateur (mixed):   content/ + content-en/ + content/es/
  - pixinstant:           nested layout (content/ + content/<loc>/)

Each case asserts which actual file on disk we expect to find.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_ctr_optimizer import BASE_DIR, _find_mdx_for_url


CASES = [
    # (label, site_slug, url, expected_relative_path_or_None)
    (
        "matelas EN avis (URL /avis/ but file in tests/)",
        "matelas",
        "https://matelas-expert.fr/en/avis/tediber-avis-test/",
        "matelas/content-en/tests/tediber-avis-test.mdx",
    ),
    (
        "matelas FR test default-locale",
        "matelas",
        "https://matelas-expert.fr/test/test-emma-original/",
        "matelas/content/tests/test-emma-original.mdx",
    ),
    (
        "matelas EN test parallel layout",
        "matelas",
        "https://matelas-expert.fr/en/test/test-emma-original/",
        "matelas/content-en/tests/test-emma-original.mdx",
    ),
    (
        "aspirateur ES nested layout (content/es/)",
        "aspirateur",
        "https://top-aspirateur.fr/es/comparatif/mejores-aspiradoras-sin-cable-2026",
        "aspirateur/content/es/comparatifs/mejores-aspiradoras-sin-cable-2026.mdx",
    ),
    (
        "pixinstant EN nested layout (content/en/)",
        "pixinstant",
        "https://pixinstant.com/en/test/test-polaroid-go-gen2/",
        "pixinstant/content/en/tests/test-polaroid-go-gen2.mdx",
    ),
]


def run() -> int:
    passes = 0
    for label, site, url, expected in CASES:
        expected_abs = (BASE_DIR / expected).resolve() if expected else None
        got = _find_mdx_for_url(site, url)
        got_abs = got.resolve() if got else None
        ok = got_abs == expected_abs
        marker = "✓" if ok else "✗"
        print(f"{marker} {label}")
        if not ok:
            print(f"    expected: {expected_abs}")
            print(f"    got:      {got_abs}")
        else:
            passes += 1
    print(f"\n{passes}/{len(CASES)} passed")
    return 0 if passes == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(run())
