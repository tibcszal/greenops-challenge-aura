"""Per-run credentials read from a file, instead of six `export` lines by hand.

Every simulation run hands out a fresh key, secret and set of endpoints, so these
values change more often than anything else in a session.  What the portal hands
out is a shell snippet (`export NAME=value`), which is only usable by sourcing it
into exactly the right shell, and which `docker --env-file` cannot read at all:
that parser would take `export NAME` as the variable's name.

So the orchestrator reads the file itself.  It looks in the working directory and
in every parent, which means one file in the repository root serves the main
checkout and every git worktree beneath it, and a mounted checkout serves a
container with no `-e` plumbing at all.  Real environment variables always win
over the file, so an explicit `-e` or `export` still overrides it, and an
organizer rerun that injects credentials properly is unaffected.

Nothing here prints a value: credentials must not reach a log or a transcript.
"""
from __future__ import annotations

import os
from pathlib import Path

# The five the orchestrator needs, plus the work endpoint the portal also hands out.
VARS = ("EXOSCALE_API_KEY", "EXOSCALE_API_SECRET", "GREENOPS_CLOUD_ENDPOINT",
        "GREENOPS_GRID_ENDPOINT", "GREENOPS_GRID_SECRET", "GREENOPS_WORK_ENDPOINT")
REQUIRED = ("EXOSCALE_API_KEY", "EXOSCALE_API_SECRET", "GREENOPS_CLOUD_ENDPOINT",
            "GREENOPS_GRID_ENDPOINT", "GREENOPS_GRID_SECRET")
MASK = {"EXOSCALE_API_KEY", "EXOSCALE_API_SECRET", "GREENOPS_GRID_SECRET"}

FIXED = ("greenops.env", ".env")      # conventional names, in this order
PATTERN = "greenops-*.env"            # what the portal download is called
MAX_UP = 8                            # parents to walk; a worktree sits 3 below the repo root


def parse(text: str) -> dict:
    """Read `KEY=value` lines, tolerating what the portal and editors actually emit:
    an `export ` prefix, `#` comments, blank lines, CRLF endings and quoted values.

    A `#` is not treated as an inline comment, because a secret may legitimately
    contain one; put a comment on its own line instead.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        key = key.strip()
        if not sep or not key.replace("_", "").isalnum():
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def candidates(start: Path | None = None, extra: Path | None = None) -> list[Path]:
    """Every file we would consider, best first.

    A nearer directory always beats a farther one, and inside one directory the
    most recently written file wins.  That makes the actual workflow correct
    without any cleanup: a new run's download lands next to last run's and is
    simply newer.  `run env` lists the ones it passed over, so the choice is never
    a mystery."""
    found: list[Path] = []
    seen: set[Path] = set()
    here = Path(start or Path.cwd()).resolve()
    roots = [here, *list(here.parents)[:MAX_UP]]
    if extra:
        roots.append(Path(extra).resolve())
    for d in roots:
        if d in seen:
            continue
        seen.add(d)
        here_files = {d / name for name in FIXED if (d / name).is_file()}
        here_files.update(p for p in d.glob(PATTERN) if p.is_file())
        found.extend(sorted(here_files, key=lambda p: (-p.stat().st_mtime, p.name)))
    return found


def find(start: Path | None = None, extra: Path | None = None) -> Path | None:
    for p in candidates(start, extra):
        return p
    return None


def load(path=None, start: Path | None = None, extra: Path | None = None,
         environ=None) -> tuple[Path | None, list[str]]:
    """Put the file's variables into `environ` without overwriting what is already
    set.  Returns the file used and the names it actually contributed.

    An explicit `path` that does not exist is an error: the caller asked for that
    file by name.  Discovery finding nothing is not, because the environment may
    already carry the credentials.
    """
    env = environ if environ is not None else os.environ
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"env file not found: {p}")
    else:
        p = find(start, extra)
        if p is None:
            return None, []
    applied = []
    for key, val in parse(p.read_text(encoding="utf-8", errors="replace")).items():
        if not env.get(key):
            env[key] = val
            applied.append(key)
    return p, applied


def status(environ=None) -> list[tuple[str, str]]:
    """One line per variable for `run env`, with secrets reduced to a length so the
    output is safe to paste into a bug report."""
    env = environ if environ is not None else os.environ
    rows = []
    for key in VARS:
        val = env.get(key)
        if not val:
            rows.append((key, "MISSING" if key in REQUIRED else "unset (optional)"))
        elif key in MASK:
            rows.append((key, f"set, {len(val)} chars"))
        else:
            rows.append((key, val))
    return rows


def missing(environ=None) -> list[str]:
    env = environ if environ is not None else os.environ
    return [k for k in REQUIRED if not env.get(k)]
