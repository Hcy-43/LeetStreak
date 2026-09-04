# LeetStreak

**Daily LeetCode, with your friends watching.**

GitHub-style green squares for your LeetCode habit, with your friends' squares next to
yours. You make a group, send people the invite link, and everyone sees the same board:
who is on a streak, who is about to break one, and who has not solved anything today.

A board is private to its group. There is no global leaderboard and no public profile.

```
Riya Balan    70 day streak   70 longest   248 active days   ▪▪▪▫▪▪▪▪▫▪▪▪▪▪▪▪▪▪
Dana Whitfield 28 day streak  28 longest   234 active days   ▪▫▪▪▪▪▫▪▪▪▪▫▪▪▪▪▪▪
Sam Okonkwo   11 day streak   11 longest   202 active days   ▫▪▪▫▪▫▪▪▫▪▪▪▫▪▪▫▪▪
```

## Quick start

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/). No Node, no build step.

```bash
createdb leetstreak_dev && createdb leetstreak_test
uv sync && uv run uvicorn app.main:app --reload
```

Needs a running PostgreSQL. The schema is created on first boot; there is no
migration command to run.

Open <http://localhost:8000>, register with your email, password and LeetCode username,
then create a group. To see what a populated board looks like before you have any real
data:

```bash
uv run python scripts/seed_demo.py
```

That creates five fake people in a group called "Daily grind crew"; sign in as
`dana@example.com` with password `demo-password-1`.

## Putting it online

See **[DEPLOY.md](DEPLOY.md)** — Render plus a free Neon database, the mail provider and
Google redirect URI you have to set up first. The app refuses to boot on a real
domain without a signing key or a way to send verification codes, rather than
serving a site nobody can sign up for.

## Where the squares come from

**LeetCode** — reads the public submission calendar from `leetcode.com/graphql`, the same
data behind the heatmap on your profile. Needs nothing but your username, and your profile
calendar is public by default.

LeetCode reports its calendar in UTC and we store it exactly as given, rather than
inventing our own boundary — so a day here matches what leetcode.com shows you.

**The clock belongs to the group, not the person.** Each group has a timezone, set by
its owner, and every member's streak and "solved today" is measured against that one
midnight. Otherwise "4 of 5 solved today" would mean something different to each person
reading it. Your own home page borrows the clock from your first group, and falls back
to UTC before you have joined one.

> **GitHub is parked.** An earlier version could also count commits from your GitHub
> contribution graph or a LeetHub/LeetSync solutions repo. That is switched off to keep
> the app to one thing. `app/sources/github.py` and its tests are untouched — set
> `GITHUB_SOURCE_ENABLED = True` in `app/sync.py` and restore the two fields to
> `settings.html` to bring it back. Stored `github_login` / `github_repo` values are
> preserved, not wiped, so nobody loses their settings in the meantime.
>
> GitHub *sign-in* is unaffected and still works if you configure it.

## Accounts

Registration asks for an **email address**, a **password**, a **LeetCode username**, and
optionally a display name — leave that blank and your LeetCode username is used instead.
The LeetCode username is checked against the live API at signup, so a typo is caught
immediately rather than silently producing an empty board. (If LeetCode itself is
unreachable we let the signup through and sort it out at the next sync.)

Passwords are hashed with **scrypt** (`hashlib`, N=2¹⁵, 32 MiB per hash) with a random
per-password salt. Sign-in failures are throttled per address, and an unknown email costs
the same time and returns the same message as a wrong password, so the form cannot be used
to discover who has an account.

Two addresses cannot register the same LeetCode account, which keeps boards honest and
stops casual impersonation.

### One box for sign-in and sign-up

There is a single entry page. You type an email address and the server works out what
happens next: a known address asks for a password, an unknown one starts a signup, and an
address that belongs to a Google-only account says so instead of pretending the password
is wrong.

That fork does reveal whether an address is registered. It is the same trade every large
site makes for this flow, and the lookup is rate-limited per device so it cannot be used
to enumerate your members in bulk.

### Verifying the address

Signing up sends a **6-digit code** to the address before an account exists. The code is
valid for 15 minutes, survives 5 wrong guesses before it burns, and cannot be requested
again for 60 seconds. Only a hash of it is stored — HMAC-SHA256 keyed with `SECRET_KEY`,
with the address and the purpose bound into the digest, so a code cannot be lifted from
the database, replayed against a different address, or spent on a different flow.

Forgotten passwords use the same machinery: **Forgotten your password?** on the password
step emails a fresh code and lets you set a new one. There is also a CLI path for when
mail is broken and someone is locked out:

