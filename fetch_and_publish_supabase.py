#!/usr/bin/env python3
"""
QCoach Studio v4.14 — Automated Supabase -> packs/*.json -> Supabase Published Bridge
Called by GitHub Actions (.github/workflows/sync-approved-songs.yml).
Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from environment secrets.
"""
import json
import os
import subprocess
import sys
import urllib.request

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""


def sb_request(method: str, path: str, payload=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else []


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY secrets.")
        sys.exit(1)

    approved = sb_request("GET", "community_song_submissions?status=eq.approved&select=*")
    if not approved:
        print("No approved songs waiting in Supabase queue. Nothing to do.")
        sys.exit(0)

    print(f"Found {len(approved)} approved song(s) in Supabase. Running sync pipeline...")
    queue_file = "/tmp/qcoach_approved_queue.json"
    with open(queue_file, "w", encoding="utf-8") as f:
        json.dump({"songs": approved}, f, indent=2, ensure_ascii=False)

    rc = subprocess.run([
        sys.executable, "scripts/process_approved_queue.py",
        "--queue", queue_file,
        "--packs-dir", "packs"
    ]).returncode

    if rc != 0:
        print("Pipeline or pack validation failed. Not marking songs as published.")
        sys.exit(rc)

    # Mark processed rows as 'published' in Supabase
    for row in approved:
        rid = row.get("id")
        if rid:
            sb_request("PATCH", f"community_song_submissions?id=eq.{rid}", {"status": "published"})
            print(f"Marked published in Supabase: {row.get('title')}")

    print("All approved songs processed, validated, and marked published!")


if __name__ == "__main__":
    main()
