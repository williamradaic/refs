"""Run refs in the background, starting at login (a macOS LaunchAgent).

`refs service install` writes a small property-list file into
~/Library/LaunchAgents and hands it to launchd, the macOS service manager.
From then on launchd starts `refs serve` when you log in and restarts it if it
ever stops. `refs service uninstall` removes it again. Nothing here needs
admin rights; it is the same mechanism the bCourses sync uses.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path

LABEL = "com.williamradaic.refs"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "refs.log"
PORT = 8377


def _domain() -> str:
    return f"gui/{os.getuid()}"          # "this user's login session", in launchd terms


def definition() -> dict:
    """What launchd is told: which program to run, and how to keep it running."""
    program = shutil.which("refs") or sys.argv[0]
    return {
        "Label": LABEL,
        "ProgramArguments": [str(Path(program).resolve()), "serve", "--port", str(PORT)],
        "RunAtLoad": True,               # start at login (and right after install)
        "KeepAlive": True,               # restart if it exits
        "ProcessType": "Background",
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", PORT)) == 0


def is_loaded() -> bool:
    return subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"],
                          capture_output=True).returncode == 0


def install() -> str:
    if is_loaded():
        uninstall()
    elif port_in_use():
        return (f"Something is already serving on port {PORT} (a refs started by hand?). "
                "Stop it first, then run this again.")
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as handle:
        plistlib.dump(definition(), handle)
    done = subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        return f"launchd refused the service: {done.stderr.strip()}"
    return (f"Installed. refs now starts at login and is running at "
            f"http://refs.localhost:{PORT}\nLog: {LOG}")


def uninstall() -> str:
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)
    return "Removed. refs no longer starts at login and has been stopped."


def restart() -> str:
    """Stop and start the service, e.g. to pick up changes to the code."""
    if not is_loaded():
        return "The service is not installed. Run `refs service install`."
    subprocess.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], capture_output=True)
    return "Restarted."


def status() -> str:
    if not PLIST.exists():
        return "Not installed. `refs service install` sets it up."
    state = "loaded" if is_loaded() else "installed but not loaded"
    answering = "answering" if port_in_use() else "NOT answering"
    return f"Service {state}; http://refs.localhost:{PORT} is {answering}.\nLog: {LOG}"
