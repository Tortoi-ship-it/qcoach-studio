#!/usr/bin/env python3
"""
QCoach Studio v5.11 — Automated Song Ingestion, Fuzzy Multi-Signal Identification,
Copyright Gate & YouTube Sync Pipeline.

Features:
  1. Fuzzy Multi-Signal Song Identification (handles typos, case differences, wrong artist names,
     broken/missing YouTube links, and pasted lyric phrases).
  2. Confidence Gate & Quarantine: ignores / rejects junk or zero-match requests so the
     centralized Song Bank (packs/community_catalog.json) stays clean.
  3. Fail-Safe Copyright Policy:
     - Only songs confirmed as Public Domain store full lyrics.
     - Everything else AUTOMATICALLY defaults to `chords_youtube_only` (zero stored lyrics).

Usage:
  python3 scripts/process_approved_queue.py --queue qcoach_approved_song_queue.json --packs-dir packs [--report-out report.json]
"""
import argparse
import difflib
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
    "tutorial", "lesson", "medley", "non-stop", "jukebox", "1 hour",
    "reaction", "unboxing", "podcast", "sermon", "full album"
)

STOP_WORDS = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "with", "by", "at",
    "official", "video", "lyric", "lyrics", "audio", "hd", "4k", "song", "worship",
    "hymn", "gospel", "music", "live", "version", "ft", "feat", "new", "full"
}

MIN_MATCH_CONFIDENCE = 0.50


