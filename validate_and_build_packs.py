#!/usr/bin/env python3
"""
QCoach Studio v3.0 — Song Pack Validator & Manifest Builder
Run locally or via GitHub Actions (.github/workflows/validate-packs.yml).

Usage:
  python scripts/validate_and_build_packs.py --check
  python scripts/validate_and_build_packs.py --rebuild-manifest
"""
import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

VALID_PALETTE_SLOTS = {
    # 4/4 (8 slots)
    "dn4": 8, "isl4": 8, "bal4": 8, "syn4": 8, "flk4": 8, "ich4": 8,
    "chk4": 8, "drv4": 8, "trv4": 8, "arp4": 8, "rif4": 8, "wlk4": 8,
    "srf4": 8, "keh4": 8,
    # 3/4 & 6/8 (6 slots)
    "dn3": 6, "wlz3": 6, "wch3": 6, "arp3": 6, "rif3": 6,
    "dn6": 6, "rol6": 6, "arp6": 6, "dad6": 6,
}

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def normalize_song_title_key(title: str) -> str:
    s = (title or "").lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def validate_packs(packs_dir: str, rebuild_manifest: bool = False) -> int:
    pack_files = sorted(
        p for p in glob.glob(os.path.join(packs_dir, "*.json"))
        if os.path.basename(p) not in ("manifest.json", "syllabus.json")
    )
    errors = []
    seen_keys = set()
    manifest_entries = []
    total_songs = 0
    total_verified_yt = 0

    for fp in pack_files:
        fname = os.path.basename(fp)
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        songs = data.get("songs", [])
        if not isinstance(songs, list):
            errors.append(f"{fname}: 'songs' must be a list")
            continue

        for idx, s in enumerate(songs):
            total_songs += 1
            title = s.get("title", "")
            nk = normalize_song_title_key(title)
            if not nk:
                errors.append(f"{fname}[{idx}]: missing title")
                continue
            if nk in seen_keys:
                errors.append(f"{fname}[{idx}]: duplicate normKey '{nk}' ({title})")
            seen_keys.add(nk)

            mm = s.get("musicalMeta") or {}
            meter = mm.get("meter", "4/4")
            expected_slots = 6 if meter in ("3/4", "6/8") else 8

            strums = s.get("strums")
            if not isinstance(strums, dict):
                errors.append(f"{fname} '{title}': missing 3-tier strums object")
            else:
                codes = [strums.get("b"), strums.get("i")] + list(strums.get("e") or [])
                if len(codes) < 5:
                    errors.append(f"{fname} '{title}': strums must define b, i, and 3 expert codes")
                for c in codes:
                    if c not in VALID_PALETTE_SLOTS:
                        errors.append(f"{fname} '{title}': invalid strum code '{c}'")
                    elif VALID_PALETTE_SLOTS[c] != expected_slots:
                        errors.append(f"{fname} '{title}': meter {meter} expects {expected_slots} slots, got '{c}'")

            offline = s.get("offlineLines") or []
            if not offline:
                errors.append(f"{fname} '{title}': missing offlineLines for offline playback")

            if s.get("script", "latin") == "latin":
                for row in offline:
                    txt = " ".join(row) if isinstance(row, list) else str(row)
                    if DEVANAGARI_RE.search(txt):
                        errors.append(f"{fname} '{title}': Devanagari found in latin offlineLines")

            yt = s.get("youtubeSync")
            if yt:
                lines = yt.get("lines") or []
                if len(lines) < 4:
                    errors.append(f"{fname} '{title}': youtubeSync.lines has fewer than 4 lines")
                else:
                    total_verified_yt += 1
                    for i in range(1, len(lines)):
                        if float(lines[i][0]) <= float(lines[i - 1][0]):
                            errors.append(f"{fname} '{title}': non-monotonic timestamp at index {i}")
                    if s.get("script", "latin") == "latin":
                        for _, txt in lines:
                            if DEVANAGARI_RE.search(str(txt)):
                                errors.append(f"{fname} '{title}': Devanagari found in latin youtubeSync")

        manifest_entries.append({
            "id": os.path.splitext(fname)[0],
            "name": data.get("packName", fname),
            "language": data.get("language", "en"),
            "songCount": len(songs),
            "url": f"./packs/{fname}"
        })


    # Validate packs/syllabus.json if present
    syllabus_fp = os.path.join(packs_dir, "syllabus.json")
    if os.path.exists(syllabus_fp):
        with open(syllabus_fp, "r", encoding="utf-8") as sf:
            syl = json.load(sf)
        lib = syl.get("drillLibrary") or {}
        units = syl.get("guidedUnits") or []
        all_drill_ids = {}
        for pane, items in lib.items():
            for d in items:
                did = d.get("id")
                if not did:
                    errors.append(f"syllabus.json ({pane}): drill missing id")
                    continue
                if did in all_drill_ids:
                    errors.append(f"syllabus.json: duplicate drill id '{did}'")
                all_drill_ids[did] = d
                sc = d.get("strumCode")
                if sc and sc not in VALID_PALETTE_SLOTS:
                    errors.append(f"syllabus.json '{did}': invalid strumCode '{sc}'")
        for u in units:
            for lid in u.get("lessonIds") or []:
                if lid not in all_drill_ids:
                    errors.append(f"syllabus.json Unit {u.get('unit')}: missing lessonId '{lid}'")
        print(f"Validated syllabus.json: {len(units)} units, {len(all_drill_ids)} drills across {len(lib)} panes.")

    if rebuild_manifest and not errors:
        manifest = {
            "schemaVersion": "3.0",
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "packs": manifest_entries
        }
        with open(os.path.join(packs_dir, "manifest.json"), "w", encoding="utf-8") as mf:
            json.dump(manifest, mf, indent=2)
        print(f"Rebuilt manifest.json ({len(manifest_entries)} packs, {total_songs} songs).")

    print(f"Validated {len(pack_files)} pack(s) | {total_songs} unique songs | {total_verified_yt} YouTube-synced | {len(errors)} error(s).")
    if errors:
        for e in errors[:25]:
            print("  ERROR:", e)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="Validate QCoach Studio JSON Song Packs")
    parser.add_argument("--packs-dir", default="packs", help="Path to packs directory")
    parser.add_argument("--check", action="store_true", help="Validate packs only")
    parser.add_argument("--rebuild-manifest", action="store_true", help="Validate and rebuild packs/manifest.json")
    args = parser.parse_args()
    sys.exit(validate_packs(args.packs_dir, rebuild_manifest=args.rebuild_manifest))


if __name__ == "__main__":
    main()
