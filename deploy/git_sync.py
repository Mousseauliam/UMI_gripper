import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time


def git(*args, cwd=None):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Git command failed")
    return result.stdout.strip()


def ethernet_ready(interface):
    device = Path("/sys/class/net") / interface
    if (device / "wireless").exists():
        return False
    try:
        if (device / "carrier").read_text().strip() != "1":
            return False
        result = subprocess.run(
            ["ip", "-j", "address", "show", "dev", interface],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
        return any(
            address.get("scope") == "global"
            for link in json.loads(result.stdout)
            for address in link.get("addr_info", [])
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def sync(repository, branch, destination):
    destination = Path(destination).expanduser().resolve()
    if not repository or not branch:
        return "waiting for repository and branch configuration"
    if not destination.exists():
        if not git("ls-remote", "--heads", repository, "refs/heads/" + branch):
            return "waiting for the first push to " + branch
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".umi-clone-", dir=destination.parent) as temporary:
            checkout = Path(temporary) / "checkout"
            git("clone", "--single-branch", "--branch", branch, "--", repository, str(checkout))
            checkout.rename(destination)
        return "cloned " + git("rev-parse", "--short", "HEAD", cwd=destination)
    if not (destination / ".git").is_dir():
        raise RuntimeError("Destination exists and is not a Git checkout")
    if git("remote", "get-url", "origin", cwd=destination) != repository:
        raise RuntimeError("Configured repository differs from origin")
    if git("branch", "--show-current", cwd=destination) != branch:
        raise RuntimeError("Checkout is on a different branch")
    if git("status", "--porcelain", cwd=destination):
        raise RuntimeError("Local changes: refusing to overwrite them")
    git("fetch", "--prune", "origin", branch, cwd=destination)
    before = git("rev-parse", "HEAD", cwd=destination)
    after = git("rev-parse", "FETCH_HEAD", cwd=destination)
    if before == after:
        return "up to date"
    git("merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD", cwd=destination)
    git("merge", "--ff-only", "FETCH_HEAD", cwd=destination)
    return "updated " + after[:12]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/umi-git-sync.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    last = None
    while True:
        started = time.monotonic()
        try:
            config = json.loads(Path(args.config).read_text())
            if not ethernet_ready(config.get("ethernet", "eth0")):
                status = "waiting for Ethernet"
            else:
                status = sync(
                    config.get("repository", ""),
                    config.get("branch", "main"),
                    config.get("destination", "/home/admin/umi-v3"),
                )
        except Exception as error:
            status = "sync failed: " + str(error)
        if status != last:
            logging.info(status)
            last = status
        if args.once:
            return
        time.sleep(max(0, 10 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
