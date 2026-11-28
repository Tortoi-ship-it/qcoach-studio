#!/usr/bin/env python3
"""
QCoach Studio v6.0.1 — Automated Supabase -> packs/community_catalog.json -> Supabase Published/Rejected Bridge
Called by GitHub Actions (.github/workflows/sync-approved-songs.yml).
Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from environment secrets.
Supports both Supabase's new `sb_secret_...` keys (apikey header only) and legacy `eyJ...` service_role JWTs.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request


def _clean_url(raw: str) -> str:
    s = (raw or "").strip().strip('"').strip("'")
    # Handle accidental Markdown links like [https://foo.supabase.co](https://foo.supabase.co)
    m = re.search(r"https?://[^\s\)\]\>\"']+", s)
    if m:
        s = m.group(0)
    return s.rstrip("/")


SUPABASE_URL = _clean_url(os.environ.get("SUPABASE_URL") or "")
SUPABASE_KEY = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip().strip('"').strip("'")


def _build_headers() -> dict:
    headers = {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
        "User-Agent": "QCoach-Auto-Publisher/6.0",
    }
    # Supabase legacy JWT keys (eyJ...) require Authorization: Bearer;
    # new opaque secret keys (sb_secret_...) are NOT JWTs and must ONLY be sent in the `apikey` header.
    if SUPABASE_KEY.startswith("eyJ") and SUPABASE_KEY.count(".") == 2:
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    return headers


def sb_request(method: str, path: str, payload=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=_build_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else []
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="ignore")[:500]
        except Exception:
            pass
        key_kind = (
            "sb_secret" if SUPABASE_KEY.startswith("sb_secret_")
            else "sb_publishable (WRONG KEY!)" if SUPABASE_KEY.startswith("sb_publishable_")
            else "legacy_jwt" if SUPABASE_KEY.startswith("eyJ")
            else "unknown_format"
        )
        print(f"[SUPABASE ERROR] {method} {path} -> HTTP {e.code} ({e.reason}) | key_type={key_kind}")
        if err_body:
            print(f"[SUPABASE RESPONSE BODY] {err_body}")
        if e.code in (401, 403):
            print(
                "[HINT] Verify that GitHub Secret SUPABASE_SERVICE_ROLE_KEY contains your Supabase "
                "Secret key (starts with sb_secret_...) or legacy service_role JWT (starts with eyJ...), "
                "NOT the Publishable/anon key."
            )
        sys.exit(1)


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY secrets.")
        sys.exit(1)

    if SUPABASE_KEY.startswith("sb_publishable_"):
        print(
            "[CONFIG ERROR] SUPABASE_SERVICE_ROLE_KEY is set to a Publishable key (sb_publishable_...). "
            "Replace it in GitHub Secrets with your Supabase Secret key (sb_secret_...) from "
            "Supabase Dashboard -> Project Settings -> API Keys."
        )
        sys.exit(1)

    approved = sb_request("GET", "community_song_submissions?status=eq.approved&select=*")
    if not approved:
        print("No approved songs waiting in Supabase queue. Nothing to do.")
        sys.exit(0)

    print(f"Found {len(approved)} approved song(s) in Supabase. Running fuzzy multi-signal sync pipeline...")
    queue_file = "/tmp/qcoach_approved_queue.json"
    report_file = "/tmp/qcoach_sync_report.json"
    with open(queue_file, "w", encoding="utf-8") as f:
        json.dump({"songs": approved}, f, indent=2, ensure_ascii=False)

    rc = subprocess.run([
        sys.executable, "scripts/process_approved_queue.py",
        "--queue", queue_file,
        "--packs-dir", "packs",
        "--report-out", report_file,
    ]).returncode

    if rc != 0:
        print("Pipeline or pack validation failed. Not updating Supabase statuses.")
        sys.exit(rc)

    results_by_id = {}
    if os.path.exists(report_file):
        with open(report_file, "r", encoding="utf-8") as rf:
            rep = json.load(rf)
            for item in rep.get("results", []):
                if item.get("id"):
                    results_by_id[item["id"]] = item

    # Update Supabase status per song: 'published' for added/duplicate, 'rejected' for quarantined
    for row in approved:
        rid = row.get("id")
        if not rid:
            continue
        outcome = results_by_id.get(rid, {"status": "added"})
        if outcome.get("status") == "quarantined":
            prev_payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            prev_payload["rejectReason"] = outcome.get("reason", "Low confidence match")
            sb_request("PATCH", f"community_song_submissions?id=eq.{rid}", {
                "status": "rejected",
                "payload": prev_payload,
            })
            print(f"Quarantined -> marked rejected in Supabase: {row.get('title')} ({prev_payload['rejectReason']})")
        else:
            sb_request("PATCH", f"community_song_submissions?id=eq.{rid}", {"status": "published"})
            print(f"Marked published in Supabase: {row.get('title')}")

    print("All approved songs processed, validated, and synced with Supabase!")


if __name__ == "__main__":
    main()
