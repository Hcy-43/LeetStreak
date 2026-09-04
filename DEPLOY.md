# Putting LeetStreak on the internet

Everything here is one-time setup, and all of it is free.

Before either, you need two things that only you can create:

1. **A mail provider**, or nobody can finish signing up — the verification code goes
   to the server log instead of their inbox. The app now refuses to boot in this
   state rather than pretending to work.
2. **Google OAuth credentials for the real domain.** The ones in `.env` are
   registered for `http://localhost:8000` and Google will reject them anywhere else.

---

## 1. Get a mail provider

The free tier of [Resend](https://resend.com) is plenty for a friend group and takes
about five minutes: sign up, add your domain (or use their sandbox sender to start),
create an API key. That gives you `RESEND_API_KEY`.

Gmail works too via `SMTP_HOST=smtp.gmail.com`, but it needs an
[App Password](https://support.google.com/accounts/answer/185833) rather than your
account password, and Gmail rate-limits enough to matter if the group grows.

## 2. Generate a secret key

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

This signs session cookies. If it changes, everyone is signed out. Keep it somewhere
you will still have next year.

## 3. Add the production redirect URI to Google

In the [Google Cloud console](https://console.cloud.google.com/apis/credentials),
open your OAuth client and add to **Authorised redirect URIs**:

```
https://YOUR-DOMAIN/auth/google/callback
```

Keep the localhost one so development still works. The URI must match exactly —
scheme, host, path, no trailing slash.

While the consent screen is in **Testing**, only accounts listed under *Test users*
can sign in. To let anyone in, publish the app.

---

## Deploy: Render + Neon

Two free accounts, no card. The app runs on Render; the database is Neon, because
Render's free tier has no persistent disk.

**1. Create the database.** Sign up at [neon.tech](https://neon.tech), create a
project, and copy the connection string. It looks like
`postgresql://user:pass@ep-xxx.region.aws.neon.tech/neondb?sslmode=require`.

**2. Create the web service.** Push this repo to GitHub, then on
[Render](https://render.com) → **New → Web Service** → connect the repo. It picks up
`render.yaml`. Choose the **Free** plan and set these environment variables:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | the Neon string from step 1 |
| `SECRET_KEY` | the key you generated above |
| `BASE_URL` | `https://YOUR-APP.onrender.com` — no trailing slash |
| `RESEND_API_KEY` | from your mail provider |
| `MAIL_FROM` | `LeetStreak <streaks@your-domain>` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | from the Google console |
| `DB_POOL_MAX` | `3` — Neon's free tier allows few connections |

The schema is created automatically on first boot. There is no migration step to run.

**3. Keep it awake.** A free Render service sleeps after 15 minutes idle and takes
about a minute to wake, which for a daily habit tracker means almost every visit is a
cold start.

Point a free external pinger at `https://YOUR-APP.onrender.com/healthz` every 10
minutes — [cron-job.org](https://cron-job.org) or
[UptimeRobot](https://uptimerobot.com) both do this at no cost. Render's own cron jobs
are $1/month, so they are deliberately not in `render.yaml`.

Staying awake all month uses about 720 of the free tier's 750 instance-hours, so this
has to be the only free service running continuously in your Render workspace.

### What "free" costs you

Neon's free tier suspends a database that goes unused for a few days, so the first
request after a quiet spell is slow. Both free tiers change their terms from time to
time — check before you rely on this for anything you would be upset to lose. Take
backups (below); they are one command.

## If you outgrow the free tier: any VPS with Docker

Not needed to get started — this is the escape hatch if Render's cold starts or
Neon's idle suspension stop being acceptable. `compose.yaml` brings its own
Postgres and Caddy handles TLS.

Point your domain's A record at the server first — Caddy needs it resolving before
it can get a certificate.

```bash
git clone YOUR-REPO && cd leetstreak
cp .env.example .env    # then fill in SECRET_KEY, mail, Google
echo "LEETSTREAK_DOMAIN=streaks.example.com" >> .env
echo "POSTGRES_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_hex(16))')" >> .env
docker compose up -d
```

Caddy handles TLS and renewal. Check it came up with `docker compose logs -f app`.

---

## After it is up

**Test the whole signup once, in a private window**, with an email you can actually
read. This is the only way to catch a broken mail provider, and a broken mail
provider means nobody can join.

**Decide who can register.** `ALLOW_REGISTRATION` defaults to true, so anyone who
finds the URL can create an account. They still cannot see any group they were not
invited to — boards are private to their members — but they will exist as users. Once
your friends are in:

```bash
# Render: set ALLOW_REGISTRATION=false in the dashboard and redeploy.
```

**Back up the database.** One command, and it is the whole app.

```bash
pg_dump "$DATABASE_URL" > backup-$(date +%F).sql
```

Restore with `psql "$DATABASE_URL" < backup.sql`. Neon's free tier does not keep
long backup history, so if the group matters to you, run this occasionally.

**Refreshing activity.** The app re-syncs anyone whose data is stale when their board
is viewed, so this one is optional. Set `CRON_TOKEN` and hit it on a schedule if you
want boards warm before people look:

```bash
curl -X POST https://YOUR-DOMAIN/api/cron/sync -H "Authorization: Bearer $CRON_TOKEN"
```

**Daily reminders.** This one is worth setting up: it is what makes the app reach
people rather than waiting to be opened. Call it **hourly** — the app works out which
groups are at their own local evening (`NUDGE_HOUR`, default 20:00) and emails only
members who have not solved yet. Nobody is emailed twice in a day, and anyone can turn
it off in their settings.

```bash
curl -X POST https://YOUR-DOMAIN/api/cron/nudge -H "Authorization: Bearer $CRON_TOKEN"
```

Add it on cron-job.org next to the keep-alive ping.

The token can go in an `Authorization: Bearer …` header, or — if your scheduler's free
tier has no header fields, as cron-job.org's does not — simply paste the token on its
own into the **request body** box. Both are checked. It is deliberately *not* accepted
as a query parameter, because those end up in access logs.

## When something is wrong

The app fails at boot with an explicit message rather than serving a broken site, so
start with the logs: the **Logs** tab on your Render service, or `docker compose logs app`
on a VPS.

| What you see | Cause |
| --- | --- |
| `SECRET_KEY is unset` | Secret not set on the host. |
| `no mail provider is configured` | Set `RESEND_API_KEY`/`SMTP_HOST`, or close registration. |
| `redirect_uri_mismatch` from Google | The production callback URL is not on the OAuth client, or `BASE_URL` does not match the real hostname. |
| Signup never sends a code | Mail provider rejecting the `MAIL_FROM` domain. Check the logs for the provider's error. |
| Everyone signed out after a deploy | `SECRET_KEY` changed between deploys. |
