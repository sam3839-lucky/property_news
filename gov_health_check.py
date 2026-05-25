#!/usr/bin/env python3
"""Pre-flight health check: verify all dependencies are available."""
import sys
import shutil
import subprocess
from pathlib import Path


def check(name, ok):
    status = "OK" if ok else "MISSING"
    print(f"  [{status}] {name}")
    return ok


def main():
    print("gov_crawler health check\n")
    all_ok = True

    # Python deps
    for mod in ["playwright", "yaml", "bs4", "lxml"]:
        try:
            __import__(mod if mod != "bs4" else "bs4")
            all_ok &= check(f"python:{mod}", True)
        except ImportError:
            all_ok &= check(f"python:{mod}", False)

    # Playwright browser
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        all_ok &= check("playwright:chromium", True)
    except Exception as e:
        all_ok &= check(f"playwright:chromium ({e})", False)

    # lark-cli
    lark_path = shutil.which("lark-cli") or shutil.which("lark-cli", path=os.path.expanduser("~/.npm-global/bin"))
    all_ok &= check("lark-cli", lark_path is not None)

    # Config file
    config = Path("config/gov_targets.yaml")
    all_ok &= check("config/gov_targets.yaml", config.exists())

    # Screenshot dirs
    for d in ["screenshots/full", "screenshots/body", "pdfs", "logs"]:
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        all_ok &= check(f"dir:{d}", p.is_dir())

    print(f"\nOverall: {'ALL OK' if all_ok else 'ISSUES FOUND'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    import os
    sys.exit(main())
