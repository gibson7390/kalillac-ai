# Staging host assessment and release integration (2026-11-18)

For the owner, before staging is placed anywhere. Three parts: what was measured, what still has to be
measured on the host (one command), and the integration a coherent staging release needs. Nothing was
deployed, merged, purchased or changed on any server.

## 1. Host access: why the host's own numbers are not in this document yet

The workstation holds a key (`~/.ssh/kalillac_dedicated_ed25519`) and a `known_hosts` entry for
**134.122.5.134**, the production origin. No login user is recorded anywhere (no SSH config, no
deployment document names one; `deploy/release` runs as root on the host), and the standing directive
forbids SSH to production. Guessing users against production is not read-only in effect: failed logins
can trip fail2ban and lock the owner's own address out. So **no claim is made here about the host's CPU,
memory, disk, ports, units or PostgreSQL**. They come from one read-only command (section 3); the
decision rules in section 4 turn its output into a recommendation. Given the user name and a go-ahead,
the session can run the same command itself.

## 2. Measured (2026-11-18)

**Public edge, from the workstation:**

| Check | Result |
|---|---|
| `kalillac.ai`, `www.kalillac.ai` | resolve only to Cloudflare (172.67.177.55, 104.21.67.138); `Server: cloudflare`, `CF-RAY` present |
| `staging.kalillac.ai` | **no DNS record**: the name is free to create |
| The origin (134.122.5.134) contacted directly on 443 and 80 | **no TCP connection** (timeout, exit 28): direct traffic from outside Cloudflare is dropped, at least from this network. Section 8 of the script shows the rule that does it |

**Staging's own footprint**, measured on Linux (WSL, Python 3.12) from this repository at `e6aa7a5`:

