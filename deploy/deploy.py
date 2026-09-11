#!/usr/bin/python3
"""Pull deployment with isolated venvs, durable rollback, and a shared lock.

Installed by root, executed by opdash-deploy. Only fixed systemctl actions use sudo.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path("/opt/opdash")
DATA = Path("/var/lib/opdash-deploy")
REPO = DATA / "repo.git"
URL = "https://github.com/hangchow/opdash.git"
SERVICE = "opdash-web.service"
HEALTH_TIMEOUT = 120
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(message):
    print(time.strftime("%Y-%m-%dT%H:%M:%S%z"), message, flush=True)


def run(args, *, timeout=600, cwd=None, capture=False):
    process = subprocess.Popen(
        [str(a) for a in args], cwd=cwd, start_new_session=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    if process.returncode:
        # Fetch has no credentials; do not capture application snapshots in errors.
        raise RuntimeError(f"{args[0]} failed ({process.returncode}): "
                           + (stderr or b"").decode(errors="replace")[-1000:])
    return (stdout or b"").decode().strip()


def durable_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_state():
    path = DATA / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save(state):
    durable_json(DATA / "state.json", state)


def checked_sha(sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise ValueError("Expected a full 40-character commit SHA")
    return sha


def linked(name):
    path = ROOT / name
    return checked_sha(path.resolve(strict=True).name) if path.is_symlink() else None


def point(name, sha):
    path = ROOT / name
    if sha is None:
        path.unlink(missing_ok=True)
    else:
        target = ROOT / "releases" / checked_sha(sha)
        if not (target / ".complete").is_file():
            raise RuntimeError("Refusing to activate incomplete release")
        temporary = ROOT / ("." + name + ".next")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, path)
    sync_directory(ROOT)


def control(action):
    assert action in {"stop", "start", "restart"}
    run(["sudo", "-n", "/usr/bin/systemctl", action, SERVICE], timeout=45)


def config():
    result = {}
    for line in Path("/etc/opdash/opdash.env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            result[key] = value.strip().strip('"').strip("'")
    return result


def opend_available(cfg):
    try:
        for port in cfg.get("FUTU_PORTS", "11111").split(","):
            with socket.create_connection((cfg.get("FUTU_HOST", "127.0.0.1"), int(port)), timeout=2):
                pass
        return True
    except OSError:
        return False


def request(cfg, path, json_body=True):
    url = f"http://{cfg['WEB_HOST']}:{cfg['WEB_PORT']}{path}"
    with HTTP.open(url, timeout=4) as response:
        body = response.read()
    return json.loads(body) if json_body else body


def opend_logged_in(cfg, sha):
    # Used for bootstrap, where there is no existing /readyz to check.
    # The SDK may retry login forever: isolate it in a bounded subprocess.
    probe = """
import sys
from futu import OpenQuoteContext, RET_OK
for port in sys.argv[2].split(','):
    ctx = OpenQuoteContext(host=sys.argv[1], port=int(port))
    try:
        ret, state = ctx.get_global_state()
        if ret != RET_OK or not (state.get('qot_logined') and state.get('trd_logined')):
            raise SystemExit(1)
    finally:
        ctx.close()
