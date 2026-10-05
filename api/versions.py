import re, subprocess
from pathlib import Path


def git(proj_dir: Path, *args, timeout=60):
    return subprocess.run(["git", "-C", str(proj_dir), *args],
                          capture_output=True, text=True, timeout=timeout)


def history(proj_dir: Path, limit: int = 40) -> list[dict]:
    r = git(proj_dir, "log", f"-n{limit}", "--pretty=format:%h%x1f%ct%x1f%s")
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            out.append({"sha": parts[0], "time": int(parts[1]), "subject": parts[2]})
    return out


def restore(proj_dir: Path, sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        raise ValueError("bad version id")
    if git(proj_dir, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise ValueError("unknown version")

    git(proj_dir, "add", "-A")
    git(proj_dir, "commit", "-qm", "Snapshot before restore")   # fails harmlessly if nothing changed
    head = git(proj_dir, "rev-parse", "HEAD").stdout.strip()
    subject = git(proj_dir, "log", "-1", "--pretty=%s", sha).stdout.strip()

    # Make the files match <sha>, then move HEAD back so history stays linear and nothing is lost
    if git(proj_dir, "reset", "--hard", "-q", sha).returncode != 0:
        raise RuntimeError("could not restore that version")
    git(proj_dir, "reset", "--soft", "-q", head)
    c = git(proj_dir, "commit", "-qm", f"Restore: {subject[:50]}")
    return "restored" if c.returncode == 0 else "already at that version"