| Component | Measured | Bound in the code |
|---|---|---|
| Application process, every module loaded | **77 MiB** peak RSS, 3.8 s | one Uvicorn worker, enforced by `kalillac_server.py` |
| File worker, heaviest benchmark tasks (research PDF and DOCX, Arabic PDF, forecast XLSX, executive PPTX) | **94 MiB** peak RSS (the DOCX), 1–3 s each | each worker capped at 768 MiB address space, at most 2 at once, 20 s (`kalillac_files/worker.py`) |
| Database connections | — | at most 11: runtime 2+2, registration 4, V1 authority 3 (the operator tool's P only when run) |
| Disk | Python environment with document libraries 676 MB; repository 8 MB packed; fonts 8 MB (+ ~15 MB CJK fetched); built site 0.7 MB | uploads at most 10 MiB each; Node and `node_modules` only while building the web root |

**Resulting envelope** (measured values plus the code's bounds; to be confirmed under load in staging):
memory about 0.3 GiB typical for the application unit (application plus at times two workers), worst case
bounded by the unit's limit below; a second PostgreSQL 16 cluster about 0.2–0.3 GiB (defaults:
`shared_buffers` 128 MB, about 11 backends); disk about 3 GiB including a second release, logs and the
staging database; CPU about one core under the owner's own testing, two briefly while two documents render.

**Proposed limits for a shared host** (protect production first; staging may be slow, production may not):

| Setting | Application unit | Staging PostgreSQL cluster (drop-in) |
|---|---|---|
| `MemoryHigh` / `MemoryMax` | 768M / 1G (the cgroup includes the workers; their own 768 MiB cap stops a single runaway) | 384M / 512M |
| `CPUWeight` | 20 (production's default 100 wins 5:1 under contention) | 20 |
| `CPUQuota` | 50% on 2 vCPU, 100% on 4 or more (never on 1 vCPU: see section 4) | 50% |
| `IOWeight`, `TasksMax` | 20, 256 | 20, — |

## 3. The one command that measures the host (read-only)

From the workstation, in this repository (streams the script; nothing is copied to or written on the host):

    ssh <user>@134.122.5.134 'sudo -n bash -s' < deploy/staging/assess_host.sh > host-assessment.txt

Without passwordless sudo, drop `sudo -n`; the root-only sections then say so. About 70 seconds (a
one-minute CPU and memory sample). It covers: CPU, load and pressure stall information, a 1-minute
`vmstat` sample, memory and OOM history (30 days), sysstat history if installed, disk and inodes,
production's unit footprint by named properties (memory now and peak, CPU used, restarts, user, limits),
every listener and ports 8001–8003 and 5432–5434, Kalillac units, timers and directories, the Nginx
directives that matter (server names, listeners, upstreams, origin-client checks; never secrets),
PostgreSQL (clusters, versions, key settings, connections, database sizes and who may connect, `pg_hba`
rules, login roles; all inside a read-only transaction), SSH, firewall and fail2ban posture, automatic
updates and reboots. It was run on a Linux test system as root and as a normal user; both complete.

## 4. Decision rules for the script's output

Staging on the production host only if **every** line holds; otherwise a separate server:

| From the output | Required |
|---|---|
| `nproc`; `vmstat` idle; `cpu pressure` avg300 | 2 or more vCPU; production idle ≥ 50% across the sample; CPU `some avg300` below 10 |
| `MemAvailable`; OOM events; swap (`si`/`so`, `SwapFree`) | ≥ 2.5 GiB available (1 GiB application, 0.5 GiB staging PostgreSQL, 1 GiB margin for production's own peaks); no OOM in 30 days; no active swapping |
| `df` for `/` (and `/var`) | ≥ 10 GiB free and below 80% used after staging's ~3 GiB |
| Ports | 8002 free and named by no unit or Nginx site; otherwise 8003 under the same test |
| Units and directories | no surviving `kalillac-v31-staging` left running on the chosen port (if present, the owner decides about it; it is left untouched) |
| PostgreSQL | a version-16 install present for `pg_createcluster 16 staging -p 5433`, port 5433 free; production's cluster is not touched |
| Production's unit `User=` | recorded either way; if production runs as root, a staging escalation is one step from production's secrets, which weighs strongly toward a separate server |

**Reasons to prefer a separate server even if the numbers pass** (these are design facts, not
measurements): staging runs the newest, not yet independently verified code, including the parser of
uploaded files and images, on the same kernel as production's credentials and database; disk, page cache
and kernel are shared even under cgroup limits, and a full disk stops both; production's unit is named
`kalillac-ai-fastapi-staging.service`, so an operator error on a shared host has an obvious way to hit the
wrong service; host upgrades and reboots for staging interrupt production. A separate small server
removes all four, and is a new monthly cost that needs the owner's approval.

**Provisional recommendation:** a separate small server (2 vCPU, 4 GiB, ~50 GiB), because of those
four facts; the shared host is acceptable only when the script's output meets every rule above, with the
limits in section 2. The final recommendation is made from the measurements, not from this paragraph.

**Cloudflare Access (item 7):** feasible either way. Create `staging.kalillac.ai` (proxied) to the
chosen origin, an Nginx server block for it on the staging port with `X-Robots-Tag: noindex`, and an Access
application limited to the owner's identity. One exception is required: Stripe's webhooks cannot pass
Access, so an Access **bypass** policy for exactly `POST /api/billing/stripe/webhook` on that hostname
(the application verifies Stripe's signature and the test mode on every event). Keep the origin dropping
non-Cloudflare traffic (measured above for production; a separate server needs the same rule).

## 5. One coherent staging release: the integration that is needed

Checked 2026-11-18 against `origin`, by ancestry and by patch identity (`git cherry`):

* The release line is `feat/staging-preflight` at **`e6aa7a5`**. It contains production's commit
  `54e38ec` and every other branch of the project (156 local and remote refs), except the items below.
  The main checkout's committed head `a2e8482` is contained; its **uncommitted** work is not, and is not
  touched.
* Already present under other ids (nothing to do): `84cc7f1` (CI timeout), `32a0e77` (as `55ae0ff`).
* **Missing, and required:** Codex's verified correction cycle for prose totals and exact-edit
  confirmations, `fix/codex-reverification-3` → `-6` and `docs/codex-reverification-6-closeout`
  (`c5291b4`, `856d9a5`, `0ea08a8`, `78b7a05`, `67c1e25`). Codex verified `78b7a05` resolved on 2026-11-16.
  It forked at `53869d7` (D2 quarantine), before the release line moved on, and was never merged into it.
  A release from `e6aa7a5` would lack four independently verified fixes in `kalillac_files`
  (`router.py`, `assistant.py`, `editing.py`).
* Optional: `docs/e1b-billing-authority-design` (`8fd0cb5`, documentation only). Not included:
  `frontend/replit-ui` (`ebe6a2e`, 2026-10-03), a React/Vite app added under `frontend/` (76 files), a
  parallel approach to the `site/` application the release uses; never part of a release. Excluded unless
  the owner wants it.

**Done 2026-11-18** on the owner's authorization: `release/staging-v1`, merge `c832fcb`, release head `7d857c5aaa35b8877db1deb33286daceb3af5d9e`, every applicable workflow green (Codex handoff section 14). The steps as planned:

1. Branch `release/staging-v1` from `e6aa7a5`; merge `origin/docs/codex-reverification-6-closeout` (a merge
   commit, so both histories and Codex's audited commit `78b7a05` stay intact).
2. `git merge-tree` shows one textual conflict only: the pinned counts in
   `.github/workflows/file-generation-verification.yml`. Merged expectation: fork point 836, plus the
   release line's 23 (19 image, 4 allowance hook), plus the corrections' 194 (189 editing, 5 router)
   = **1,053**; confirmed by `pytest --collect-only` before committing.
3. `router.py` and `assistant.py` merge automatically but were changed on both sides (atomic confirmation
   claims; allowance counting and images), so the merged behaviour is new code: run every workflow on the
   merge (file generation, subscription with PostgreSQL and the root boot verifier, frontend, provider
   ceilings, anonymous allowance, safety, Spaces) and the PostgreSQL file-allowance and Premium journeys.
4. Codex re-audits the merge commit: its verification of `78b7a05` and its pending audits of the release
   line apply to those commits, not to their combination. No finding is marked resolved by this merge.
5. The staging release is that merge commit's full SHA (`KALILLAC_RELEASE_SHA`), built per the runbook.

## 6. What the owner needs to decide or supply to begin staging

| # | Needed | Why |
|---|---|---|
| 1 | SSH user for 134.122.5.134 and a go-ahead, or the owner runs section 3 and shares the output | the host measurements |
| 2 | Same host or a separate server (a separate one is a new monthly cost) | section 4 |
| 3 | Approval of the integration in section 5 (`release/staging-v1`) | one coherent release |
| 4 | Staging hostname (`staging.kalillac.ai` is free) and the Cloudflare Access identity (owner's email) | access |
| 5 | Stripe **test-mode** secret key, webhook signing secret (after the endpoint exists), one recurring test Price id | billing in test mode (`docs/STRIPE_TEST_MODE_SETUP.md`) |
| 6 | Staging's own OpenAI, Brave, Tavily and Resend keys, each with its own spending limit, and Resend's sender for the staging hostname | providers, never production's keys |
| 7 | The values behind every `<OWNER_...>` placeholder: daily allowances, provider attempt ceilings, budgets (decision 1; the proposal is `docs/FINANCE_BUDGET_PROPOSAL.md`), which reasoning modes each tier gets (decision 9), DEEP's extra tokens | the settings template |
| 8 | Provider prices on the day (decision 2) and production's current request and transport limits (read from production's unit by an operator) | the `<PRICE_...>` and `<PROD_...>` values |
| 9 | What to do with any surviving `kalillac-v31-staging` (only if the script finds one) | port and clean-up |
| 10 | The Spaces beta account ids: the owner's own staging accounts, after they register | Spaces in staging |

Codex's outstanding findings stay open throughout: the allowance correction and every checkpoint since D3
await independent verification, and the merge in section 5 adds one more audit, it resolves none.
