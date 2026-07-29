"""Self-healing watchdog for the coach + tunnel containers.

`restart: unless-stopped` (docker-compose.yml) only recovers a container that
actually EXITS. It does nothing for one that stays "Up" but is functionally
broken — e.g. cloudflared silently loses its tunnel registration, or uvicorn
hangs — because Docker's built-in HEALTHCHECK only *reports* status, it never
acts on it without something watching. This is that watcher: it polls each
service's own health endpoint over the compose network and restarts the
container via the Docker Engine API after enough consecutive failures,
notifying ADMIN_LINE_USER_ID over LINE when it does.

Added after the 2026-07-28 incident where the tunnel silently disconnected
(Cloudflare error 1033 — no active connector) and stayed that way until the
owner noticed LINE messages were failing; fixing it needed a manual restart.

Run as its own compose service (see `watchdog` in docker-compose.yml) sharing
the coach image with COMMAND overridden to `python -m coach.watchdog`.
Requires the Docker socket mounted to issue restarts.
"""

import logging
import os
import time

import docker
import requests

from coach.config import ADMIN_LINE_USER_ID
from coach.line import send_text

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = int(os.environ.get("WATCHDOG_INTERVAL_SECONDS", "30"))
FAILURE_THRESHOLD = int(os.environ.get("WATCHDOG_FAILURE_THRESHOLD", "3"))  # consecutive failures before restarting
RESTART_COOLDOWN_SECONDS = int(os.environ.get("WATCHDOG_RESTART_COOLDOWN_SECONDS", "600"))  # don't re-restart within this window
# Grace period after EACH restart (including the watchdog's own startup) before
# failures count at all — cloudflared/uvicorn take a few seconds to come up,
# and without this a slow-starting container gets restarted before it ever
# gets a chance to become healthy (caught during local testing: a fresh
# cloudflared quick tunnel wasn't ready for ~8s, well past a tight interval).
START_GRACE_SECONDS = int(os.environ.get("WATCHDOG_START_GRACE_SECONDS", "60"))

# container_name (docker-compose.yml) -> health endpoint reachable on the compose network.
# tunnel's /ready comes from cloudflared's own --metrics server (confirmed
# live: returns 200 with {"readyConnections": N} once registered, otherwise
# connection-refused/non-200 — exactly what error 1033 looks like from here).
CHECKS = {
    "coach": "http://coach:8080/healthz",
    "tunnel": "http://tunnel:60123/ready",
}


def _healthy(url: str) -> bool:
    try:
        return requests.get(url, timeout=5).status_code == 200
    except requests.RequestException:
        return False


def _notify(text: str) -> None:
    if not ADMIN_LINE_USER_ID:
        log.warning("ADMIN_LINE_USER_ID not set — skipping LINE notification: %s", text)
        return
    try:
        send_text(text, to=ADMIN_LINE_USER_ID)
    except Exception:
        log.exception("failed to send watchdog notification")


def run() -> None:
    client = docker.from_env()
    fail_counts = {name: 0 for name in CHECKS}
    last_restart = {name: 0.0 for name in CHECKS}
    grace_until = {name: time.monotonic() + START_GRACE_SECONDS for name in CHECKS}

    log.info("watchdog started: checking %s every %ds (start grace %ds)",
             list(CHECKS), CHECK_INTERVAL_SECONDS, START_GRACE_SECONDS)

    while True:
        for name, url in CHECKS.items():
            if _healthy(url):
                fail_counts[name] = 0
                continue

            if time.monotonic() < grace_until[name]:
                log.info("%s check failed but still within start grace — not counting", name)
                continue

            fail_counts[name] += 1
            log.warning("%s health check failed (%d/%d)", name, fail_counts[name], FAILURE_THRESHOLD)
            if fail_counts[name] < FAILURE_THRESHOLD:
                continue

            now = time.monotonic()
            if now - last_restart[name] < RESTART_COOLDOWN_SECONDS:
                continue  # already tried recently — avoid restart-looping on a persistent problem

            try:
                client.containers.get(name).restart(timeout=10)
                last_restart[name] = now
                grace_until[name] = now + START_GRACE_SECONDS
                log.warning("restarted %s after %d consecutive failed checks", name, fail_counts[name])
                _notify(f"⚠️ {name} was unresponsive — restarted it automatically.")
            except Exception:
                log.exception("failed to restart %s", name)
            finally:
                fail_counts[name] = 0

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
