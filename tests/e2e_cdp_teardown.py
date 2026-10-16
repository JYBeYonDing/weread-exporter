# -*- coding: utf-8 -*-
"""End-to-end check of the CDP-teardown fix.

Runs a real export, then kills the Chromium that export launched mid-run — which
is what produced the InvalidStateError wedge on 《赢单九问》 (twice: 02:48 and
07:27, once leaving the run stuck for 4h46m).

Before the fix: the process hangs here forever, with the last log line a
`Navigate to ...` and no further output.
After the fix: the pending `page.goto` raises, the exporter logs the failure,
restarts the browser and carries on to the next chapter.
"""
import json
import os
import subprocess
import sys
import time

REPO = r"C:\Users\zglzs\workplace\opensource\weread-exporter"
BOOK = "9c832d005c85699c860113f"        # 失传的营销秘诀, 10 chapters
KILL_AFTER = 75                          # seconds of healthy running before the kill
OBSERVE_AFTER_KILL = 150                 # seconds to watch for recovery

LOG = os.path.join(REPO, "logs", "e2e_cdp_teardown.log")


def ps(script):
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, timeout=120).stdout.strip()
    return json.loads(out) if out else []


def main():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    log = open(LOG, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "weread_exporter", "-b", BOOK, "-o", "md", "-o", "pdf"],
        cwd=REPO, stdout=log, stderr=subprocess.STDOUT, env=env,
    )
    print("export pid %d; letting it settle for %ds" % (proc.pid, KILL_AFTER))
    time.sleep(KILL_AFTER)

    if proc.poll() is not None:
        print("export already exited (rc=%s) — cannot run the kill test" % proc.returncode)
        return 1

    kids = ps("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
              "Where-Object { $_.ParentProcessId -eq %d } | "
              "Select-Object ProcessId | ConvertTo-Json -Compress" % proc.pid)
    pids = [k["ProcessId"] for k in kids] if isinstance(kids, list) else (
        [kids["ProcessId"]] if kids else [])
    if not pids:
        print("no chromium child of pid %d — cannot run the kill test" % proc.pid)
        proc.kill()
        return 1

    print("killing exporter chromium %s (simulating the CDP link drop)" % pids)
    for pid in pids:
        ps("Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % pid)

    print("watching %ds for recovery..." % OBSERVE_AFTER_KILL)
    time.sleep(OBSERVE_AFTER_KILL)
    alive = proc.poll() is None
    log.flush()

    text = open(LOG, encoding="utf-8", errors="ignore").read()
    tail = text[-3000:]
    log.seek(0, os.SEEK_END)
    size_at_check = log.tell()
    time.sleep(45)          # is the log still moving?
    still_moving = log.tell() > size_at_check
    log.close()

    print("\n--- verdict ---")
    print("process alive after kill:      %s" % alive)
    print("log still advancing (45s):     %s" % still_moving)
    print("saw a browser restart:         %s" % ("Launch url" in text.split("killing")[-1] or text.count("Launch url") > 1))
    print("saw a settled-callback failure: %s" % ("Target closed" in tail or "Protocol error" in tail))
    resumed = "Export chapter" in tail
    print("exported a chapter after that: %s" % resumed)

    hung = alive and not still_moving
    print("HUNG (the pre-fix behaviour):  %s" % hung)

    if proc.poll() is None:
        proc.kill()
        time.sleep(2)
    return 1 if hung else 0


if __name__ == "__main__":
    sys.exit(main())
