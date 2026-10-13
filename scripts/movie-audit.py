#!/usr/bin/env python3
"""Audit Radarr movies against their Profilarr quality profiles.

Read-only: issues only GET requests against the Radarr API, then lists every
movie whose file does not fit its quality profile, one row per movie with the
most important issue first. Use the output as a worklist for profile changes
and manual interactive searches in Radarr.

Usage:
  RADARR_URL=http://radarr:7878 RADARR_API_KEY=xxx movie-audit.py [options]

Options:
  --json          emit JSON instead of a table
  --issue NAME    only show one issue (see ISSUES below)

Movies tagged "audit-ok" in Radarr are skipped. Tag a movie to accept its
current file and remove it from the list.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

OK_TAG = "audit-ok"

# Radarr's built-in profiles. Every movie should be on a Profilarr profile.
DEFAULT_PROFILES = {"Any", "SD", "HD-720p", "HD-1080p", "Ultra-HD", "HD - 720p/1080p"}

# Issue key -> label, in priority order. A movie is listed under the first
# issue that applies to it.
ISSUES = {
    "default-profile": "default profile",
    "wrong-profile": "wrong profile",
    "banned-group": "banned group",
    "excluded-format": "excluded format",
    "language": "language",
    "no-group": "no release group",
    "no-tier": "no tier match",
}

LANGUAGE_FORMAT = "Not Original or English"
NO_GROUP_FORMAT = "Release Group (Missing)"
HEVC = {"x265", "h265", "hevc"}


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
            sys.exit("error: Radarr returned 401 — check RADARR_API_KEY")
        sys.exit(f"error: GET /api/v3/{path} returned HTTP {e.code}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach Radarr at {base_url}: {e.reason}")


def get_movie_files(base_url, api_key, file_ids):
    # The movie list has no custom formats, so read the files in batches.
    files = {}
    for i in range(0, len(file_ids), 100):
        query = "&".join(f"movieFileIds={fid}" for fid in file_ids[i:i + 100])
        for f in api_get(base_url, api_key, f"moviefile?{query}"):
            files[f["id"]] = f
    return files


def allowed_qualities(profile):
    allowed = set()
    for item in profile["items"]:
        if not item["allowed"]:
            continue
        for sub in item.get("items") or [item]:
            if sub["allowed"]:
                allowed.add(sub["quality"]["name"])
    return allowed


def resolution_class(movie_file):
    # Read the resolution from the file, not the quality label: some labels
    # are wrong (a 1760x976 encode labelled Bluray-720p).
    try:
        width, height = (int(n) for n in movie_file["mediaInfo"]["resolution"].split("x"))
    except (KeyError, TypeError, ValueError):
        return movie_file["quality"]["quality"]["resolution"]
    if width >= 3000 or height >= 1700:
        return 2160
    if width >= 1400 or height >= 1000:
        return 1080
    if width >= 1200 or height >= 700:
        return 720
    return 480


def recommended_profile(movie_file):
    """Return the profile for this file, or None when no rule applies."""
    resolution = resolution_class(movie_file)
    remux = movie_file["quality"]["quality"]["modifier"] == "remux"
    if resolution == 2160:
        return "2160p Remux" if remux else "2160p Efficient"
    if remux:
        return "1080p Remux"
    if resolution >= 720:
        return "1080p Compact"
    return None


def is_reencoded_remux(movie_file):
    # 1080p Blu-rays are AVC, VC-1 or MPEG-2. A "Remux-1080p" in HEVC was
    # re-encoded after download (Tdarr did this to many files in 2024).
    quality = movie_file["quality"]["quality"]
    codec = ((movie_file.get("mediaInfo") or {}).get("videoCodec") or "").lower()
    return quality["modifier"] == "remux" and quality["resolution"] == 1080 and codec in HEVC


def find_issue(movie, movie_file, profile):
    """Return (issue key, detail) for the most important issue, or None."""
    quality = movie_file["quality"]["quality"]["name"]
    recommended = recommended_profile(movie_file)

    if profile["name"] in DEFAULT_PROFILES:
        if recommended is None:
            return "default-profile", f"no rule for a {resolution_class(movie_file)}p file; choose a profile"
        return "default-profile", f"set profile to {recommended}"

    if quality not in allowed_qualities(profile):
        if recommended and recommended != profile["name"]:
            return "wrong-profile", f"{quality} is not allowed; set profile to {recommended}"
        return "wrong-profile", f"{quality} is not allowed; fix the quality label in Radarr"

    score = movie_file.get("customFormatScore") or 0
    if score >= profile["minFormatScore"]:
        return None

    # A negative format is one the profile rejects (Profilarr gives -999999).
    format_scores = {f["name"]: f["score"] for f in profile["formatItems"]}
    negative = [
        f["name"] for f in movie_file.get("customFormats") or []
        if format_scores.get(f["name"], 0) < 0
    ]
    group = movie_file.get("releaseGroup") or "no group"

    banned = [name for name in negative if name.startswith("Banned")]
    if banned:
        return "banned-group", f"{group} ({', '.join(banned)})"

    excluded = [name for name in negative if name not in (LANGUAGE_FORMAT, NO_GROUP_FORMAT)]
    if excluded:
        return "excluded-format", ", ".join(excluded)

    if LANGUAGE_FORMAT in negative:
        languages = [lang["name"] for lang in movie_file.get("languages") or []]
        if languages in ([], ["Unknown"]):
            # A false positive: Radarr reads the language from the file name.
            return "language", "language is Unknown in Radarr; set it in Manage Files"
        original = (movie.get("originalLanguage") or {}).get("name", "?")
        return "language", f"file is {'/'.join(languages)}, original is {original}; check the audio"

    if NO_GROUP_FORMAT in negative:
        return "no-group", "Radarr has no release group for this file"

    if movie_file.get("releaseGroup"):
        return "no-tier", f"no tier format matches {group}"
    return "no-tier", "no release group, so no tier format can match"


def short_name(result, width=40):
    # Shorten the title, not the year, so long titles stay identifiable.
    year = f" ({result['year']})"
    title = result["title"]
    if len(title) + len(year) > width:
        title = title[:width - len(year) - 1] + "~"
    return title + year


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--issue", choices=list(ISSUES))
    args = parser.parse_args()

    base_url = os.environ.get("RADARR_URL", "http://localhost:7878")
    api_key = os.environ.get("RADARR_API_KEY")
    if not api_key:
        sys.exit("error: RADARR_API_KEY is not set")

    movies = [m for m in api_get(base_url, api_key, "movie") if m.get("movieFile")]
    profiles = {p["id"]: p for p in api_get(base_url, api_key, "qualityprofile")}
    ok_tags = {t["id"] for t in api_get(base_url, api_key, "tag") if t["label"] == OK_TAG}
    files = get_movie_files(base_url, api_key, [m["movieFile"]["id"] for m in movies])

    results = []
    skipped = 0
    for movie in movies:
        if ok_tags & set(movie.get("tags") or []):
            skipped += 1
            continue
        movie_file = files[movie["movieFile"]["id"]]
        profile = profiles[movie["qualityProfileId"]]
        found = find_issue(movie, movie_file, profile)
        if not found:
            continue
        issue, detail = found
        if is_reencoded_remux(movie_file):
            detail += "; re-encoded, not a real remux"
        results.append({
            "issue": issue,
            "title": movie["title"],
            "year": movie["year"],
            "profile": profile["name"],
            "quality": movie_file["quality"]["quality"]["name"],
            "release_group": movie_file.get("releaseGroup") or "",
            "score": movie_file.get("customFormatScore") or 0,
            "formats": [f["name"] for f in movie_file.get("customFormats") or []],
            "detail": detail,
        })

    order = list(ISSUES)
    results.sort(key=lambda r: (order.index(r["issue"]), r["profile"], r["title"].lower()))
    shown = [r for r in results if not args.issue or r["issue"] == args.issue]

    if args.as_json:
        json.dump(shown, sys.stdout, indent=2)
        print()
        return

    if shown:
        header = f"{'Issue':<16} {'Movie':<40} {'Profile':<16} {'Quality':<13} {'Score':>8}  Detail"
        print(header)
        print("-" * len(header))
        for r in shown:
            print(
                f"{ISSUES[r['issue']]:<16} {short_name(r):<40} {r['profile']:<16}"
                f" {r['quality']:<13} {r['score']:>8}  {r['detail']}"
            )
        print()

    counts = {key: sum(1 for r in results if r["issue"] == key) for key in ISSUES}
    summary = ", ".join(f"{ISSUES[key]} {n}" for key, n in counts.items() if n)
    print(f"{len(results)} of {len(movies)} movie(s) have an issue: {summary or 'none'}.")
    print(f"{skipped} movie(s) tagged {OK_TAG} skipped.")


if __name__ == "__main__":
    main()