"""
    try:
        run([ROOT / "releases" / sha / ".venv/bin/python", "-c", probe,
             cfg.get("FUTU_HOST", "127.0.0.1"), cfg.get("FUTU_PORTS", "11111")],
            timeout=15, capture=True)
        return True
    except (RuntimeError, subprocess.TimeoutExpired):
        return False


def healthy(cfg, sha, *, full=False):
    try:
        health = request(cfg, "/healthz")
        ready = request(cfg, "/readyz")
        if not (health.get("ok") and health.get("release") == sha
                and ready.get("ok") and ready.get("release") == sha):
            return False
        if full:
            snapshot = request(cfg, "/api/snapshot")
            if not (isinstance(snapshot.get("panels"), list)
                    and isinstance(snapshot.get("versions"), dict)
                    and isinstance(snapshot.get("stock_codes"), list)):
                return False
            for path in ("/", "/static/app.js", "/static/styles.css", "/static/vendor/plotly-2.35.2.min.js"):
                if not request(cfg, path, False):
                    return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def wait_healthy(cfg, sha):
    deadline = time.monotonic() + HEALTH_TIMEOUT
    consecutive = 0
    while time.monotonic() < deadline:
        if healthy(cfg, sha, full=consecutive == 0):
            consecutive += 1
            if consecutive >= 3:
                return True
        else:
            consecutive = 0
        time.sleep(3)
    return False


def prepare(sha):
    release = ROOT / "releases" / checked_sha(sha)
    if (release / ".complete").is_file():
        return release
    if release.exists():
        if sha in {linked("current"), linked("previous")}:
            raise RuntimeError("Active release is incomplete; refusing to replace it")
        shutil.rmtree(release)
    release.mkdir()
    log(f"prepare {sha}")
    with tempfile.TemporaryDirectory(dir=DATA) as temp:
        archive = Path(temp) / "source.tar"
        run(["git", "--git-dir", REPO, "archive", "--format=tar", "-o", archive, sha])
        with tarfile.open(archive) as bundle:
            bundle.extractall(release, filter="data")
    lock = release / "requirements-web.lock"
    if not lock.is_file():
        raise RuntimeError("Commit has no requirements-web.lock")
    run(["/usr/bin/python3", "-m", "venv", release / ".venv"])
    python = release / ".venv/bin/python"
    run([python, "-m", "pip", "install", "--timeout", "30", "--retries", "2", "-r", lock], timeout=600)
    run([python, "-m", "pip", "check"])
    run([python, "-m", "compileall", "-q", "opdash_web.py", "backend.py", "core.py", "positions.py", "options.py"], cwd=release)
    run([python, "-c", "import opdash_web; assert callable(opdash_web.create_app)"], cwd=release, timeout=30)
    for asset in ("web/index.html", "web/app.js", "web/styles.css", "web/vendor/plotly-2.35.2.min.js"):
        if not (release / asset).is_file():
            raise RuntimeError("Missing asset: " + asset)
    (release / ".complete").write_text(sha + "\n")
    return release


def restore(state, cfg):
    transaction = state["transaction"]
    old = transaction["old"]
    log(f"restore {old or 'no previous release'}")
    control("stop")
    point("current", old)
    if old:
        control("start")
        if not wait_healthy(cfg, old):
            state["paused"] = True
            save(state)  # Preserve transaction for administrator recovery.
            raise RuntimeError("Rollback not ready; deployments paused")
    state.pop("transaction", None)
    save(state)


def activate(state, cfg, sha):
    old = linked("current")
    state["transaction"] = {"old": old, "target": sha}
    save(state)
    try:
        log(f"activate {sha} (previous {old})")
        control("stop")
        point("current", sha)
        control("start")
        if not wait_healthy(cfg, sha):
            raise RuntimeError("Candidate failed readiness within deadline")
        if old and old != sha:
            point("previous", old)
        state["successful"] = list(dict.fromkeys(state.get("successful", []) + [sha]))
        state["current"] = sha
        state["deployed_at"] = time.time()
        state.pop("transaction", None)
        state.pop("failed", None)
        save(state)
        log(f"deployment successful {sha}")
    except BaseException:
        # A termination request still restores the old service before exiting.
        if "transaction" in state:
            restore(state, cfg)
        raise


def clean(state):
    successes = state.get("successful", [])
    keep = set(successes[-3:]) | {linked("current"), linked("previous")}
    for path in (ROOT / "releases").iterdir():
        if path.name not in keep and re.fullmatch(r"[0-9a-f]{40}", path.name):
            shutil.rmtree(path)
    state["successful"] = [sha for sha in successes if sha in keep]
    save(state)


def update(state, cfg, retry):
    if state.get("paused"):
        log("automatic deployment paused")
        return
    if not REPO.exists():
        run(["git", "init", "--bare", REPO])
    run(["git", "--git-dir", REPO, "fetch", "--no-tags", URL,
         "+refs/heads/master:refs/heads/master"], timeout=90)
    sha = checked_sha(run(["git", "--git-dir", REPO, "rev-parse", "refs/heads/master"], capture=True))
    if sha == linked("current"):
        log(f"unchanged {sha}")
        return
    failure = state.get("failed", {})
    if not retry and failure.get("sha") == sha and time.time() < failure.get("retry_after", 0):
        log(f"cooldown {sha}")
        return
    stage = "prepare"
    try:
        prepare(sha)
        current = linked("current")
        if (not opend_available(cfg)
                or (current and not healthy(cfg, current))
                or (not current and not opend_logged_in(cfg, sha))):
            log(f"prepared {sha}; waiting for OpenD/current service readiness")
            return
        stage = "activate"
        activate(state, cfg, sha)
        clean(state)
    except Exception as error:
        state["failed"] = {"sha": sha, "stage": stage, "error": str(error),
                           "at": time.time(), "retry_after": time.time() + 1800}
        save(state)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["update", "pause", "resume", "rollback", "status"])
    parser.add_argument("sha", nargs="?")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    with (DATA / "deploy.lock").open("a") as lock:
        # Blocking lock makes manual rollback wait for an in-flight deployment.
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state()
        cfg = config()
        if args.action == "status":
            print(json.dumps(state, indent=2))
            return
        if args.action == "pause":
            state["paused"] = True
            save(state)
            return
        if args.action == "resume":
            state["paused"] = False
            save(state)
        if args.action == "update" and state.get("paused"):
            log("automatic deployment paused")
            return
        if "transaction" in state:
            if not opend_available(cfg):
                log("transaction recovery waiting for OpenD")
                return
            restore(state, cfg)
        if args.action == "rollback":
            sha = checked_sha(args.sha or linked("previous"))
            if sha not in state.get("successful", []):
                raise ValueError("Rollback target must be a retained successful release")
            state["paused"] = True
            save(state)
            activate(state, cfg, sha)
        elif args.action in {"update", "resume"}:
            update(state, cfg, args.retry)


def terminate(signum, frame):
    raise SystemExit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    try:
        main()
    except Exception as error:
        log(f"ERROR: {error}")
        raise SystemExit(1)
