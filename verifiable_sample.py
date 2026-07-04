#!/usr/bin/env python3
"""
verifiable_sample.py — publicly verifiable random sampling via the drand beacon.

Draws a random sample from a CSV population in a way that a third-party auditor can
independently reproduce and that the operator cannot predict or steer.

Trust model
-----------
Randomness comes from the drand *quicknet* beacon (League of Entropy) — a decentralized,
signed, public randomness beacon. A new random value is produced every 3 seconds and is
impossible for any single party to predict ahead of time.

The tool uses a COMMIT-AHEAD protocol to close the two ways an operator could otherwise
cheat even with a public beacon:

  * Grinding      — if the random value already exists when you draw, you could re-run
                    until you like the result. Prevented by committing to a *future*
                    beacon round (one whose randomness does not exist yet).
  * List tampering — if you see the randomness before freezing the list, you could
                    reorder/edit rows to steer the outcome. Prevented by committing to a
                    hash of the exact population file *before* the round is drawn.

Workflow
--------
  1. commit       — freeze the population (hash it) and pick a future drand round. Also
                    embeds the latest drand round as a cryptographic "not-before" floor.
  2. anchor       — publish commitment.json (the hash, never the raw list) to a public
                    GitHub repo as a signed commit, BEFORE the target round happens. This
                    is the timestamp evidence any future auditor can check — you don't need
                    to know the auditor in advance.
  3. draw         — once the round's time has passed, fetch its signed randomness and
                    derive the sample deterministically. Writes an audit record.
  4. verify       — re-fetch the round from an independent mirror, re-derive the sample,
                    and (if anchored) re-check the GitHub anchor. Anyone can run this.
  5. check-anchor — re-query GitHub to confirm the commitment commit is public, signed,
                    and dated before the beacon round.

Privacy: the public anchor contains only commitment.json (the population *hash*, row
count, and beacon parameters). The raw population.csv — which may contain PII — is NEVER
pushed; it stays private and is shown to auditors under confidentiality at report time.

Selection algorithm (no PRNG — reproducible in any language)
------------------------------------------------------------
  seed  = sha256( randomness_hex + "|" + population_sha256 )
  key_i = sha256( seed_hex + "|" + record_id_i )
  Sort records ascending by (key_i, record_id_i); take the first N.

Dependencies: Python 3 standard library only.
"""

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TOOL_VERSION = "1.0.0"

# drand quicknet chain (League of Entropy). Verify these against https://api.drand.sh/<hash>/info
CHAIN_HASH = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
GENESIS_TIME = 1692803367  # unix seconds
PERIOD = 3                 # seconds per round
GROUP_PUBLIC_KEY = (
    "83cf0f2896adee7eb8b5f01fcad3912212c437e0073e911fb90022d3e760183c"
    "8c4b450b6a0a6c3ac6a5776a2d1064510d1fec758c921cc22b0e17e63aaf4bcb"
    "5ed66304de9cf809bd274ca73bab4af5a6e9c76a4bc09e76eae8991ef5ece45a"
)
SCHEME_ID = "bls-unchained-g1-rfc9380"

DEFAULT_DRAW_BASE = "https://api.drand.sh"
DEFAULT_VERIFY_MIRROR = "https://api2.drand.sh"  # a different host than draw's default

GITHUB_API = "https://api.github.com"


# --------------------------------------------------------------------------- helpers

