# LeetStreak — notes for whoever picks this up next

A private, group-based LeetCode streak tracker. GitHub-style green squares for your own
habit, with your friends' squares on the same board. Built and deployed over a few days
in September 2026; live and in use by a handful of people.

**Live:** <https://leetstreak-v6cv.onrender.com> · **Repo:** `Hcy-43/LeetStreak`

The owner and their friends are all in **Pittsburgh**. That matters more than it sounds —
see *Time* below, which is where most of the bugs came from.

---

## Running it

```bash
createdb leetstreak_dev && createdb leetstreak_test
uv sync
uv run uvicorn app.main:app --reload
uv run pytest                        # 342 tests, needs leetstreak_test
uv run python scripts/seed_demo.py   # five fake people in a demo group
```

Sign in as `dana@example.com` / `demo-password-1`.

Needs a running PostgreSQL (Postgres.app is installed on this machine). The schema is
created on boot — there is no migration command to run.

**There is no Node on this machine.** No npm, bun or deno. The stack is Python with no
build step and no client framework, and that was a deliberate consequence. Don't reach
for a bundler.

---

## Shape of it

```
app/
  main.py         routes, onboarding, flash + redirect helpers
  auth.py         session cookies, Google + GitHub OAuth exchanges
  passwords.py    scrypt hashing and password policy
  verification.py emailed sign-up / reset codes
  mailer.py       SMTP, Resend, or console fallback
  ratelimit.py    sign-in and lookup throttles
  board.py        assembles a group board from stored activity
  streaks.py      streak maths and grid building — pure functions, heavily tested
  sync.py         refresh orchestration, staleness, concurrency
  store.py        all SQL
  db.py           schema, migrations, connection pool
  sources/        leetcode.py, github.py
tests/            342 tests, run against a real Postgres, never the network
scripts/          seed_demo.py, set_password.py
```

`streaks.py` is pure functions over `{date: count}` maps — no database, no clock. That is
why the streak rules are cheap to change and cheap to trust. Keep it that way.

---

## Time — read this before touching anything date-shaped

Most of the bugs in this project were one of these. LeetCode's submission calendar gives
**day-and-count in UTC, with no timestamps.** That single fact drives the design.

- **Group boards run on the group's timezone.** Each group has a `timezone` column set by
  its owner. Every member is measured against that one midnight, or "4 of 5 solved today"
  means something different to each person reading it.
- **Recent days are re-cut from real timestamps.** `recentAcSubmissionList` gives up to
  **20** accepted submissions *with* timestamps. `board.reconcile()` prefers those for the
  days they cover and falls back to the UTC calendar for older history. Without this, a
  20:56 solve in Pittsburgh is dated the next UTC day and lands on the wrong square.
- **A streak needs a solve, not an attempt.** The calendar counts every submission
  including failures; the timestamped list counts only accepted ones. Inside the window
  the latter wins, so a day of failed attempts does not hold a streak up. Deliberate —
  see the README.
- **Nothing after `today` is ever counted.** `compute_stats` clamps to `day <= today`.
  Before that, future-dated UTC activity inflated "longest" and "active days" while the
  grid, which did hide future days, showed nothing — the numbers and the squares
  disagreed on screen.
- **Each member is measured from the day they joined**, not the day the group started.
  `MemberBoard.since` is `max(group.created_at, membership.joined_at)`.

If you change the day boundary anywhere, **change it everywhere**: squares, streaks,
totals, the problem list, and the review queue all read the same data. Twice now, one of
them was left on UTC while the others moved, and the symptom was subtle — a problem
visible on the home page but missing from a group board.

---

## Conventions worth keeping

**Migrations are append-only.** `db.MIGRATIONS` is a list; its index is the version
recorded in `schema_migrations`. Inserting one in the middle renumbers every migration
after it, so the database treats the new one as already applied and silently skips it.
This has already happened once. Always append.

**Tests run against a real Postgres**, not a stand-in — that was the point of porting off
SQLite. `tests/conftest.py` truncates between tests. They never touch the network;
sources are stubbed.

**Secrets live in Render's Environment tab and nowhere else.** `.env` is gitignored and
should hold only local development values. A production `DATABASE_URL` was once pasted
into it, one deleted line away from `seed_demo.py` running against real accounts —
`db.is_local()` and the seed script's refusal now guard that, but don't rely on it.

**Verify before asserting.** Several confident claims in this project turned out to be
stale — Fly's free tier, Render's cron pricing, SendGrid's free plan. Check the current
docs rather than trusting recall.

---

## Deployment

Render (free web service, Docker) + Neon (free Postgres, `us-east-2`). `render.yaml` is
the blueprint but the service was created by hand, so the dashboard is the source of
truth. `DEPLOY.md` has the full runbook.

Two cron-job.org jobs exist: **LeetStreak Pin** hits `/healthz` every 10 minutes so the
free service never sleeps, and an hourly job survives from the removed reminder feature.

**Render blocks outbound SMTP on free instances** (ports 25/465/587, since Sept 2025).
This is why the evening-reminder feature was built and then removed, and it means
**email sign-up is broken in production** — verification codes cannot be delivered.
Everyone currently signs in with Google. Fixing it needs either a paid Render instance or
an HTTP email API (`mailer.py` already has a Resend backend; Resend needs a verified
domain to reach anyone but yourself).

The app **refuses to boot** on an `https://` origin without `SECRET_KEY`, or with
registration open and no mail provider configured. Localhost runs with nothing set.

---

## Feature flags and constants

| Where | What |
| --- | --- |
| `sync.GITHUB_SOURCE_ENABLED = False` | GitHub contributions / LeetHub repo source, parked. Module and tests intact; flip it and restore two settings fields. |
| `store.REVIEW_AFTER_DAYS = 3` | How long before a solved problem returns to the review list. |
| `users.review_from` | Reviews only cover solves from when the feature was switched on, so nobody gets a backlog. |
| `users.show_problems` | Whether a group sees your problem titles or just your squares. |

---

## Known gaps

- **Email sign-up is broken in production** (SMTP blocked). Google sign-in works.
- **Two groups are still on UTC** rather than `America/New_York` — the owner needs to set
  each group's timezone on its own page. Until then those boards roll over at 8pm local.
- **The Neon password and `CRON_TOKEN` were pasted into a chat transcript** and should be
  rotated.
- **The Google consent screen may still be in Testing**, which means friends who are not
  listed as test users get `access_denied`.
- **Problem history cannot be backfilled** — LeetCode exposes only the last 20 accepted
  submissions, so it accumulates forward from the day the app started watching.

---

## What the owner cares about

Small, private, and honest. The board is for five friends keeping each other going, not a
product. Free hosting is a hard constraint that has already shaped two decisions. Prefer
clear behaviour over clever behaviour, and say plainly when something cannot work rather
than shipping a version that looks like it does.
