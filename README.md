# Verifiable Random Sampling

Draw a random sample from a list so that:

- **an auditor can reproduce it** independently, and
- **you, the operator, cannot predict or steer it**, even though you run the tool yourself.

> **New to this?** Read [**How it works — a plain-language explainer**](https://ktvarun82.github.io/verifiable-sampling/docs/how-it-works.html)
> ([source](docs/how-it-works.html)). It explains *why* the sample is provably fair,
> for a non-technical auditor or donor, using the numbers from a real draw.

**Highlights**

- **Public randomness.** Uses the [drand](https://drand.love) quicknet beacon, run by the
  League of Entropy (Cloudflare, EPFL, Kudelski, universities and others).
- **Commit-ahead protocol.** You lock in the list *before* the randomness exists, so you
  can't pick a result you like.
- **Zero dependencies.** Pure Python 3 standard library. The selection logic is about 30
  lines you and your auditor can read in full.
- **Self-contained audit package.** Each `draws/<name>/` folder holds everything an
  auditor needs, verifiable against a beacon mirror you never touched.

## Contents

- [Quick start](#quick-start)
- [Why "just use a random number" fails an audit](#why-just-use-a-random-number-fails-an-audit)
- [The protocol, step by step](#the-protocol-step-by-step)
- [How the sample is chosen](#how-the-sample-is-chosen)
- [The public GitHub anchor](#the-public-github-anchor)
- [Auditor verification recipe](#auditor-verification-recipe)
- [Beacon details](#beacon-details-drand-quicknet)
- [Notes and limits](#notes-and-limits)

---

## Quick start

```bash
# 1. Commit: freeze the list and pick a future beacon round
python3 verifiable_sample.py commit --population farmers_2026.csv --id-column farmer_id \
    --sample-size 200 --lead-minutes 30 --out draws/2026-farmer-survey

# 2. Anchor: publish the commitment to your public GitHub repo
python3 verifiable_sample.py anchor --draw-dir draws/2026-farmer-survey

# 3. Draw: once the round has passed, derive the sample
python3 verifiable_sample.py draw --draw-dir draws/2026-farmer-survey

# 4. Verify: you or the auditor re-check everything
python3 verifiable_sample.py verify --draw-dir draws/2026-farmer-survey
```

The anchor step needs a one-time [GitHub setup](#one-time-setup).

---

## Why "just use a random number" fails an audit

Ordinary random generators (`random`, `Math.random()`, `=RAND()`) prove nothing:
whoever controls the seed controls the outcome.

Even a public beacon leaves two loopholes unless you commit ahead of time:

| Loophole | What goes wrong | How this tool closes it |
|---|---|---|
| **Grinding** | If the random value already exists, you can re-run until you like the sample. | Target a **future** beacon round whose randomness does not exist yet. |
| **List tampering** | If you see the randomness before freezing the list, you can reorder or edit rows to steer who gets picked. | Publish a **hash of the exact population** *before* the round happens. |

---

## The protocol, step by step

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

### One-time setup

The anchor step publishes commitments to a public GitHub repo. Only *hashes* are
published, never your raw data (see [Privacy](#privacy)).

```bash
cd verifiable-sampling
git init && git branch -M main
gh repo create <owner>/verifiable-sampling --public --source=. --remote=origin
```

**Recommended:** sign your commits, so anchors are provably *yours*.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519      # skip if you already have a key
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
```

Then add `~/.ssh/id_ed25519.pub` as a *Signing Key* at github.com/settings/keys.

### Step 1 — Commit (before the randomness exists)

```bash
python3 verifiable_sample.py commit \
    --population farmers_2026.csv \
    --id-column farmer_id \
    --sample-size 200 \
    --lead-minutes 30 \
    --out draws/2026-farmer-survey
```

This command:

1. freezes a copy of your CSV and computes its SHA-256;
2. embeds the *latest* drand round as a cryptographic "not-before" floor;
3. picks the future drand round that will be produced `--lead-minutes` from now.

### Step 2 — Anchor (publish before the round)

```bash
python3 verifiable_sample.py anchor --draw-dir draws/2026-farmer-survey
```

This commits `commitment.json` to your public GitHub repo (signed, if you set up a key),
pushes it, and prints a **public URL** like:

```
https://github.com/<owner>/verifiable-sampling/commit/<sha>
```

Keep that URL with your project files. It is the timestamp evidence any future auditor
checks. You do **not** need to know the auditor in advance.

> **Anchor well before the target round.** The whole guarantee is that the commitment was
> public *before* the randomness existed. A 15–30 minute lead is plenty.

### Step 3 — Draw (after the round's time has passed)

```bash
python3 verifiable_sample.py draw --draw-dir draws/2026-farmer-survey
```

This command:

- refuses to run until the committed round exists;
- re-checks the population hash and aborts if the file changed;
- fetches the **committed** round (not "latest") and derives the sample;
- writes `audit_record.json`.

A completed draw is final. The tool refuses to overwrite it, because re-drawing would
reintroduce grinding.

### Step 4 — Verify (you or the auditor)

```bash
python3 verifiable_sample.py verify --draw-dir draws/2026-farmer-survey
```

This command:

- re-fetches the round from a **different** drand mirror than the draw used;
- confirms the randomness matches;
- re-hashes the population and re-derives the sample;
- re-checks the GitHub commit, if the draw was anchored.

It prints **PASS** or **FAIL**. To re-check only the anchor:

```bash
python3 verifiable_sample.py check-anchor --draw-dir draws/2026-farmer-survey
```

---

## How the sample is chosen

No pseudo-random generator is involved, so the result reproduces bit-for-bit in Python,
JavaScript, Go, or even a shell one-liner:

```
seed  = sha256( randomness_hex + "|" + population_sha256 )
key_i = sha256( seed_hex + "|" + record_id_i )       for each record
sort records ascending by (key_i, record_id_i), take the first N
```

`record_id` is the value in your `--id-column`. It must be **unique and non-empty** for
every row.

---

## The public GitHub anchor

### Why it's needed

A typical case: you draw the sample now (say, farmers to survey), run the survey for a
month or two, and report to donors or auditors at year-end. You don't know who they will
be, so you *can't* send them the commitment before the round.

Instead, you publish the commitment somewhere **public and independently timestamped**.
A public GitHub commit is visible to whoever shows up at year-end, and you can't quietly
rewrite public history after the fact.

### Two safeguards

- **Signed commits** prove the commitment is genuinely *yours*. GitHub shows them as
  "Verified."
- **The "not-before" floor.** On its own, a GitHub commit date can be forged by the
  committer. So `commit` embeds the latest drand randomness into `commitment.json`. Since
  a drand value can't be known before it is produced, the commitment could **not** have
  been written earlier than that round. Backdating below that floor is impossible.

### Privacy

The anchor publishes **only `commitment.json`**: the population *hash*, row count, and
beacon parameters.

Your raw `population.csv`, which may contain names or other personal data, is **never**
pushed. The tool refuses to commit it and adds it to `.gitignore`. Show the raw list to
auditors privately at report time; they check it against the published hash.

### Making the anchor even stronger

GitHub is a trusted third party, so one assumption remains: that GitHub's public history
wasn't tampered with. To reduce even that:

- share the commit URL as early as possible (project mailing list, your organisation's
  site), and/or
- also timestamp `commitment.json` with [OpenTimestamps](https://opentimestamps.org) for
  a trustless Bitcoin anchor.

For most donor reporting, a signed public commit is enough.

---

## Auditor verification recipe

With the `draws/<name>/` folder and the public commit URL, an auditor can check the draw
without trusting the operator's machine.

**1. The commitment was public before the randomness existed.**
Open the commit URL on GitHub. Confirm it is "Verified" (signed by the organisation's key)
and dated before `target_round_time_utc` in `commitment.json`. Or run:

```bash
python3 verifiable_sample.py check-anchor --draw-dir draws/<name>
```

**2. The population is the one that was committed.**

```bash
sha256sum draws/<name>/population.csv
```

The result must equal `population_sha256` in `commitment.json`, which is what the public
commit contains.

**3. The randomness is genuine drand output for the committed round.**
Fetch the round from an independent mirror:

```bash
curl https://api3.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public/<round>
```

Confirm `randomness` matches `audit_record.json`. Then verify the BLS signature with
official drand tooling against the group public key in `commitment.json`:

```bash
# using the official drand CLI
drand verify --chain-hash 52db9ba7…  # or the JS lib drand-client
```

The public key `83cf0f2896…` is the League of Entropy quicknet key, published at
<https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/info>.

**4. The sample follows from the randomness.**
Redo the sha256-and-sort steps from [How the sample is chosen](#how-the-sample-is-chosen)
by hand, or run:

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

This config lives at the top of `verifiable_sample.py`. If drand ever rotates the quicknet
group key, update `GROUP_PUBLIC_KEY` there and cross-check it against the `/info` endpoint.

---

## Notes and limits

- **Sampling is without replacement** and uniform: every record is equally likely. The
  sample comes in a fixed order, so if you need reserves, increase the sample size and the
  extra ids follow deterministically.
- **Export database data to CSV first.** The commitment freezes a file. A live query could
  return different rows later and can't be hashed by an auditor.
- **BLS signatures are not checked by the tool.** That is left to official drand tooling
  on purpose (see auditor step 3). The tool stores the signature so anyone can check it.
- **Anchor before you draw, with margin.** The guarantee holds only if the public commit
  predates the beacon round. The tool checks the order, but a 15–30 minute lead keeps a
  slow push from cutting it close.
- **`population.csv` never leaves your machine through the anchor.** It is gitignored and
  the tool refuses to publish it. Give it to auditors privately at report time.
- **One folder per draw.** Keep each real draw in its own `draws/<name>/` folder. That
  folder plus the public commit URL is the complete evidence package.
