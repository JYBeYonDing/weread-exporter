#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kill leftover pyppeteer Chromium instances that no longer have a live parent.

Why: when an export run has to be killed (a wedged CDP connection leaves the
Python process hanging on an event loop that never advances — observed on
《赢单九问》, stuck 4.5 h at 9/15 chapters), the Chromium it launched can survive
as an orphan and sit on memory plus a window.

Safety: only processes whose image path is under pyppeteer's own
local-chromium directory are considered. The two long-lived debugging browsers
(ports 9333 / 9334) run from the installed Google Chrome, so they are never
matched. A process whose parent is still alive is left alone.

Usage:  python kill_stale_chromium.py [--dry-run]
"""
import argparse
import json
import os
import subprocess
import sys

PYPPETEER_MARKER = os.path.join("pyppeteer", "pyppeteer", "local-chromium")


def ps_json(script):
    """Run PowerShell expecting JSON, returning a list of dicts."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=180,
    ).stdout.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        return [data]
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    procs = ps_json(
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' } | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath | ConvertTo-Json -Compress"
    )
    live_pids = {row["Id"] for row in ps_json(
        "Get-Process | Select-Object Id | ConvertTo-Json -Compress"
    ) if "Id" in row}

    targets = []
    for proc in procs:
        if PYPPETEER_MARKER not in (proc.get("ExecutablePath") or ""):
            continue
        if proc.get("ParentProcessId") in live_pids:
            continue
        targets.append(proc["ProcessId"])

    if not targets:
        print("no stale pyppeteer chromium")
        return 0

    print("stale pyppeteer chromium: %s" % targets)
    if args.dry_run:
        return 0
    for pid in targets:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % pid],
            timeout=60,
        )
    print("killed %d" % len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