def utc_iso(unix_seconds):
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    """Parse an ISO-8601 timestamp (with trailing 'Z' or offset) to an aware datetime."""
    if s is None:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def sha256_hex(data_bytes):
    return hashlib.sha256(data_bytes).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def round_for_time(unix_seconds):
    """The drand round whose randomness is produced at or just after `unix_seconds`."""
    if unix_seconds < GENESIS_TIME:
        return 1
    return int((unix_seconds - GENESIS_TIME) // PERIOD) + 1


def time_for_round(round_number):
    """Unix time at which a given round's randomness is produced."""
    return GENESIS_TIME + (round_number - 1) * PERIOD


def fetch_round(base_url, round_number, timeout=20):
    """Fetch a specific beacon round. Returns the parsed JSON dict, or raises."""
    url = f"{base_url.rstrip('/')}/{CHAIN_HASH}/public/{round_number}"
    req = urllib.request.Request(url, headers={"User-Agent": f"verifiable_sample/{TOOL_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload, url


def read_population(csv_path, id_column):
    """Read rows and validate the id column exists and has unique, non-empty values."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("Population CSV appears to be empty (no header row).")
        if id_column not in reader.fieldnames:
            raise ValueError(
                f"id-column {id_column!r} not found. Available columns: {reader.fieldnames}"
            )
        rows = list(reader)

    if not rows:
        raise ValueError("Population CSV has a header but no data rows.")

    ids = [(r.get(id_column) or "").strip() for r in rows]
    if any(v == "" for v in ids):
        raise ValueError(f"Some rows have an empty {id_column!r} value; every row needs a unique id.")
    dupes = {v for v in ids if ids.count(v) > 1}
    if dupes:
        raise ValueError(f"Duplicate {id_column!r} values found (ids must be unique): {sorted(dupes)[:10]}")

    return rows, ids


def select_sample(randomness_hex, population_sha256, ids, sample_size):
    """Deterministic, PRNG-free selection. Returns the list of selected ids, in draw order."""
    if sample_size > len(ids):
        raise ValueError(f"sample-size {sample_size} exceeds population size {len(ids)}.")
    seed = sha256_hex((randomness_hex + "|" + population_sha256).encode("utf-8"))
    keyed = []
    for rid in ids:
        key = sha256_hex((seed + "|" + rid).encode("utf-8"))
        keyed.append((key, rid))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [rid for _key, rid in keyed[:sample_size]], seed


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ------------------------------------------------------------------ git / github anchor

def git(repo_root, *args, check=True):
    """Run a git command in repo_root. Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(["git", "-C", repo_root, *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git_toplevel(path):
    """Return the git repository root containing `path`, or None if not in a repo."""
    start = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    proc = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_config(repo_root, key):
    """Return a resolved git config value (local or global), or '' if unset."""
    proc = subprocess.run(["git", "-C", repo_root, "config", "--get", key],
                          capture_output=True, text=True)
    return proc.stdout.strip()


def signing_configured(repo_root):
    """True if git is set up to sign commits (gpg or ssh signing key present)."""
    return bool(git_config(repo_root, "user.signingkey")) or \
        git_config(repo_root, "commit.gpgsign").lower() == "true"


def repo_slug_from_remote(url):
    """Extract 'owner/repo' from a GitHub remote URL (ssh or https)."""
    if not url:
        return None
    u = url.strip()
    if u.startswith("git@") and ":" in u:      # git@github.com:owner/repo.git
        u = u.split(":", 1)[1]
    elif "github.com/" in u:                    # https://github.com/owner/repo.git
        u = u.split("github.com/", 1)[1]
    else:
        return None
    if u.endswith(".git"):
        u = u[:-4]
    parts = u.strip("/").split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def github_get_commit(slug, sha, timeout=20):
    """Fetch a commit's metadata from GitHub. Prefers authenticated `gh`, falls back to
    the unauthenticated public API. Returns the parsed dict, or raises."""
    if shutil.which("gh"):
        proc = subprocess.run(["gh", "api", f"repos/{slug}/commits/{sha}"],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        # fall through to public API on gh failure (e.g. not authed for this repo)
    url = f"{GITHUB_API}/repos/{slug}/commits/{sha}"
    req = urllib.request.Request(url, headers={
        "User-Agent": f"verifiable_sample/{TOOL_VERSION}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def summarize_github_commit(payload, target_round_time_iso):
    """Extract the audit-relevant fields from a GitHub commit API response."""
    commit = payload.get("commit", {})
    committer_date = (commit.get("committer") or {}).get("date")
    author_date = (commit.get("author") or {}).get("date")
    verification = commit.get("verification") or {}
    target_dt = parse_iso(target_round_time_iso)
    committer_dt = parse_iso(committer_date)
    ordering_ok = bool(committer_dt and target_dt and committer_dt < target_dt)
    return {
        "sha": payload.get("sha"),
        "html_url": payload.get("html_url"),
        "committer_date": committer_date,
        "author_date": author_date,
        "signature_verified": bool(verification.get("verified")),
        "signature_reason": verification.get("reason"),
        "committed_before_round": ordering_ok,
    }


# ---------------------------------------------------------------------------- commit

def cmd_commit(args):
    if not os.path.isfile(args.population):
        sys.exit(f"ERROR: population file not found: {args.population}")

    rows, ids = read_population(args.population, args.id_column)
    if args.sample_size > len(rows):
        sys.exit(f"ERROR: sample-size {args.sample_size} exceeds population size {len(rows)}.")

    os.makedirs(args.out, exist_ok=True)
    frozen = os.path.join(args.out, "population.csv")
    shutil.copyfile(args.population, frozen)
    population_sha256 = sha256_file(frozen)

    now = time.time()
    target_time = now + args.lead_minutes * 60
    target_round = round_for_time(target_time)
    target_round_time = time_for_round(target_round)

    # "Not-before" floor: embed the latest drand round that already exists. Its randomness
    # is unpredictable, so a commitment containing it cannot have been authored before that
    # round's time — this hardens the timeline against backdating the anchor commit.
    created_after = None
    try:
        latest, _url = fetch_round(DEFAULT_DRAW_BASE, "latest")
        created_after = {
            "round": latest["round"],
            "randomness": latest["randomness"],
            "round_time_utc": utc_iso(time_for_round(latest["round"])),
        }
    except Exception as e:  # noqa: BLE001 — freshness proof is best-effort
        print(f"WARNING: could not fetch latest drand round for the not-before floor ({e}).")
        print("         Proceeding without it; the anchor's GitHub timestamp still applies.")

    commitment = {
        "tool_version": TOOL_VERSION,
        "created_utc": utc_iso(now),
        "population_file": "population.csv",
        "population_sha256": population_sha256,
        "population_rows": len(rows),
        "id_column": args.id_column,
        "sample_size": args.sample_size,
        "created_after_beacon": created_after,
        "beacon": {
            "network": "drand-quicknet",
            "chain_hash": CHAIN_HASH,
            "scheme_id": SCHEME_ID,
            "group_public_key": GROUP_PUBLIC_KEY,
            "target_round": target_round,
            "target_round_time_utc": utc_iso(target_round_time),
        },
        "algorithm": (
            "seed = sha256(randomness_hex + '|' + population_sha256); "
            "key_i = sha256(seed_hex + '|' + record_id_i); "
            "sort ascending by (key_i, record_id_i); take first sample_size."
        ),
    }
    commit_path = os.path.join(args.out, "commitment.json")
    write_json(commit_path, commitment)

    commitment_hash = sha256_hex(json.dumps(commitment, sort_keys=True, ensure_ascii=False).encode("utf-8"))

    print("COMMITMENT CREATED")
    print(f"  draw dir           : {args.out}")
    print(f"  population.csv      : {len(rows)} rows, sha256={population_sha256}")
    print(f"  id column          : {args.id_column}")
    print(f"  sample size        : {args.sample_size}")
    print(f"  target drand round : {target_round}")
    print(f"  round produced at  : {utc_iso(target_round_time)}  (~{args.lead_minutes} min from now)")
    print(f"  commitment sha256  : {commitment_hash}")
    print()
    print("PUBLISH THIS NOW, before the round happens, so the commitment is provably")
    print("prior to the randomness:")
    print(f"  round {target_round}  |  population {population_sha256}  |  commitment {commitment_hash}")
    print()
    print(f"NEXT — anchor it publicly (well before {utc_iso(target_round_time)}):")
    print(f"  python3 {os.path.basename(__file__)} anchor --draw-dir {args.out}")
    print(f"THEN — after the round, draw:")
    print(f"  python3 {os.path.basename(__file__)} draw --draw-dir {args.out}")


# ---------------------------------------------------------------------------- anchor

def cmd_anchor(args):
    commit_path = os.path.join(args.draw_dir, "commitment.json")
    if not os.path.isfile(commit_path):
        sys.exit(f"ERROR: no commitment.json in {args.draw_dir} (run `commit` first).")
    commitment = load_json(commit_path)

    repo_root = git_toplevel(args.draw_dir)
    if not repo_root:
        sys.exit(
            "ERROR: the draw directory is not inside a git repository.\n"
            "One-time setup (public repo that will hold your commitments):\n"
            "  cd <this folder>\n"
            "  git init && git branch -M main\n"
            "  gh repo create <owner>/verifiable-sampling --public --source=. --remote=origin\n"
            "Then re-run `anchor`."
        )

    rel_commit = os.path.relpath(os.path.abspath(commit_path), repo_root)
    rel_pop = os.path.relpath(os.path.abspath(os.path.join(args.draw_dir, "population.csv")), repo_root)

    # PRIVACY GUARD: never allow the raw population into public history.
    _, tracked, _ = git(repo_root, "ls-files", "--", rel_pop, check=False)
    _, staged, _ = git(repo_root, "diff", "--cached", "--name-only", "--", rel_pop, check=False)
    if tracked or staged:
        sys.exit(f"ERROR: {rel_pop} is tracked/staged in git. The raw population must NOT be "
                 "published. Remove it from git (git rm --cached) and add it to .gitignore.")
    # Make sure population.csv files are ignored going forward.
    gitignore = os.path.join(repo_root, ".gitignore")
    ignore_line = "population.csv"
    existing = ""
    if os.path.isfile(gitignore):
        with open(gitignore, encoding="utf-8") as f:
            existing = f.read()
    if ignore_line not in existing.split():
        with open(gitignore, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("# raw sampling populations may contain PII — never publish them\n")
            f.write("**/population.csv\n")

    # Stage only the commitment (and .gitignore).
    git(repo_root, "add", "--", rel_commit)
    git(repo_root, "add", "--", ".gitignore", check=False)

    # Decide on signing.
    want_sign = not args.no_sign
    sign_available = signing_configured(repo_root)
    if want_sign and not sign_available:
        print("WARNING: no git signing key configured — committing UNSIGNED.")
        print("         The anchor still works, but a signed commit is stronger evidence of")
        print("         authorship. To enable SSH signing later (one time):")
        print("           git config --global gpg.format ssh")
        print("           git config --global user.signingkey ~/.ssh/id_ed25519.pub")
        print("           git config --global commit.gpgsign true")
        print("         and add that key as a *Signing Key* on github.com/settings/keys.")
    do_sign = want_sign and sign_available

    msg = (f"commit(sample): {args.draw_dir} — population {commitment['population_sha256'][:16]} "
           f"target-round {commitment['beacon']['target_round']}")
    commit_args = ["commit", "-m", msg]
    if do_sign:
        commit_args.insert(1, "-S")

    rc, out, err = git(repo_root, *commit_args, check=False)
    if rc != 0 and "nothing to commit" in (out + err).lower():
        print("NOTE: commitment already committed; using existing commit.")
    elif rc != 0:
        sys.exit(f"ERROR: git commit failed: {err or out}")

    _, sha, _ = git(repo_root, "rev-parse", "HEAD")
    _, branch, _ = git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")

    slug = args.repo or repo_slug_from_remote(
        git(repo_root, "remote", "get-url", args.remote, check=False)[1])

    pushed = False
    if not args.no_push:
        prc, pout, perr = git(repo_root, "push", args.remote, branch, check=False)
        if prc == 0:
            pushed = True
        else:
            print(f"WARNING: git push failed ({perr or pout}). Commit is local only — push it "
                  f"manually before the target round, or the anchor won't be public in time.")

    # Best-effort: capture GitHub's view of the commit (public URL, signature, dates).
    github = None
    if slug and pushed:
        try:
            payload = github_get_commit(slug, sha)
            github = summarize_github_commit(payload, commitment["beacon"]["target_round_time_utc"])
        except Exception as e:  # noqa: BLE001
            print(f"NOTE: could not read the commit back from GitHub yet ({e}). "
                  "Run `check-anchor` shortly to capture it.")

    anchor = {
        "anchored_utc": utc_iso(time.time()),
        "method": "public-github-commit",
        "repo": slug,
        "branch": branch,
        "commit_sha": sha,
        "signed": do_sign,
        "pushed": pushed,
        "public_url": (f"https://github.com/{slug}/commit/{sha}" if slug else None),
        "target_round_time_utc": commitment["beacon"]["target_round_time_utc"],
        "github": github,
    }
    write_json(os.path.join(args.draw_dir, "anchor.json"), anchor)

    print("ANCHORED")
    print(f"  repo        : {slug}")
    print(f"  commit      : {sha}  (signed={do_sign}, pushed={pushed})")
    if anchor["public_url"]:
        print(f"  public URL  : {anchor['public_url']}")
    print(f"  must predate: {anchor['target_round_time_utc']}  (the beacon round)")
    if github:
        mark = "OK" if github["committed_before_round"] else "PROBLEM"
        print(f"  ordering    : [{mark}] commit dated {github['committer_date']} "
              f"{'<' if github['committed_before_round'] else '>='} round time")
        print(f"  signature   : verified={github['signature_verified']} "
              f"({github.get('signature_reason')})")
    print()
    print("Record the public URL above with your project files. Any donor/auditor at year-end")
    print("can open it to see the commitment existed before the beacon round.")


# ----------------------------------------------------------------------- check-anchor

def anchor_checks(draw_dir, repo_override=None):
    """Re-query GitHub and return a list of (label, ok, detail) checks for the anchor."""
    anchor_path = os.path.join(draw_dir, "anchor.json")
    commit_path = os.path.join(draw_dir, "commitment.json")
    if not os.path.isfile(anchor_path):
        return [("anchor present", False, "no anchor.json — run `anchor` first")]
    anchor = load_json(anchor_path)
    commitment = load_json(commit_path)
    slug = repo_override or anchor.get("repo")
    sha = anchor.get("commit_sha")
    target_iso = commitment["beacon"]["target_round_time_utc"]

    checks = []
    if not (slug and sha):
        return [("anchor is publishable", False, f"missing repo/sha in anchor.json (repo={slug})")]

    try:
        payload = github_get_commit(slug, sha)
    except Exception as e:  # noqa: BLE001
        return [(f"commit {sha[:10]} is public on GitHub", False,
                 f"could not fetch https://github.com/{slug}/commit/{sha}: {e}")]

    g = summarize_github_commit(payload, target_iso)
    checks.append((f"commit is public on GitHub ({slug})", True, g["html_url"]))
    checks.append(("commit dated before the beacon round", g["committed_before_round"],
                   f"committed {g['committer_date']} vs round {target_iso}"))
    checks.append(("commit is cryptographically signed", g["signature_verified"],
                   f"reason={g['signature_reason']} — auditor must confirm the key belongs to your org"))
    return checks


def cmd_check_anchor(args):
    checks = anchor_checks(args.draw_dir, args.repo)
    print("ANCHOR CHECK")
    hard_fail = False
    for label, ok, detail in checks:
        # Signature is a soft check (unsigned is allowed but weaker); the rest are hard.
        soft = "signed" in label
        mark = "PASS" if ok else ("WARN" if soft else "FAIL")
        if not ok and not soft:
            hard_fail = True
        print(f"  [{mark}] {label}")
        print(f"         {detail}")
    print()
    if hard_fail:
        print("RESULT: FAIL — the public anchor does not (yet) prove the commitment predates the round.")
        sys.exit(1)
    print("RESULT: PASS — the commitment is publicly anchored before the beacon round.")


# ------------------------------------------------------------------------------ draw

def cmd_draw(args):
    commit_path = os.path.join(args.draw_dir, "commitment.json")
    if not os.path.isfile(commit_path):
        sys.exit(f"ERROR: no commitment.json in {args.draw_dir} (run `commit` first).")
    audit_path = os.path.join(args.draw_dir, "audit_record.json")
    if os.path.isfile(audit_path) and not args.force:
        sys.exit(f"ERROR: {audit_path} already exists. A draw is final; refusing to overwrite. "
                 "(Re-drawing would enable grinding.) Use --force only if you understand this.")

    commitment = load_json(commit_path)
    frozen = os.path.join(args.draw_dir, commitment["population_file"])

    # 1. Tamper check: the population must be byte-for-byte what was committed.
    actual_hash = sha256_file(frozen)
    if actual_hash != commitment["population_sha256"]:
        sys.exit("ERROR: population.csv has changed since commit!\n"
                 f"  committed sha256 : {commitment['population_sha256']}\n"
                 f"  current  sha256 : {actual_hash}\n"
                 "The frozen population was altered. Aborting.")

    rows, ids = read_population(frozen, commitment["id_column"])
    if len(rows) != commitment["population_rows"]:
        sys.exit("ERROR: row count differs from commitment; aborting.")

    target_round = commitment["beacon"]["target_round"]
    target_time = time_for_round(target_round)
    now = time.time()
    if now < target_time:
        wait = int(target_time - now) + 1
        sys.exit(f"ERROR: target round {target_round} is not produced yet "
                 f"(available at {utc_iso(target_time)}). Wait ~{wait}s and re-run.")

    # 2. Fetch the committed round (NOT "latest" — the round was fixed at commit time).
    try:
        payload, url = fetch_round(args.base_url, target_round)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit(f"ERROR: round {target_round} not available yet at {args.base_url}. "
                     "Wait a few seconds and re-run.")
        sys.exit(f"ERROR fetching beacon: HTTP {e.code} {e.reason}")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR reaching beacon at {args.base_url}: {e.reason}")

    if payload.get("round") != target_round:
        sys.exit(f"ERROR: beacon returned round {payload.get('round')}, expected {target_round}.")
    randomness = payload.get("randomness", "")
    if len(randomness) != 64 or any(c not in "0123456789abcdef" for c in randomness):
        sys.exit(f"ERROR: unexpected randomness format from beacon: {randomness!r}")

    # 3. Deterministic selection.
    selected_ids, seed = select_sample(randomness, commitment["population_sha256"],
                                       ids, commitment["sample_size"])
    by_id = {(r.get(commitment["id_column"]) or "").strip(): r for r in rows}
    selected_rows = [by_id[i] for i in selected_ids]

    audit = {
        "tool_version": TOOL_VERSION,
        "drawn_utc": utc_iso(now),
        "commitment": commitment,
        "beacon_result": {
            "chain_hash": CHAIN_HASH,
            "round": payload["round"],
            "randomness": randomness,
            "signature": payload.get("signature"),
            "fetched_from": url,
        },
        "derived_seed": seed,
        "selected_ids": selected_ids,
        "selected_rows": selected_rows,
        "verify_note": (
            "Verify signature independently with the official drand tooling against the "
            "group_public_key in commitment.beacon, then reproduce selection with `verify`."
        ),
    }
    write_json(audit_path, audit)

    print("DRAW COMPLETE")
    print(f"  round        : {payload['round']}")
    print(f"  randomness   : {randomness}")
    print(f"  signature    : {payload.get('signature')}")
    print(f"  selected     : {len(selected_ids)} of {len(ids)}")
    print(f"  audit record : {audit_path}")
    print()
    print(f"Selected {commitment['id_column']} values (draw order):")
    for i, rid in enumerate(selected_ids, 1):
        print(f"  {i:>4}. {rid}")


# ---------------------------------------------------------------------------- verify

def cmd_verify(args):
    audit_path = os.path.join(args.draw_dir, "audit_record.json")
    if not os.path.isfile(audit_path):
        sys.exit(f"ERROR: no audit_record.json in {args.draw_dir} (run `draw` first).")
    audit = load_json(audit_path)
    commitment = audit["commitment"]
    frozen = os.path.join(args.draw_dir, commitment["population_file"])

    checks = []  # (label, ok, detail)

    # Check 1: population still matches the committed hash.
    actual_hash = sha256_file(frozen)
    checks.append(("population.csv matches committed sha256", actual_hash == commitment["population_sha256"],
                   f"committed={commitment['population_sha256']} actual={actual_hash}"))

    # Check 2: re-fetch the round from an INDEPENDENT mirror; randomness must match.
    target_round = commitment["beacon"]["target_round"]
    mirror_ok, mirror_detail, mirror_rand = False, "", None
    try:
        payload, url = fetch_round(args.mirror, target_round)
        mirror_rand = payload.get("randomness")
        mirror_ok = (payload.get("round") == target_round
                     and mirror_rand == audit["beacon_result"]["randomness"])
        mirror_detail = f"mirror={url} round={payload.get('round')} randomness={mirror_rand}"
    except Exception as e:  # noqa: BLE001 — surface any fetch problem as a failed check
        mirror_detail = f"mirror fetch failed: {e}"
    checks.append((f"beacon round {target_round} reproduced from independent mirror", mirror_ok, mirror_detail))

    # Check 3: re-derive the sample from the (mirror-confirmed) randomness.
    rerun_ok, rerun_detail = False, "skipped (no randomness)"
    randomness = mirror_rand or audit["beacon_result"]["randomness"]
    try:
        rows, ids = read_population(frozen, commitment["id_column"])
        recomputed, _seed = select_sample(randomness, commitment["population_sha256"],
                                          ids, commitment["sample_size"])
        rerun_ok = recomputed == audit["selected_ids"]
        rerun_detail = "recomputed sample matches audit record" if rerun_ok else \
            f"MISMATCH: recomputed {recomputed[:5]}... vs recorded {audit['selected_ids'][:5]}..."
    except Exception as e:  # noqa: BLE001
        rerun_detail = f"recompute failed: {e}"
    checks.append(("sample re-derives to the same selection", rerun_ok, rerun_detail))

    # Check 4 (soft): the "not-before" floor — the committed round must be later than the
    # latest round embedded at commit time (i.e. the target round came after the floor).
    floor = commitment.get("created_after_beacon")
    if floor:
        floor_ok = floor["round"] < target_round
        checks.append(("commitment names a round after its not-before floor", floor_ok,
                       f"floor round {floor['round']} < target round {target_round}"))

    print("VERIFICATION")
    all_ok = True
    for label, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        print(f"  [{mark}] {label}")
        print(f"         {detail}")

    # Anchor checks (only if this draw was publicly anchored).
    if not args.skip_anchor and os.path.isfile(os.path.join(args.draw_dir, "anchor.json")):
        print("  -- public anchor --")
        for label, ok, detail in anchor_checks(args.draw_dir):
            soft = "signed" in label
            mark = "PASS" if ok else ("WARN" if soft else "FAIL")
            if not ok and not soft:
                all_ok = False
            print(f"  [{mark}] {label}")
            print(f"         {detail}")

    print()
    if all_ok:
        print("RESULT: PASS — the sample is reproducible and consistent with the commitment.")
        print("NOTE: also verify the BLS signature with official drand tooling against")
        print(f"      group_public_key {GROUP_PUBLIC_KEY[:32]}... for full cryptographic proof.")
    else:
        print("RESULT: FAIL — see failed checks above.")
        sys.exit(1)


# ------------------------------------------------------------------------------- cli

def build_parser():
    p = argparse.ArgumentParser(
        description="Publicly verifiable random sampling using the drand beacon (commit-ahead).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("commit", help="freeze the population + pick a future beacon round")
    c.add_argument("--population", required=True, help="path to population CSV")
    c.add_argument("--id-column", required=True, help="name of the unique id column")
    c.add_argument("--sample-size", type=int, required=True, help="number of records to draw")
    c.add_argument("--lead-minutes", type=float, default=10.0,
                   help="how far in the future to target the beacon round (default 10)")
    c.add_argument("--out", required=True, help="draw directory to create (holds commitment + population)")
    c.set_defaults(func=cmd_commit)

    a = sub.add_parser("anchor", help="publish commitment.json to a public GitHub repo (before the round)")
    a.add_argument("--draw-dir", required=True, help="draw directory created by `commit`")
    a.add_argument("--repo", help="owner/repo override (default: parsed from the git remote)")
    a.add_argument("--remote", default="origin", help="git remote to push to (default origin)")
    a.add_argument("--no-sign", action="store_true", help="do not GPG/SSH-sign the commit")
    a.add_argument("--no-push", action="store_true", help="commit locally but do not push (push manually later)")
    a.set_defaults(func=cmd_anchor)

    ca = sub.add_parser("check-anchor", help="confirm the anchor commit is public, signed, and pre-round")
    ca.add_argument("--draw-dir", required=True, help="draw directory containing anchor.json")
    ca.add_argument("--repo", help="owner/repo override (default: from anchor.json)")
    ca.set_defaults(func=cmd_check_anchor)

    d = sub.add_parser("draw", help="fetch the committed round and derive the sample")
    d.add_argument("--draw-dir", required=True, help="draw directory created by `commit`")
    d.add_argument("--base-url", default=DEFAULT_DRAW_BASE, help=f"drand base URL (default {DEFAULT_DRAW_BASE})")
    d.add_argument("--force", action="store_true", help="overwrite an existing audit record (discouraged)")
    d.set_defaults(func=cmd_draw)

    v = sub.add_parser("verify", help="reproduce a draw from an independent mirror")
    v.add_argument("--draw-dir", required=True, help="draw directory containing audit_record.json")
    v.add_argument("--mirror", default=DEFAULT_VERIFY_MIRROR,
                   help=f"independent drand mirror (default {DEFAULT_VERIFY_MIRROR})")
    v.add_argument("--skip-anchor", action="store_true", help="skip the GitHub anchor re-check")
    v.set_defaults(func=cmd_verify)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
