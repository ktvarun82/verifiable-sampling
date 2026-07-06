# Verifiable Random Sampling

Draw a random sample from a list in a way that a third-party auditor can **independently
reproduce**, and that **you (the operator) cannot predict or steer** — even though you
run the tool yourself.

Standard random generators (`random`, `Math.random()`, `=RAND()`) can't prove this:
whoever controls the seed controls the outcome. This tool sources its randomness from a
**public randomness beacon** ([drand](https://drand.love) quicknet, run by the League of
Entropy — Cloudflare, EPFL, Kudelski, universities, etc.) and binds the draw with a
**commit-ahead protocol** so the result is fixed by public randomness that did not exist
when you committed to the population.

> **New to this?** [**How it works — a plain-language explainer**](https://ktvarun82.github.io/verifiable-sampling/docs/how-it-works.html)
> walks through *why* the sample is provably fair — written for a non-technical auditor or
> donor, using the numbers from a real draw.
> ([source](docs/how-it-works.html))

- **Zero dependencies** — pure Python 3 standard library. The selection logic is ~30
  lines you (and your auditor) can read in full.
- **The `draws/<name>/` folder is a self-contained audit package** — hand it to an
  auditor and they can verify everything from a beacon mirror you never touched.

---

## Why a naive "use a random number" approach fails an audit

Even if you use a public beacon, two loopholes remain unless you commit ahead of time:

1. **Grinding.** If the random value already exists when you run the draw, you can re-run
   until you get a sample you like. → Closed by targeting a **future** beacon round whose
   randomness does not exist yet.
2. **List tampering.** If you can see the randomness before freezing the list, you can
   reorder or edit rows to steer who gets picked. → Closed by publishing a **hash of the
   exact population** *before* the round happens.

This tool enforces both.

---

## The protocol

```
      commit            anchor           (wait)            draw            verify
  ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐
  │ freeze list│   │ push hash  │   │ target     │   │ fetch that │   │ anyone re- │
  │ + pick a   │──▶│ to public  │──▶│ round      │──▶│ round;     │──▶│ fetches +  │
  │ FUTURE     │   │ GitHub     │   │ produced   │   │ derive     │   │ recomputes │
  │ round      │   │ (signed)   │   │ by drand   │   │ sample     │   │ + re-check │
  └────────────┘   └────────────┘   └────────────┘   └────────────┘   └────────────┘
                   public URL is                     writes            PASS / FAIL
                   the timestamp                     audit_record
                   ── BEFORE the round ──
```

### One-time setup: a public GitHub repo for your commitments

The anchor step publishes commitments to a public GitHub repo (only the *hashes* — never
the raw data; see Privacy below). Set this up once:

```bash
cd verifiable-sampling
git init && git branch -M main
gh repo create <owner>/verifiable-sampling --public --source=. --remote=origin
```

Optional but recommended — enable commit signing so anchors are provably *yours*:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519      # if you don't already have a key
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
# then add ~/.ssh/id_ed25519.pub as a *Signing Key* at github.com/settings/keys
```

### 1. Commit (before the randomness exists)

```bash
python3 verifiable_sample.py commit \
    --population farmers_2026.csv \
    --id-column farmer_id \
    --sample-size 200 \
    --lead-minutes 30 \
    --out draws/2026-farmer-survey
```

This freezes a copy of your CSV, computes its SHA-256, embeds the *latest* drand round as
a cryptographic "not-before" floor, and picks the future drand round that will be produced
`--lead-minutes` from now.

### 2. Anchor (publish the commitment publicly, before the round)

```bash
python3 verifiable_sample.py anchor --draw-dir draws/2026-farmer-survey
```

Commits `commitment.json` to your public GitHub repo (signed, if you set up a key) and
pushes it. It prints a **public URL** like
`https://github.com/<owner>/verifiable-sampling/commit/<sha>`. Record that URL with your
project files — it is the timestamp evidence any future auditor checks. You do **not** need
to know the auditor in advance; the public commit stands on its own.

> **Do this well before the target round.** The whole security property is that the
> commitment was public *before* the randomness existed. A 15–30 minute lead is plenty.

### 3. Draw (after the round's time has passed)

```bash
python3 verifiable_sample.py draw --draw-dir draws/2026-farmer-survey
```

It refuses to run until the committed round exists, re-checks the population hash (aborts
if the file changed), fetches the **committed** round (not "latest"), derives the sample,
and writes `audit_record.json`. A completed draw is final — it refuses to overwrite,
because re-drawing would reintroduce grinding.

### 4. Verify (you, or the auditor)

```bash
python3 verifiable_sample.py verify --draw-dir draws/2026-farmer-survey
```

Re-fetches the round from a **different** drand mirror than the draw used, confirms the
randomness matches, re-hashes the population, re-derives the sample, and (if anchored)
re-checks the GitHub commit. Prints PASS/FAIL. To re-check just the anchor:

```bash
python3 verifiable_sample.py check-anchor --draw-dir draws/2026-farmer-survey
```

---

## How the sample is chosen (no PRNG — reproducible in any language)

```
seed  = sha256( randomness_hex + "|" + population_sha256 )
key_i = sha256( seed_hex + "|" + record_id_i )       for each record
sort records ascending by (key_i, record_id_i), take the first N
```

No language's random-number generator is involved, so it reproduces bit-for-bit in
Python, JavaScript, Go, or even a shell one-liner. `record_id` is the value in your
`--id-column`, which must be **unique and non-empty** for every row.

---

## Why a public GitHub anchor (when you don't know the auditor yet)

A common situation: you draw the sample now (e.g. farmers to survey), run the survey over
a month or two, and only submit the report to donors/auditors at year-end — and you don't
know who those donors will be in advance. So you *can't* email the commitment to a specific
auditor before the round.

The fix is to publish the commitment to a **public, independently-timestamped medium**
that *anyone* can inspect later. A public GitHub commit does this: it's visible to whoever
shows up at year-end, and its existence in public history is evidence you can't quietly
rewrite after the fact.

**Two safeguards make the GitHub timeline trustworthy:**

- **Signed commits** prove the commitment is genuinely *yours* (not someone impersonating
  you). GitHub shows these as "Verified."
- **The "not-before" floor.** GitHub commit *dates* are, on their own, forgeable by the
  committer. So `commit` embeds the latest drand round's randomness into `commitment.json`.
  Because a drand value cannot be known before it is produced, the commitment provably
  could **not** have been authored earlier than that round's time — no backdating below
  that floor is possible.

**Privacy — important:** the anchor publishes **only `commitment.json`** (the population
*hash*, row count, and beacon parameters). Your raw `population.csv` — which may contain
farmer names or other PII — is **never** pushed; the tool refuses to commit it and adds it
to `.gitignore`. The raw list stays private and is shown to auditors under confidentiality
at report time, where they check it against the published hash.

**Strengthening the anchor.** GitHub is a trusted third party, so the residual assumption
is "GitHub's public history wasn't tampered with." To reduce even that: publicize the
commit URL as early as possible (project mailing list, your org's site), and/or *also*
[OpenTimestamps](https://opentimestamps.org) the `commitment.json` for a trustless
Bitcoin anchor. For most donor-reporting purposes a signed public commit is sufficient.

---

## Auditor verification recipe (independent of the operator)

An auditor who has the `draws/<name>/` folder plus the public commit URL can confirm the
draw without trusting your machine:

1. **The commitment was public before the randomness existed.** Open the commit URL on
   GitHub, confirm it is "Verified" (signed by your org's key), and that its date is before
   `target_round_time_utc` in `commitment.json`. Or run:
   ```bash
   python3 verifiable_sample.py check-anchor --draw-dir draws/<name>
   ```

2. **The population is the one that was committed.**
   ```bash
   sha256sum draws/<name>/population.csv
   ```
   Must equal `population_sha256` in `commitment.json` (which is what the public commit
   contains).

3. **The randomness is genuine drand output for the committed round.** Fetch the round
   from an independent mirror:
   ```bash
   curl https://api3.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public/<round>
   ```
   Confirm `randomness` matches `audit_record.json`, then verify the BLS signature with
   the official drand tooling against the group public key in `commitment.json`:
   ```bash
   # using the official drand CLI
   drand verify --chain-hash 52db9ba7…  # or the JS lib drand-client
   ```
   (The public key `83cf0f2896…` is the League of Entropy quicknet key, published at
   <https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/info>.)

4. **The sample follows from the randomness.** Either re-run the sha256 + sort steps
   above by hand, or just:
   ```bash
   python3 verifiable_sample.py verify --draw-dir draws/<name> --mirror https://api3.drand.sh
   ```

---

## Beacon details (drand quicknet)

| Field | Value |
|---|---|
| Chain hash | `52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971` |
| Scheme | `bls-unchained-g1-rfc9380` (unchained, timelock-friendly) |
| Round period | 3 seconds |
| Genesis time | 1692803367 (unix) |
| Round for time T | `floor((T − genesis) / period) + 1` |
| Mirrors | `api.drand.sh`, `api2.drand.sh`, `api3.drand.sh`, `drand.cloudflare.com` |

Config lives at the top of `verifiable_sample.py`. If drand ever rotates the quicknet
group key, update `GROUP_PUBLIC_KEY` there (and cross-check against the `/info` endpoint).

---

## Notes & limits

- **Sampling without replacement**, uniform over the population. Each record is equally
  likely; the sample is drawn in a defined order (useful if you later need "the next few"
  as reserves — extend the sample size and the extra ids follow deterministically).
- **Export DB data to CSV first.** The commitment freezes a file; a live query could
  return different rows on re-run and can't be hashed by an auditor.
- The tool does **not** itself verify the BLS signature (that's delegated to official
  drand tooling on purpose — see step 3). It stores the signature so anyone can.
- **Anchor before you draw, with margin.** The security property holds only if the public
  commit predates the beacon round. The tool checks the ordering, but give yourself a
  15–30 minute lead so a slow push doesn't cut it close.
- **`population.csv` never leaves your machine via the anchor.** It is gitignored and the
  tool refuses to publish it. Deliver it privately to auditors at report time.
- Keep each real draw in its own `draws/<name>/` subfolder; that folder plus the public
  commit URL is the complete evidence package.
