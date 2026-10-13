"""RemoteFunnel watchdog for Windows (the "system" install).

Started at boot by a Scheduled Task running as SYSTEM, in session 0. It keeps the agent
(app.py) running *inside the active console session* with a SYSTEM token - the same arrangement
Parsec and Sunshine use - so the agent can show and control the sign-in screen, the lock screen,
UAC prompts and elevated windows. When the console session changes (log off, user switch) the
agent is restarted in the new session. It also keeps cloudflared running, so the tunnel stays up
while the agent moves between sessions.

    pythonw winsvc.py --config C:\\ProgramData\\RemoteFunnel\\config.env
"""
import argparse
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import winsys  # noqa: E402
from app import load_config  # noqa: E402

log = logging.getLogger("rd.watchdog")


def run_cloudflared(stop):
    token = os.environ.get("CF_TUNNEL_TOKEN", "")
    exe = os.environ.get("RD_CLOUDFLARED") or shutil.which("cloudflared")
    if not token or not exe:
        return
    port = os.environ.get("RD_PORT", "47913")
    env = dict(os.environ, TUNNEL_TOKEN=token)
    while not stop.is_set():
        log.info("starting cloudflared")
        p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "run", "--url", f"http://127.0.0.1:{port}"],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        while p.poll() is None and not stop.is_set():
            time.sleep(1)
        if p.poll() is None:
            p.kill()
        log.warning("cloudflared exited (%s)", p.returncode)
        stop.wait(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    load_config(args.config)
    logdir = os.path.dirname(os.path.abspath(args.config))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        filename=os.path.join(logdir, "watchdog.log"))
    # needed to put a SYSTEM token into another session and start processes with it
    winsys.enable_privileges("SeTcbPrivilege", "SeAssignPrimaryTokenPrivilege", "SeIncreaseQuotaPrivilege")

    stop = threading.Event()
    threading.Thread(target=run_cloudflared, args=(stop,), daemon=True).start()

    # the agent must not run its own cloudflared, and should know it has SYSTEM powers
    os.environ["RD_RUN_CLOUDFLARED"] = "0"
    os.environ["RD_SYSTEM_AGENT"] = "1"
    os.environ.setdefault("RD_LOG", os.path.join(logdir, "remotefunnel.log"))
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    cmd = [pythonw, os.path.join(HERE, "app.py"), "--config", os.path.abspath(args.config)]

    child, failures, started = None, 0, 0.0
    while True:
        session = winsys.active_console_session()
        if child and (session != child.session or child.wait(0)):
            exited = child.wait(0)
            log.info("agent in session %s %s", child.session,
                     "exited" if exited else f"replaced (console is now session {session})")
            child.kill()
            child = None
            if exited and time.monotonic() - started < 10:     # crashing on start: back off
                failures += 1
                time.sleep(min(30, 2 * failures))
                continue
        if child is None and session is not None:
            try:
                child = winsys.SessionProcess(cmd, session, cwd=HERE)
                started = time.monotonic()
                log.info("agent started in session %s (pid %s)", session, child.pid)
            except OSError as ex:
                failures += 1
                log.error("could not start the agent in session %s: %s", session, ex)
                time.sleep(min(30, 2 * failures))
                continue
        if child:
            if not child.wait(2000) and time.monotonic() - started > 60:
                failures = 0                                      # it has been stable for a while
        else:
            time.sleep(1)


if __name__ == "__main__":
    main()
