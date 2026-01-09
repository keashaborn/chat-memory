#!/usr/bin/env python3
import os
import subprocess
from datetime import datetime

REPO = "/opt/chat-memory"
SSH_CONFIG = "/opt/chat-memory/.ssh/config"
SSH_CMD = f"ssh -F {SSH_CONFIG}"

def run(cmd, check=True):
    return subprocess.run(cmd, cwd=REPO, check=check, text=True, capture_output=True)

def main():
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = SSH_CMD

    # stage everything (respects .gitignore)
    r = subprocess.run(["git", "add", "-A"], cwd=REPO, env=env)
    if r.returncode != 0:
        raise SystemExit("git add failed")

    # exit if no staged changes
    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO, env=env)
    if r.returncode == 0:
        print("No changes staged; exiting.")
        return

    ts = datetime.utcnow().strftime("%Y-%m-%d")
    msg = f"auto: daily snapshot {ts}Z"
    c = subprocess.run(["git", "commit", "-m", msg], cwd=REPO, env=env)
    if c.returncode != 0:
        raise SystemExit("git commit failed")

    p = subprocess.run(["git", "push"], cwd=REPO, env=env)
    if p.returncode != 0:
        raise SystemExit("git push failed")

    print("OK")

if __name__ == "__main__":
    main()