def normalize_song_title_key(title: str) -> str:
    s = (title or "").lower()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_keep_parens(text: str) -> str:
    s = (text or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokenize_clean(text: str) -> list[str]:
    norm = normalize_keep_parens(text)
    return [tok for tok in norm.split() if len(tok) >= 2 and tok not in STOP_WORDS]


def fuzzy_similarity(a: str, b: str) -> float:
    """
    Combines character-level SequenceMatcher ratio with typo-tolerant token overlap.
    Preserves words inside parentheses like '(Where Feet May Fail)' or '(My Chains Are Gone)'.
    Handles spelling mistakes ('Amazng Grce' vs 'Amazing Grace'), case, and extra YouTube tags.
    """
    na = normalize_keep_parens(a)
    nb = normalize_keep_parens(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.96

    ta = tokenize_clean(a)
    tb = tokenize_clean(b)
    if not ta or not tb:
        return difflib.SequenceMatcher(None, na, nb).ratio()

    # Best fuzzy token match for each query token against candidate tokens
    matched_sum = 0.0
    for qa in ta:
        best_tok = max((difflib.SequenceMatcher(None, qa, cb).ratio() for cb in tb), default=0.0)
        if best_tok >= 0.72:
            matched_sum += best_tok
    token_recall = matched_sum / len(ta)

    seq_ratio = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return round(max(token_recall, (token_recall * 0.7) + (seq_ratio * 0.3)), 3)


def clean_youtube_song_title(raw_title: str, fallback: str) -> str:
    """Strips noisy YouTube suffixes like '(Official Lyric Video)' or '| Worship Song'."""
    t = (raw_title or "").strip()
    if not t:
        return fallback
    t = re.sub(r"\s*[\(\[](?:official|lyric|lyrics|audio|hd|4k|live|video|music video|worship)[^\)\]]*[\)\]]", "", t, flags=re.I)
    t = re.split(r"\s*[\|•]\s*", t)[0].strip()
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—")
    return t if len(t) >= 2 else fallback


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

    try:
        q = urllib.parse.quote(title)
        req = urllib.request.Request(
            f"https://hymnary.org/search?qu={q}",
            headers={"User-Agent": "QCoachStudioPipeline/5.11"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if "Public Domain" in body and "Copyright" not in body[:4000]:
                return True, "hymnary_public_domain"
    except Exception:
        pass

    return False, "default_chords_youtube_only"


def score_candidate(req_title: str, req_artist: str, lyric_hint: str, cand_title: str, cand_channel: str, cand_desc: str = "") -> tuple[float, dict]:
    """
    Multi-signal confidence scorer:
      - Signal 1: Fuzzy title similarity (handles typos & case)
      - Signal 2: Fuzzy artist similarity against title + channel (handles artist typos / wrong artist)
      - Signal 3: Lyric phrase match against title + description
    """
    t_score = fuzzy_similarity(req_title, cand_title)
    a_score = 0.0
    if req_artist and req_artist.lower() not in ("worship", "unknown", "traditional"):
        a_score = max(
            fuzzy_similarity(req_artist, cand_channel),
            fuzzy_similarity(req_artist, cand_title),
        )
    l_score = 0.0
    if lyric_hint:
        l_score = max(
            fuzzy_similarity(lyric_hint, cand_title),
            fuzzy_similarity(lyric_hint, cand_desc[:600]) if cand_desc else 0.0,
        )

    # Combine signals so:
    # - Strong title match rescues a wrong/misspelled artist
    # - Moderate title + artist match confirms a 2-signal match
    # - Strong lyric phrase match rescues an unknown/misspelled title
    if t_score >= 0.62:
        combined = min(1.0, t_score * 0.85 + max(a_score, l_score) * 0.15 + 0.05)
    elif l_score >= 0.45:
        combined = min(1.0, l_score * 0.85 + max(t_score, a_score) * 0.15 + 0.12)
    elif t_score >= 0.40 and a_score >= 0.45:
        combined = min(1.0, (t_score * 0.55) + (a_score * 0.45) + 0.08)
    else:
        combined = max(t_score * 0.75, l_score * 0.75, (t_score * 0.5 + a_score * 0.35 + l_score * 0.15))

    return round(combined, 3), {
        "titleScore": t_score,
        "artistScore": a_score,
        "lyricScore": l_score,
    }


def fetch_pinned_youtube_metadata(vid: str) -> dict:
    """Verifies a user-supplied YouTube link actually exists and returns its real title/channel/duration."""
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--skip-download", f"https://youtu.be/{vid}"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if res.returncode == 0 and res.stdout.strip():
            info = json.loads(res.stdout.strip().splitlines()[0])
            return {
                "videoId": vid,
                "youtubeUrl": f"https://youtu.be/{vid}",
                "videoTitle": info.get("title") or "",
                "channel": info.get("channel") or info.get("uploader") or "",
                "description": info.get("description") or "",
                "dur": int(info.get("duration") or 210),
            }
    except Exception:
        pass
    return {}


def discover_youtube_video(title: str, artist: str, language: str, pinned_url: str = "", lyric_hint: str = "") -> dict:
    """
    Stage 3: Multi-Signal Fuzzy YouTube Discovery & Verification (yt-dlp).
    Returns dict with videoId, youtubeUrl, videoTitle, resolvedTitle, resolvedArtist, dur, confidence, signals.
    Returns {} if no candidate passes MIN_MATCH_CONFIDENCE (so caller quarantines the request).
    """
    lang_hint = {"hi": "hindi gospel", "mr": "marathi gospel"}.get(language, "worship")

    # 3A. Verify pinned YouTube link first (handles right title + wrong artist + valid link!)
    vid = extract_youtube_id(pinned_url)
    if vid:
        meta = fetch_pinned_youtube_metadata(vid)
        if meta and meta.get("videoTitle"):
            vlow = meta["videoTitle"].lower()
            dur = meta.get("dur", 210)
            if (60 <= dur <= 720) and not any(b in vlow for b in BLOCKLIST_WORDS):
                conf, signals = score_candidate(
                    title, artist, lyric_hint,
                    meta["videoTitle"], meta["channel"], meta.get("description", "")
                )
                # Explicit valid YouTube link counts as an additional signal (+0.25 boost)
                # unless the title AND lyric have near-zero overlap AND artist has zero overlap
                if conf >= 0.30 or (signals["titleScore"] >= 0.25) or (signals["lyricScore"] >= 0.25):
                    effective_conf = round(min(1.0, max(conf, 0.55) + 0.15), 3)
                    return {
                        "videoId": vid,
                        "youtubeUrl": f"https://youtu.be/{vid}",
                        "videoTitle": meta["videoTitle"],
                        "resolvedTitle": clean_youtube_song_title(meta["videoTitle"], title),
                        "resolvedArtist": meta["channel"] or artist or "Worship",
                        "dur": dur,
                        "confidence": effective_conf,
                        "signals": {**signals, "pinnedUrlVerified": True},
                    }
                else:
                    print(f"  [WARN] Pinned YouTube URL title '{meta['videoTitle']}' did not match request '{title}' (conf={conf}); falling back to search.")
        else:
            print(f"  [WARN] Pinned YouTube URL invalid/unavailable ({pinned_url}); falling back to fuzzy search.")

    # 3B. Progressive multi-query fuzzy search (handles artist typos, title typos, and lyric phrases!)
    queries = []
    if artist and artist.lower() not in ("worship", "unknown"):
        queries.append(f"{title} {artist} {lang_hint}")
    queries.append(f"{title} {lang_hint} lyrics")
    if lyric_hint:
        queries.append(f"{lyric_hint} {lang_hint} song lyrics")
    elif len(title.split()) >= 4:
        queries.append(f"{title} song lyrics")

    best_cand = None
    best_conf = 0.0
    seen_ids = set()

    for q_text in queries:
        try:
            cmd = ["yt-dlp", "--flat-playlist", "--dump-json", "--no-warnings", f"ytsearch5:{q_text}"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            if res.returncode != 0 or not res.stdout.strip():
                continue
            for line in res.stdout.strip().splitlines():
                info = json.loads(line)
                found_id = info.get("id", "")
                if not found_id or found_id in seen_ids:
                    continue
                seen_ids.add(found_id)

                raw_vtitle = info.get("title") or ""
                vlow = raw_vtitle.lower()
                dur = int(info.get("duration") or 0)
                if dur and not (75 <= dur <= 600):
                    continue
                if any(b in vlow for b in BLOCKLIST_WORDS):
                    continue

                channel = info.get("channel") or info.get("uploader") or ""
                conf, signals = score_candidate(title, artist, lyric_hint, raw_vtitle, channel)
                if conf > best_conf:
                    best_conf = conf
                    best_cand = {
                        "videoId": found_id,
                        "youtubeUrl": f"https://youtu.be/{found_id}",
                        "videoTitle": raw_vtitle,
                        "resolvedTitle": clean_youtube_song_title(raw_vtitle, title),
                        "resolvedArtist": channel or artist or "Worship",
                        "dur": dur or 210,
                        "confidence": conf,
                        "signals": signals,
                    }
            # Early stop if we already found a high-confidence match
            if best_conf >= 0.80:
                break
        except Exception:
            continue

    if best_cand and best_conf >= MIN_MATCH_CONFIDENCE:
        return best_cand

    print(f"  [QUARANTINE] No high-confidence match for '{title}' (best_conf={best_conf:.2f} < {MIN_MATCH_CONFIDENCE})")
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
    if any(c.startswith(("Bb", "Bm", "F#m", "Cm", "Fm", "Gm", "Eb", "Ab", "Cmaj7", "Fmaj7")) for c in cset):
        pts += 6
    elif any(c in ("Dm", "Em", "G7", "E7", "D", "A", "D7", "A7") for c in cset):
        pts += 4
    elif len(cset) >= 4:
        pts += 2
    elif "G" in cset:
        pts += 1
    if i_code in ("trv4", "arp4", "arp3", "arp6", "wlk4", "rif4", "srf4", "drv4"):
        pts += 4
    elif meter == "6/8" or i_code in ("keh4", "dad6", "rol6", "chk4", "wch3"):
        pts += 3
    elif meter == "3/4" or i_code in ("syn4", "ich4", "wlz3"):
        pts += 2
    elif b_code == "isl4" or i_code == "flk4":
        pts += 1
    if bpm >= 106:
        pts += 2
    elif bpm >= 86:
        pts += 1
    if pts <= 2:
        return "Beginner"
    if pts <= 4:
        return "Strummer"
    if pts <= 5:
        return "Rhythm Player"
    if pts <= 6:
        return "Band Player"
    if pts <= 7:
        return "Worship Leader"
    return "Maestro"


def choose_pack_file(language: str, title: str, artist: str) -> str:
    # Centralized Remote Song Bank: all newly approved songs publish to packs/community_catalog.json
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


def is_fuzzy_duplicate(nk: str, existing_keys: set) -> bool:
    if nk in existing_keys:
        return True
    for ek in existing_keys:
        if fuzzy_similarity(nk, ek) >= 0.90:
            return True
    return False


def process_queue(queue_path: str, packs_dir: str, report_out: str = "") -> int:
    with open(queue_path, "r", encoding="utf-8") as f:
        qdata = json.load(f)
    requests = qdata.get("songs", qdata if isinstance(qdata, list) else [])
    existing_keys = load_existing_norm_keys(packs_dir)
    added = 0
    quarantined = 0
    report = []

    for req in requests:
        req_id = req.get("id")
        title = (req.get("title") or "").strip()
        artist = (req.get("artist") or "").strip()
        language = (req.get("language") or "en").strip().lower()
        yt_url = (req.get("youtube_url") or "").strip()
        payload = req.get("payload") if isinstance(req.get("payload"), dict) else {}
        lyric_hint = (payload.get("lyricHint") or "").strip()

        nk = normalize_song_title_key(title)
        if not nk:
            quarantined += 1
            report.append({"id": req_id, "title": title, "status": "quarantined", "reason": "Empty title"})
            print(f"[QUARANTINE] Empty title")
            continue

        if is_fuzzy_duplicate(nk, existing_keys):
            report.append({"id": req_id, "title": title, "status": "duplicate", "reason": "Already in Songbook"})
            print(f"[SKIP] Already exists (exact or fuzzy match): {title}")
            continue

        # Stage 3: Multi-Signal Fuzzy YouTube Identification & Confidence Gate
        yt_info = discover_youtube_video(title, artist, language, yt_url, lyric_hint)
        if not yt_info or not yt_info.get("videoId"):
            quarantined += 1
            reason = f"Could not verify song identity with >= {int(MIN_MATCH_CONFIDENCE*100)}% confidence (typos/unmatched info)"
            report.append({"id": req_id, "title": title, "status": "quarantined", "reason": reason})
            continue

        # Use verified clean title & artist from YouTube when request had typos or wrong artist
        final_title = strip_devanagari(yt_info.get("resolvedTitle") or title)
        final_artist = strip_devanagari(yt_info.get("resolvedArtist") or artist or "Worship")
        final_nk = normalize_song_title_key(final_title) or nk

        if is_fuzzy_duplicate(final_nk, existing_keys):
            report.append({"id": req_id, "title": title, "resolvedTitle": final_title, "status": "duplicate", "reason": "Resolved song already in Songbook"})
            print(f"[SKIP] Resolved song '{final_title}' already exists in Songbook.")
            continue

        # Stage 2A: Fail-Safe Copyright Gate (run on verified title & artist)
        is_pd, pd_reason = check_public_domain(final_title, final_artist)
        copyright_mode = "public_domain" if is_pd else "chords_youtube_only"
        print(f"[PROCESS] '{title}' -> '{final_title}' by {final_artist} | conf={yt_info['confidence']} | copyright={copyright_mode} ({pd_reason})")

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
            offline_lines, cue_lines = build_chords_only_cues(yt_info.get("dur", 210), chords)
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
            "id": f"{language}-{final_nk.replace(' ', '-')[:40]}",
            "normKey": final_nk,
            "title": final_title,
            "artist": final_artist,
            "category": {"hi": "Hindi Gospel", "mr": "Marathi Gospel"}.get(language, "Worship"),
            "language": language,
            "script": "latin",
            "copyrightStatus": copyright_mode,
            "musicalLevel": classify_song_level(final_title, chords, meter, bpm, strums),
            "musicalMeta": {"key": chords[0], "meter": meter, "bpm": bpm, "chords": chords},
            "strums": strums,
            "offlineLines": offline_lines,
            "youtubeSync": yt_sync,
        }

        pack_fname = choose_pack_file(language, final_title, final_artist)
        pack_path = os.path.join(packs_dir, pack_fname)
        with open(pack_path, "r", encoding="utf-8") as pf:
            pdata = json.load(pf)
        pdata.setdefault("songs", []).append(song_obj)
        with open(pack_path, "w", encoding="utf-8") as pf:
            json.dump(pdata, pf, indent=2, ensure_ascii=False)

        existing_keys.add(final_nk)
        added += 1
        report.append({
            "id": req_id,
            "title": title,
            "resolvedTitle": final_title,
            "resolvedArtist": final_artist,
            "confidence": yt_info["confidence"],
            "status": "added",
        })
        print(f"  -> Added to packs/{pack_fname} ({copyright_mode}, level={song_obj['musicalLevel']})")

    if report_out:
        with open(report_out, "w", encoding="utf-8") as rf:
            json.dump({"processedAt": datetime.now(timezone.utc).isoformat(), "results": report}, rf, indent=2, ensure_ascii=False)

    print(f"\nDone! Added {added} song(s), quarantined {quarantined} unmatched request(s). Running Zero-Defect Quality Gate...")
    rc = subprocess.run([sys.executable, "scripts/validate_and_build_packs.py", "--rebuild-manifest"]).returncode
    return rc


def main():
    parser = argparse.ArgumentParser(description="QCoach Studio Approved Song Queue Processor")
    parser.add_argument("--queue", required=True, help="Path to qcoach_approved_song_queue.json")
    parser.add_argument("--packs-dir", default="packs", help="Path to packs/ directory")
    parser.add_argument("--report-out", default="", help="Optional JSON report path for status updates")
    args = parser.parse_args()
    sys.exit(process_queue(args.queue, args.packs_dir, args.report_out))


if __name__ == "__main__":
    main()
