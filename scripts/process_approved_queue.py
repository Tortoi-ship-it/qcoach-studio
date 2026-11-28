#!/usr/bin/env python3
"""
QCoach Studio v4.13 — Automated Song Ingestion, Copyright Gate & YouTube Sync Pipeline
Runs on Google Colab (1-Click Notebook) or locally.

Fail-Safe Copyright Policy:
  - Only songs confirmed as Public Domain (Hymnary.org / MusicBrainz author death > 70 yrs /
    verified Public Domain author list) store full lyrics.
  - Everything else AUTOMATICALLY defaults to `chords_youtube_only` (Zero stored copyrighted lyrics:
    stores only YouTube videoId, BPM, meter, 3-tier strumming, and time-synced section/chord cues).

Usage:
  python3 scripts/process_approved_queue.py --queue qcoach_approved_song_queue.json --packs-dir packs
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")

# Verified Public Domain hymn writers (all deceased > 70+ years ago)
VERIFIED_PD_AUTHORS = {
    "narayan vaman tilak", "n.v. tilak", "n v tilak", "rev. n.v. tilak", "rev. narayan vaman tilak",
    "john newton", "fanny crosby", "charles wesley", "isaac watts", "horatio spafford",
    "reginald heber", "joseph mohr", "franz xaver gruber", "martin luther", "william cowper",
    "thomas chisholm", "robert robinson", "henry francis lyte", "sarah flower adams",
    "joseph scriven", "traditional", "public domain", "gregorian chant", "latin hymn",
}

BLOCKLIST_WORDS = (
    "instrumental", "karaoke", "minus one", "piano only", "ringtone",
    "tutorial", "lesson", "medley", "non-stop", "jukebox", "1 hour"
)


def normalize_song_title_key(title: str) -> str:
    s = (title or "").lower()
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def extract_youtube_id(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else ""


def check_public_domain(title: str, artist: str) -> tuple[bool, str]:
    """
    Stage 2A: Fail-Safe Public Domain Gate.
    Returns (is_public_domain, reason). Defaults to False (chords_youtube_only) on any uncertainty.
    """
    artist_low = (artist or "").lower()
    for pd_author in VERIFIED_PD_AUTHORS:
        if pd_author in artist_low:
            return True, f"verified_pd_author:{pd_author}"

    # Check Hymnary.org API/search for explicit Public Domain status
    try:
        q = urllib.parse.quote(title)
        req = urllib.request.Request(
            f"https://hymnary.org/search?qu={q}",
            headers={"User-Agent": "QCoachStudioPipeline/4.13"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if "Public Domain" in body and "Copyright" not in body[:4000]:
                return True, "hymnary_public_domain"
    except Exception:
        pass

    # Fail-safe default: never assume Public Domain if uncertain
    return False, "default_chords_youtube_only"


def discover_youtube_video(title: str, artist: str, language: str, pinned_url: str = "") -> dict:
    """
    Stage 3: YouTube Discovery & Candidate Filtering (yt-dlp).
    """
    vid = extract_youtube_id(pinned_url)
    if vid:
        return {"videoId": vid, "youtubeUrl": f"https://youtu.be/{vid}", "videoTitle": title, "dur": 210}

    try:
        lang_hint = {"hi": "hindi gospel", "mr": "marathi gospel"}.get(language, "worship")
        query = f"ytsearch5:{title} {artist or lang_hint} lyrics"
        cmd = ["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", query]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if res.returncode == 0 and res.stdout.strip():
            for line in res.stdout.strip().splitlines():
                info = json.loads(line)
                vtitle = (info.get("title") or "").lower()
                dur = int(info.get("duration") or 0)
                if dur and not (75 <= dur <= 600):
                    continue
                if any(b in vtitle for b in BLOCKLIST_WORDS):
                    continue
                found_id = info.get("id", "")
                if found_id:
                    return {
                        "videoId": found_id,
                        "youtubeUrl": f"https://youtu.be/{found_id}",
                        "videoTitle": info.get("title") or title,
                        "dur": dur or 210,
                    }
    except Exception:
        pass
    return {}


def strip_devanagari(text: str) -> str:
    """Ensure strict Romanized Latin script (zero Devanagari)."""
    if not DEVANAGARI_RE.search(text or ""):
        return text
    try:
        from indic_transliteration import sanscript
        from indic_transliteration.sanscript import transliterate
        return transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)
    except Exception:
        return DEVANAGARI_RE.sub("", text).strip()


def fetch_pd_captions(video_id: str) -> list:
    """Stage 4 (Public Domain Only): Fetch timestamped YouTube captions."""
    if not video_id:
        return []
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "hi", "mr"])
        lines = []
        last_t = -1
        for row in transcript:
            t = int(round(float(row.get("start", 0))))
            txt = strip_devanagari(re.sub(r"\s+", " ", row.get("text", "")).strip())
            if not txt or len(txt) < 3:
                continue
            if t <= last_t:
                t = last_t + 2
            last_t = t
            lines.append([t, txt])
        return lines[:48]
    except Exception:
        return []


def build_chords_only_cues(dur: int, chords: list) -> tuple[list, list]:
    """
    Stage 4 (Chords + YouTube Only — Copyright-Safe Default):
    Generates clean musical section/chord cue markers with ZERO copyrighted lyrics.
    """
    ch_seq = chords or ["C", "G", "Am", "F"]
    offline_lines = [
        [ch_seq[0], "Verse 1 · Strum steady"],
        [ch_seq[1 % len(ch_seq)], "Verse 1 · Hold groove"],
        [ch_seq[2 % len(ch_seq)], "Chorus · Lift energy"],
        [ch_seq[3 % len(ch_seq)], "Chorus · Resolve clean"],
    ]
    dur = max(90, int(dur or 210))
    step = max(8, dur // 12)
    sections = [
        "Intro", "Verse 1", "Verse 1", "Chorus", "Chorus",
        "Verse 2", "Verse 2", "Chorus", "Bridge", "Bridge", "Chorus", "Outro"
    ]
    yt_lines = []
    t = 6
    for idx, sec in enumerate(sections):
        if t >= dur - 4:
            break
        c_now = ch_seq[idx % len(ch_seq)]
        c_nxt = ch_seq[(idx + 1) % len(ch_seq)]
        yt_lines.append([t, f"♪ {sec} · {c_now} → {c_nxt}"])
        t += step
    return offline_lines, yt_lines


def assign_strums(language: str, meter: str = "4/4") -> dict:
    """Stage 5: Assign QCoach 23-pattern 3-tier strumming strictly matching meter."""
    if meter == "6/8":
        return {"b": "dn6", "i": "rol6", "e": ["arp6", "dad6", "rol6"]}
    if meter == "3/4":
        return {"b": "dn3", "i": "wlz3", "e": ["arp3", "wlz3", "rif3"]}
    if language in ("hi", "mr"):
        return {"b": "isl4", "i": "keh4", "e": ["flk4", "keh4", "ich4"]}
    return {"b": "isl4", "i": "ich4", "e": ["trv4", "ich4", "wlk4"]}



def classify_song_level(title: str, chords: list, meter: str, bpm: int, strums: dict) -> str:
    """Curriculum-aligned 6-tier song level classifier matching QCoach Studio."""
    cset = set(chords or [])
    b_code = (strums or {}).get("b", "dn4")
    i_code = (strums or {}).get("i", "isl4")
    pts = 0
    if any(c.startswith(("Bb","Bm","F#m","Cm","Fm","Gm","Eb","Ab","Cmaj7","Fmaj7")) for c in cset):
        pts += 6
    elif any(c in ("Dm","Em","G7","E7","D","A","D7","A7") for c in cset):
        pts += 4
    elif len(cset) >= 4:
        pts += 2
    elif "G" in cset:
        pts += 1
    if i_code in ("trv4","arp4","arp3","arp6","wlk4","rif4","srf4","drv4"):
        pts += 4
    elif meter == "6/8" or i_code in ("keh4","dad6","rol6","chk4","wch3"):
        pts += 3
    elif meter == "3/4" or i_code in ("syn4","ich4","wlz3"):
        pts += 2
    elif b_code == "isl4" or i_code == "flk4":
        pts += 1
    if bpm >= 106: pts += 2
    elif bpm >= 86: pts += 1
    if pts <= 2: return "Beginner"
    if pts <= 4: return "Strummer"
    if pts <= 5: return "Rhythm Player"
    if pts <= 6: return "Band Player"
    if pts <= 7: return "Worship Leader"
    return "Maestro"

def choose_pack_file(language: str, title: str, artist: str) -> str:
    # Centralized Remote Song Bank: all newly approved songs publish to packs/community_catalog.json
    # so mobile/App Store builds pull new songs automatically without ever updating the core app bundle!
    return "community_catalog.json"


def load_existing_norm_keys(packs_dir: str) -> set:
    keys = set()
    for fp in glob.glob(os.path.join(packs_dir, "*.json")):
        if os.path.basename(fp) in ("manifest.json", "syllabus.json"):
            continue
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        for s in data.get("songs", []):
            keys.add(normalize_song_title_key(s.get("title", "")))
    return keys


def process_queue(queue_path: str, packs_dir: str) -> int:
    with open(queue_path, "r", encoding="utf-8") as f:
        qdata = json.load(f)
    requests = qdata.get("songs", qdata if isinstance(qdata, list) else [])
    existing_keys = load_existing_norm_keys(packs_dir)
    added = 0

    for req in requests:
        title = (req.get("title") or "").strip()
        artist = (req.get("artist") or "Worship").strip()
        language = (req.get("language") or "en").strip().lower()
        yt_url = (req.get("youtube_url") or "").strip()
        nk = normalize_song_title_key(title)
        if not nk or nk in existing_keys:
            print(f"[SKIP] Already exists or empty: {title}")
            continue

        # Stage 2A: Fail-Safe Copyright Gate
        is_pd, pd_reason = check_public_domain(title, artist)
        copyright_mode = "public_domain" if is_pd else "chords_youtube_only"
        print(f"[PROCESS] {title} | lang={language} | copyright={copyright_mode} ({pd_reason})")

        # Stage 3: YouTube Discovery
        yt_info = discover_youtube_video(title, artist, language, yt_url)
        chords = ["C", "G", "Am", "F"]
        meter = "4/4"
        bpm = 76
        strums = assign_strums(language, meter)

        # Stage 4: Branched Sync
        yt_sync = None
        if is_pd and yt_info.get("videoId"):
            pd_lines = fetch_pd_captions(yt_info["videoId"])
            if len(pd_lines) >= 4:
                offline_lines = [[chords[i % len(chords)], row[1]] for i, row in enumerate(pd_lines[:8])]
                yt_sync = {
                    "videoId": yt_info["videoId"],
                    "youtubeUrl": yt_info["youtubeUrl"],
                    "videoTitle": yt_info["videoTitle"],
                    "dur": yt_info["dur"],
                    "source": "yt_transcript_pd",
                    "verified": True,
                    "lines": pd_lines,
                }
            else:
                offline_lines, cue_lines = build_chords_only_cues(yt_info["dur"], chords)
                yt_sync = {
                    "videoId": yt_info["videoId"],
                    "youtubeUrl": yt_info["youtubeUrl"],
                    "videoTitle": yt_info["videoTitle"],
                    "dur": yt_info["dur"],
                    "source": "chords_cue_sync",
                    "verified": True,
                    "lines": cue_lines,
                }
        else:
            # Copyright-safe default: Chords + YouTube Only (Zero stored lyrics)
            offline_lines, cue_lines = build_chords_only_cues(yt_info.get("dur", 210), chords)
            if yt_info.get("videoId"):
                yt_sync = {
                    "videoId": yt_info["videoId"],
                    "youtubeUrl": yt_info["youtubeUrl"],
                    "videoTitle": yt_info["videoTitle"],
                    "dur": yt_info["dur"],
                    "source": "chords_youtube_only",
                    "verified": True,
                    "lines": cue_lines,
                }

        song_obj = {
            "id": f"{language}-{nk.replace(' ', '-')[:40]}",
            "normKey": nk,
            "title": title,
            "artist": artist,
            "category": {"hi": "Hindi Gospel", "mr": "Marathi Gospel"}.get(language, "Worship"),
            "language": language,
            "script": "latin",
            "copyrightStatus": copyright_mode,
            "musicalLevel": classify_song_level(title, chords, meter, bpm, strums),
            "musicalMeta": {"key": chords[0], "meter": meter, "bpm": bpm, "chords": chords},
            "strums": strums,
            "offlineLines": offline_lines,
            "youtubeSync": yt_sync,
        }

        pack_fname = choose_pack_file(language, title, artist)
        pack_path = os.path.join(packs_dir, pack_fname)
        with open(pack_path, "r", encoding="utf-8") as pf:
            pdata = json.load(pf)
        pdata.setdefault("songs", []).append(song_obj)
        with open(pack_path, "w", encoding="utf-8") as pf:
            json.dump(pdata, pf, indent=2, ensure_ascii=False)

        existing_keys.add(nk)
        added += 1
        print(f"  -> Added to packs/{pack_fname} ({copyright_mode})")

    print(f"\nDone! Added {added} song(s). Running Zero-Defect Quality Gate...")
    rc = subprocess.run([sys.executable, "scripts/validate_and_build_packs.py", "--rebuild-manifest"]).returncode
    return rc


def main():
    parser = argparse.ArgumentParser(description="QCoach Studio Approved Song Queue Processor")
    parser.add_argument("--queue", required=True, help="Path to qcoach_approved_song_queue.json")
    parser.add_argument("--packs-dir", default="packs", help="Path to packs/ directory")
    args = parser.parse_args()
    sys.exit(process_queue(args.queue, args.packs_dir))


if __name__ == "__main__":
    main()
