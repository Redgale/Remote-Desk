#!/usr/bin/env python3
"""Build RemoteFunnel.html: a single-file launcher you can open from anywhere (file://, a USB
stick, a blob: tab). It loads the live client from your server and pre-fills its address.

    python3 make_launcher.py                       # uses the addresses setup saved, or asks
    python3 make_launcher.py desk.example.com other.ts.net
    python3 make_launcher.py --config path/to/.env -o ~/Desktop/RemoteFunnel.html

Addresses come from (first match wins): the command line, RD_PUBLIC_URLS in your RemoteFunnel
config (Linux: .env next to this script; macOS: ~/Library/Application Support/RemoteFunnel/
config.env; Windows: %LOCALAPPDATA%\\RemoteFunnel\\config.env), or a prompt.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "launcher", "RemoteFunnel.html")
MARKER = "/*RF_HOSTS*/"


def config_candidates():
    yield os.path.join(HERE, ".env")
    yield os.path.expanduser("~/Library/Application Support/RemoteFunnel/config.env")
    if os.environ.get("LOCALAPPDATA"):
        yield os.path.join(os.environ["LOCALAPPDATA"], "RemoteFunnel", "config.env")


def read_urls(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                m = re.match(r"\s*RD_PUBLIC_URLS\s*=\s*(.*)$", line)
                if m:
                    return split_hosts(m.group(1).strip().strip("'\""))
    except OSError:
        pass
    return []


def clean_host(h):
    """'https://desk.example.com/ws' -> 'desk.example.com' (ports are kept)."""
    h = h.strip()
    h = re.sub(r"^[a-z]+://", "", h, flags=re.I)
    return h.split("/")[0]


def split_hosts(text):
    hosts = []
    for part in re.split(r"[,\s]+", text):
        h = clean_host(part)
        if h and h not in hosts:
            hosts.append(h)
    return hosts


def main():
    ap = argparse.ArgumentParser(description="Build the single-file RemoteFunnel launcher.")
    ap.add_argument("hosts", nargs="*", help="addresses your desktop is published at")
    ap.add_argument("--config", help="RemoteFunnel config file to read RD_PUBLIC_URLS from")
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "RemoteFunnel.html"),
                    help="where to write the launcher (default: next to this script)")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask, use what was found")
    args = ap.parse_args()

    try:
        template = open(TEMPLATE, encoding="utf-8").read()
    except OSError:
        sys.exit(f"template not found: {TEMPLATE}")
    if MARKER not in template:
        sys.exit(f"{TEMPLATE} has no {MARKER} placeholder")

    hosts = split_hosts(" ".join(args.hosts))
    source = "command line"
    if not hosts:
        for path in ([args.config] if args.config else config_candidates()):
            hosts = read_urls(path)
            if hosts:
                source = path
                break
    interactive = sys.stdin.isatty() and not args.yes and not args.hosts
    if interactive:
        if hosts:
            print(f"Addresses from {source}:\n  " + "\n  ".join(hosts))
            answer = input("Press Enter to use them, or type the addresses to use instead: ").strip()
        else:
            print("No saved addresses found. Enter the hostname(s) your desktop is published at,")
            print("e.g. desk.example.com and/or name.tailnet.ts.net (space or comma separated).")
            answer = input("Addresses: ").strip()
        if answer:
            hosts = split_hosts(answer)
    if not hosts:
        print("warning: no addresses; the launcher will ask for one each time it opens", file=sys.stderr)

    html = template.replace(MARKER, ", ".join(json.dumps(h) for h in hosts))
    with open(args.output, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print(f"wrote {args.output}" + (f" for {', '.join(hosts)}" if hosts else ""))


if __name__ == "__main__":
    main()
