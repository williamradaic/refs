"""The agent skill: tells coding agents (Claude Code, Codex, ...) to use refs.

The skill is skill/SKILL.md inside this package. `refs skill install` copies
it into each agent's skills folder that exists on this Mac (and into Claude
Code's when none does). A folder that is already a link (as in a developer's
setup, linked to the repository) is left as it is. After an update of refs,
installing again refreshes the copies; `refs doctor` says when one is old.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SOURCE = Path(__file__).parent / "skill" / "SKILL.md"


def agent_dirs() -> dict[str, Path]:
    """Skills folders of the agents refs knows, by agent name."""
    home = Path.home()
    return {"Claude Code": home / ".claude" / "skills", "Codex": home / ".codex" / "skills",
            "other agents": home / ".agents" / "skills"}


def _targets() -> dict[str, Path]:
    """The agents present on this Mac (their home folder exists); Claude Code if none."""
    present = {name: d for name, d in agent_dirs().items() if d.parent.is_dir()}
    return present or {"Claude Code": agent_dirs()["Claude Code"]}


def status() -> list[tuple[str, Path, str]]:
    """(agent, folder, state) for each agent present: "current", "old", "linked" or "missing"."""
    out = []
    for name, folder in _targets().items():
        dest = folder / "refs"
        if dest.is_symlink():
            state = "linked"
        elif not (dest / "SKILL.md").is_file():
            state = "missing"
        else:
            state = "current" if (dest / "SKILL.md").read_bytes() == SOURCE.read_bytes() else "old"
        out.append((name, dest, state))
    return out


def install() -> list[str]:
    """Copy the skill to every agent present. Returns one line per agent."""
    lines = []
    for name, dest, state in status():
        if state == "linked":
            lines.append(f"{name}: {dest} is a link (to {dest.resolve()}); left as it is")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE, dest / "SKILL.md")
        lines.append(f"{name}: {'updated' if state == 'old' else 'installed' if state == 'missing' else 'already current'} "
                     f"{dest / 'SKILL.md'}")
    return lines