```bash
uv run python scripts/set_password.py someone@example.com
```

### Sending that email

Set **one** of these, or codes are written to the server log instead of being delivered:

- `SMTP_HOST` (+ `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`) — any SMTP server. For
  Gmail this must be an [App Password](https://support.google.com/accounts/answer/185833),
  not your account password.
- `RESEND_API_KEY` — [Resend](https://resend.com), whose free tier covers a friend group.

The console fallback is deliberate so the app runs on your laptop with no setup, and it
logs a loud warning at startup. **Configure a real one before anyone else signs up**, or
they will never receive their code.

### Google and GitHub sign-in

Both are optional and both are off until you supply credentials.

**Google** — Google Cloud console → APIs & Services → Credentials → OAuth client ID, type
"Web application", authorised redirect URI `<BASE_URL>/auth/google/callback`. Set
`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. We ask only for `openid email profile`.

**GitHub** — create an OAuth app at <https://github.com/settings/developers> with callback
URL `<BASE_URL>/auth/github/callback`, then set `GITHUB_CLIENT_ID` and
`GITHUB_CLIENT_SECRET`. We ask only for `read:user`.

Sign-in methods are stored in a separate `identities` table, so one account can carry
several. Google reports whether it verified the address; when it has, and that address
already has an account here, the two are linked rather than duplicated. An unverified
Google address is never auto-linked. A Google account that is new here still has to supply
a LeetCode username before it gets a board.

Set `ALLOW_REGISTRATION=false` to shut the door once everyone has signed up.

### Why there is no "Sign in with LeetCode"

LeetCode does not publish an OAuth or OIDC provider — no discovery document, no authorize
endpoint, no developer portal. It cannot be used as an identity provider by anyone. So a
LeetCode username here is a claim backed by the uniqueness constraint, not proof of
ownership. If you ever need real proof, the standard trick needs no OAuth: have the person
paste a one-time token into their LeetCode profile bio and read it back through the same
public API.

## Configuration

Copy `.env.example` to `.env`. Everything has a working default except `SECRET_KEY`, which
you should set before exposing this to the internet — without it, sessions are signed with
a random key that changes on every restart and logs everyone out.

`GITHUB_TOKEN` is a classic PAT with **no scopes** (public data only). Without it the
GitHub source is skipped: unauthenticated GitHub allows 60 requests/hour, and the
contributions calendar requires auth outright. LeetCode needs no token.

## Keeping data fresh

Activity is cached in Postgres. Loading a board kicks off a background refresh for anyone
whose data is older than `SYNC_TTL_SECONDS` (default 15 minutes); the page renders from
cache immediately rather than blocking on a slow API. The **Refresh** button forces it and
waits.

To refresh everyone on a schedule, set `CRON_TOKEN` and hit the endpoint:

```bash
curl -X POST -H "Authorization: Bearer $CRON_TOKEN" https://your-host/api/cron/sync
```

## Deploying

See **[DEPLOY.md](DEPLOY.md)** for the full runbook. The short version:

```bash
docker build -t leetstreak .
docker run -d -p 8000:8000 --env-file .env leetstreak
```

The container is stateless — everything lives in the Postgres you point `DATABASE_URL`
at, so it can be restarted, redeployed or scaled without losing anyone. Set `BASE_URL`
to your real origin, since invite links and the OAuth callback are built from it; an
`https://` `BASE_URL` also marks cookies `Secure` and turns two startup warnings into
refusals (see DEPLOY.md).

## API

- `GET /api/g/{group_id}.json` — a board as JSON (streaks, per-day counts), for members only.
- `POST /api/cron/sync` — refresh every tracked user. Bearer `CRON_TOKEN`.
- `GET /healthz`

## Layout

```
app/
  main.py       routes
  auth.py       session cookies, Google + GitHub OAuth
  passwords.py  scrypt hashing and password policy
  verification.py  emailed sign-up / reset codes
  mailer.py     SMTP, Resend, or console fallback
  ratelimit.py  sign-in and lookup throttles
  board.py      assembles a group board from stored activity
  streaks.py    streak math and grid building - pure functions, heavily tested
  sync.py       refresh orchestration, staleness, concurrency
  store.py      SQL
  db.py         Postgres schema, migrations and the connection pool
  sources/      leetcode.py, github.py
tests/          215 tests, run against a real Postgres, no network access
scripts/        seed_demo.py, set_password.py
```

Schema changes go in `db.MIGRATIONS` as a new entry — append only, never edit one that has
shipped.

```bash
uv run pytest
```
