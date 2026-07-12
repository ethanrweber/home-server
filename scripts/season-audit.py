#!/usr/bin/env python3
"""Audit Sonarr seasons for piecemeal (episode-by-episode) downloads.

Read-only: issues only GET requests against the Sonarr API, then ranks
complete, finished seasons by how fragmented their files look (mixed release
groups, codecs, qualities, and import dates spread over weeks). Use the
output as a worklist for manual interactive season-pack searches in Sonarr.

Usage:
  SONARR_URL=http://sonarr:8989 SONARR_API_KEY=xxx season-audit.py [options]

Options:
  --json             emit JSON instead of a table
  --all              include seasons that look like they came from a pack
  --min-score N      only show seasons with fragmentation score >= N
  --include-airing   include seasons that are still airing (packs rarely
                     exist for these yet)
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def api_get(base_url, api_key, path):
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v3/{path}",
        headers={"X-Api-Key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("error: Sonarr returned 401 — check SONARR_API_KEY")
        sys.exit(f"error: GET /api/v3/{path} returned HTTP {e.code}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach Sonarr at {base_url}: {e.reason}")


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def audio_langs(episode_file):
    raw = (episode_file.get("mediaInfo") or {}).get("audioLanguages") or ""
    return {lang.strip().lower() for lang in raw.split("/") if lang.strip()}


def analyze_season(files):
    groups = {f["releaseGroup"] for f in files if f.get("releaseGroup")}
    unknown_group = any(not f.get("releaseGroup") for f in files)
    qualities = {
        f["quality"]["quality"]["name"]
        for f in files
        if f.get("quality", {}).get("quality", {}).get("name")
    }
    codecs = {
        (f.get("mediaInfo") or {}).get("videoCodec")
        for f in files
        if (f.get("mediaInfo") or {}).get("videoCodec")
    }

    dates = [d for d in (parse_date(f.get("dateAdded")) for f in files) if d]
    span_days = (max(dates) - min(dates)).days if len(dates) > 1 else 0

    lang_sets = [audio_langs(f) for f in files]
    with_audio_data = [s for s in lang_sets if s]
    multi_audio = sum(1 for s in with_audio_data if len(s) >= 2)
    dual_jpn_eng = sum(1 for s in with_audio_data if {"jpn", "eng"} <= s)

    effective_groups = len(groups) + (1 if unknown_group else 0)
    score = (
        max(effective_groups - 1, 0) * 3
        + min(span_days // 7, 8)
        + max(len(codecs) - 1, 0) * 2
        + max(len(qualities) - 1, 0)
    )

    if effective_groups <= 1 and span_days < 7 and len(codecs) <= 1:
        verdict = "likely pack"
    elif effective_groups >= 3 or (effective_groups >= 2 and span_days >= 14) or span_days >= 30:
        verdict = "piecemeal"
    else:
        verdict = "mixed"

    return {
        "files": len(files),
        "release_groups": sorted(groups),
        "unknown_release_group": unknown_group,
        "video_codecs": sorted(codecs),
        "qualities": sorted(qualities),
        "added_span_days": span_days,
        "files_with_audio_data": len(with_audio_data),
        "multi_audio_files": multi_audio,
        "dual_jpn_eng_files": dual_jpn_eng,
        "score": score,
        "verdict": verdict,
    }


def season_is_airing(season):
    stats = season.get("statistics") or {}
    if stats.get("nextAiring"):
        return True
    prev = parse_date(stats.get("previousAiring"))
    # No next airing but the season aired very recently: a pack likely
    # doesn't exist yet either.
    return prev is not None and (datetime.now(timezone.utc) - prev).days < 14


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--min-score", type=int, default=0)
    parser.add_argument("--include-airing", action="store_true")
    args = parser.parse_args()

    base_url = os.environ.get("SONARR_URL", "http://localhost:8989")
    api_key = os.environ.get("SONARR_API_KEY")
    if not api_key:
        sys.exit("error: SONARR_API_KEY is not set")

    results = []
    for series in api_get(base_url, api_key, "series"):
        if (series.get("statistics") or {}).get("episodeFileCount", 0) == 0:
            continue

        files_by_season = {}
        for f in api_get(base_url, api_key, f"episodefile?seriesId={series['id']}"):
            files_by_season.setdefault(f["seasonNumber"], []).append(f)

        for season in series.get("seasons", []):
            number = season["seasonNumber"]
            stats = season.get("statistics") or {}
            files = files_by_season.get(number, [])
            if number == 0 or not season.get("monitored") or not files:
                continue
            if stats.get("episodeFileCount", 0) < stats.get("episodeCount", 0):
                continue  # missing episodes: not an upgrade candidate yet
            if not args.include_airing and season_is_airing(season):
                continue

            analysis = analyze_season(files)
            if analysis["score"] < args.min_score:
                continue
            if analysis["verdict"] == "likely pack" and not args.all:
                continue
            results.append(
                {"series": series["title"], "season": number, **analysis}
            )

    results.sort(key=lambda r: (-r["score"], r["series"], r["season"]))

    if args.as_json:
        json.dump(results, sys.stdout, indent=2)
        print()
        return

    if not results:
        print("No fragmented seasons found.")
        return

    header = f"{'Series':<40} {'Season':>6} {'Files':>5} {'Groups':>6} {'Codecs':>6} {'Quals':>5} {'MultiAud':>8} {'Span':>5} {'Score':>5}  Verdict"
    print(header)
    print("-" * len(header))
    for r in results:
        groups = len(r["release_groups"]) + (1 if r["unknown_release_group"] else 0)
        multi = (
            f"{r['multi_audio_files']}/{r['files_with_audio_data']}"
            if r["files_with_audio_data"]
            else "n/a"
        )
        print(
            f"{r['series'][:40]:<40} {r['season']:>6} {r['files']:>5} {groups:>6}"
            f" {len(r['video_codecs']):>6} {len(r['qualities']):>5} {multi:>8}"
            f" {str(r['added_span_days']) + 'd':>5} {r['score']:>5}  {r['verdict']}"
        )
    print(f"\n{len(results)} season(s) listed. 'MultiAud' = files with 2+ audio languages.")


if __name__ == "__main__":
    main()
