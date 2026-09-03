from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import httpx
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeSerializer,
    URLSafeTimedSerializer,
)

from . import (
    auth,
    board,
    db,
    mailer,
    passwords,
    ratelimit,
    store,
    sync,
    verification,
)
from .config import BASE_DIR, get_settings, using_ephemeral_secret
from .sources import (
    ProfileNotFound,
    SourceError,
    leetcode as leetcode_source,
)

log = logging.getLogger("leetstreak")

FLASH_COOKIE = "sb_flash"
SIGNUP_COOKIE = "sb_signup"
# Long enough to read an email and come back, short enough that an abandoned signup
# on a shared machine does not linger.
SIGNUP_MAX_AGE = 30 * 60

COMMON_TIMEZONES = [
    "UTC",
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Moscow",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Taipei",
    "Asia/Tokyo",
    "Australia/Sydney",
]


def timezone_choices(current: str | None) -> list[str]:
    """The short list, plus whatever this account is already set to."""
    zones = list(COMMON_TIMEZONES)
    if current and current not in zones and current in available_timezones():
        zones.append(current)
    return zones


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db.configure(settings.database_url)
    db.init_db()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # On localhost these are warnings. On a real origin they are refusals: both
    # produce an app that looks fine and is quietly broken for everyone using it.
    deployed = settings.base_url.startswith("https://")
    if using_ephemeral_secret():
        if deployed:
            raise RuntimeError(
                "SECRET_KEY is unset. Every restart would sign everyone out and "
                "invalidate half-finished signups. Generate one with:\n"
                "  python3 -c 'import secrets; print(secrets.token_hex(32))'"
            )
        log.warning(
            "SECRET_KEY is unset - using a random key. Sessions will not survive a restart."
        )
    providers = ["email+password"]
    if settings.google_oauth_enabled:
        providers.append("Google")
    if settings.github_oauth_enabled:
        providers.append("GitHub")
    log.info(
        "Sign-in: %s. Registration is %s. Mail backend: %s.",
        ", ".join(providers),
        "open" if settings.allow_registration else "closed",
        settings.mail_backend,
    )
    if not settings.email_delivery_configured and deployed and settings.allow_registration:
        raise RuntimeError(
            "Registration is open but no mail provider is configured, so verification "
            "codes would go to this log and nobody could ever finish signing up. Set "
            "SMTP_HOST or RESEND_API_KEY (see README), or set ALLOW_REGISTRATION=false."
        )
    if not settings.email_delivery_configured:
        log.warning(
            "No mail provider configured: verification codes will be written to this log "
            "instead of being delivered. Set SMTP_HOST (see README) before letting anyone "
            "else sign up."
        )
    yield

    db.close()


app = FastAPI(title="LeetStreak", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _asset_version() -> str:
    """Newest mtime across static files, so an edited stylesheet gets a new URL.

    Without this a browser happily keeps a cached style.css after a deploy and the
    page renders with the old rules, which looks exactly like a broken app.
    """
    static = BASE_DIR / "static"
    try:
        newest = max(f.stat().st_mtime_ns for f in static.rglob("*") if f.is_file())
    except ValueError:
        return "0"
    return format(newest, "x")[-10:]


templates.env.globals["asset_version"] = _asset_version()


# ------------------------------------------------------------------------- helpers


def _flash_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="flash")


def flash(response: Response, message: str, kind: str = "notice") -> None:
    response.set_cookie(
        FLASH_COOKIE,
        _flash_serializer().dumps({"m": message, "k": kind}),
        max_age=30,
        httponly=True,
        samesite="lax",
        path="/",
    )


def take_flash(request: Request) -> dict[str, str] | None:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        payload = _flash_serializer().loads(raw)
    except BadSignature:
        return None
    return {"message": payload.get("m", ""), "kind": payload.get("k", "notice")}


# Onboarding is the one moment someone stares at the grid wondering whether it
# worked, so it is worth waiting for the first fetch rather than redirecting them
# to an empty board. Capped, because a slow LeetCode must not hold the request.
FIRST_SYNC_WAIT_SECONDS = 8


async def sync_before_redirect(user: dict[str, Any], settings) -> None:
    try:
        await asyncio.wait_for(
            sync.sync_users([user], settings), FIRST_SYNC_WAIT_SECONDS
        )
    except (TimeoutError, asyncio.TimeoutError):
        # Finish in the background; the board fills in on the next page view.
        log.info("first sync for user %s is slow, backgrounding it", user["id"])
        sync.schedule([user], settings)


def redirect(url: str, message: str = "", kind: str = "notice") -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    if message:
        flash(response, message, kind)
    else:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def render(request: Request, template: str, context: dict[str, Any], status: int = 200):
    settings = get_settings()
    payload = {
        "request": request,
        "settings": settings,
        "flash": take_flash(request),
        "user": context.pop("user", None),
        **context,
    }
    response = templates.TemplateResponse(request, template, payload, status_code=status)
    response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


def viewer_today(user: dict[str, Any] | None):
    name = (user or {}).get("timezone") or "UTC"
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return datetime.now(zone).date()


def require_user(request: Request) -> dict[str, Any] | None:
    return auth.current_user(request)


# -------------------------------------------------------------------------- routes


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return render(request, "privacy.html", {"user": require_user(request)})


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return render(request, "terms.html", {"user": require_user(request)})


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, range: str = Query(default="1y")):
    """Your own streak first, your groups underneath."""
    settings = get_settings()
    user = require_user(request)
    if not user:
        return render(request, "landing.html", {})

    if not user.get("leetcode_username"):
        return redirect("/welcome")

    sync.refresh_stale_in_background([user], settings)

    me = board.build_board([user], viewer_today(user), range).members[0]
    return render(
        request,
        "home.html",
        {
            "user": user,
            "me": me,
            "groups": store.groups_for_user(user["id"]),
            "range_options": board.RANGE_OPTIONS,
        },
    )


@app.get("/login", response_class=HTMLResponse)
@app.get("/register", response_class=HTMLResponse)
async def legacy_auth_pages(request: Request, next: str = Query(default="/")):
    """Both old entry points now lead to the one email-first page."""
    return redirect(f"/start?next={safe_next(next)}")


# ------------------------------------------------------- one page for sign in + sign up


def _signup_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="signup")


def set_signup_state(
    response: Response,
    email: str,
    verified: bool,
    purpose: str = verification.PURPOSE_REGISTER,
) -> None:
    response.set_cookie(
        SIGNUP_COOKIE,
        _signup_serializer().dumps(
            {"email": email, "verified": verified, "purpose": purpose}
        ),
        max_age=SIGNUP_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().base_url.startswith("https://"),
        path="/",
    )


def read_signup_state(request: Request) -> dict[str, Any]:
    raw = request.cookies.get(SIGNUP_COOKIE)
    if not raw:
        return {}
    try:
        return _signup_serializer().loads(raw, max_age=SIGNUP_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return {}


def start_page(
    request: Request,
    step: str,
    next_url: str,
    *,
    email: str = "",
    error: str = "",
    notice: str = "",
    form: dict[str, Any] | None = None,
    status: int = 200,
):
    return render(
        request,
        "start.html",
        {
            "step": step,
            "next_url": next_url,
            "email": email,
            "error": error,
            "notice": notice,
            "form": form or {},
            "code_ttl_minutes": verification.TTL_MINUTES,
        },
        status=status,
    )


@app.get("/start", response_class=HTMLResponse)
async def start(request: Request, next: str = Query(default="/")):
    if require_user(request):
        return redirect(safe_next(next))
    response = start_page(request, "email", safe_next(next))
    response.delete_cookie(SIGNUP_COOKIE, path="/")
    return response


@app.post("/start")
async def start_email(
    request: Request, email: str = Form(default=""), next: str = Form(default="/")
):
    """The fork: known address goes to a password prompt, new one starts a signup."""
    settings = get_settings()
    destination = safe_next(next)
    address = store.normalise_email(email)

    if problem := store.problem_with_email(address):
        return start_page(request, "email", destination, email=email, error=problem, status=400)

    # This endpoint reveals whether an address is registered, which is the price of the
    # one-box flow every large site uses. Throttle it so it cannot be enumerated in bulk.
    ip_key = f"start:{request.client.host if request.client else 'unknown'}"
    if ratelimit.is_blocked(ip_key, limit=ratelimit.LOOKUP_ATTEMPTS):
        return start_page(
            request,
            "email",
            destination,
            email=email,
            error="Too many attempts from this device. Try again shortly.",
            status=429,
        )
    ratelimit.record_failure(ip_key)

    user = store.get_user_by_email(address)

    if user and user.get("password_hash"):
        return start_page(request, "password", destination, email=address)

    if user:
        providers = [p for p in store.identity_providers(user["id"]) if p != "password"]
        return start_page(request, "provider", destination, email=address, form={"providers": providers})

    if not settings.allow_registration:
        return start_page(
            request,
            "email",
            destination,
            email=address,
            error="This server is not accepting new accounts.",
            status=403,
        )

    try:
        await send_verification_code(settings, address)
    except mailer.MailError as exc:
        return start_page(request, "email", destination, email=address, error=str(exc), status=502)

    response = start_page(
        request, "verify", destination, email=address,
        notice=f"We sent a {verification.CODE_DIGITS}-digit code to {address}.",
    )
    set_signup_state(response, address, verified=False)
    return response


async def send_verification_code(
    settings, address: str, purpose: str = verification.PURPOSE_REGISTER
) -> None:
    issued = verification.issue(settings.secret_key, address, purpose)
    await mailer.send(
        settings,
        address,
        "Your LeetStreak verification code",
        mailer.verification_body(issued.code, verification.TTL_MINUTES),
    )


@app.post("/start/password")
async def start_password(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default="/"),
):
    destination = safe_next(next)
    address = store.normalise_email(email)
    throttle_key = f"login:{address}"

    if ratelimit.is_blocked(throttle_key):
        minutes = max(1, ratelimit.retry_after_seconds(throttle_key) // 60)
        return start_page(
            request, "password", destination, email=address,
            error=f"Too many failed attempts. Try again in about {minutes} minute(s).",
            status=429,
        )

    user = store.get_user_by_email(address) if address else None
    if user is None or not user.get("password_hash"):
        passwords.waste_time()
        ratelimit.record_failure(throttle_key)
        return start_page(
            request, "password", destination, email=address,
            error="Email or password is incorrect.", status=400,
        )

    if not passwords.verify_password(password, user["password_hash"]):
        ratelimit.record_failure(throttle_key)
        return start_page(
            request, "password", destination, email=address,
            error="Email or password is incorrect.", status=400,
        )

    ratelimit.clear(throttle_key)
    response = redirect(destination, f"Welcome back, {user['display_name'] or user['handle']}.")
    auth.set_session(response, user["id"])
    response.delete_cookie(SIGNUP_COOKIE, path="/")
    return response


@app.post("/start/resend")
async def start_resend(request: Request, next: str = Form(default="/")):
    settings = get_settings()
    destination = safe_next(next)
    state = read_signup_state(request)
    address = state.get("email", "")
    purpose = state.get("purpose", verification.PURPOSE_REGISTER)
    if not address:
        return redirect("/start", "That took too long. Start again.", "error")

    if wait := verification.seconds_until_resend(address, purpose):
        return start_page(
            request, "verify", destination, email=address,
            error=f"Hold on {wait} more second(s) before asking for another code.",
            status=429,
        )

    try:
        await send_verification_code(settings, address, purpose)
    except mailer.MailError as exc:
        return start_page(request, "verify", destination, email=address, error=str(exc), status=502)

    return start_page(
        request, "verify", destination, email=address, notice="New code sent."
    )


@app.post("/start/verify")
async def start_verify(
    request: Request, code: str = Form(default=""), next: str = Form(default="/")
):
    settings = get_settings()
    destination = safe_next(next)
    state = read_signup_state(request)
    address = state.get("email", "")
    purpose = state.get("purpose", verification.PURPOSE_REGISTER)
    if not address:
        return redirect("/start", "That took too long. Start again.", "error")

    try:
        verification.check(settings.secret_key, address, code, purpose)
    except verification.VerificationError as exc:
        return start_page(
            request, "verify", destination, email=address, error=str(exc), status=400
        )

    step = "reset" if purpose == verification.PURPOSE_RESET else "profile"
    response = start_page(request, step, destination, email=address)
    set_signup_state(response, address, verified=True, purpose=purpose)
    return response


@app.post("/start/complete")
async def start_complete(
    request: Request,
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
    leetcode_username: str = Form(default=""),
    display_name: str = Form(default=""),
    next: str = Form(default="/"),
):
    settings = get_settings()
    destination = safe_next(next)
    state = read_signup_state(request)
    address = state.get("email", "")

    # The verified flag lives in a signed cookie, so this cannot be skipped by
    # posting straight to this endpoint.
    if not address or not state.get("verified"):
        return redirect("/start", "Please verify your email address first.", "error")

    leetcode_username = leetcode_username.strip().lstrip("@")
    display_name = display_name.strip()

    def reject(message: str, status: int = 400):
        return start_page(
            request, "profile", destination, email=address, error=message,
            form={"leetcode_username": leetcode_username, "display_name": display_name},
            status=status,
        )

    if problem := passwords.problem_with(password):
        return reject(problem)
    if password != password_confirm:
        return reject("The two passwords do not match.")
    if not leetcode_username:
        return reject("Enter your LeetCode username so we know whose squares to show.")

    try:
        async with httpx.AsyncClient() as client:
            profile = await leetcode_source.fetch_profile(client, leetcode_username)
        leetcode_username = profile.get("username") or leetcode_username
    except ProfileNotFound as exc:
        return reject(str(exc))
    except SourceError:
        log.warning("could not verify LeetCode user %r at signup", leetcode_username)

    try:
        user = store.create_password_user(
            email=address,
            password_hash=passwords.hash_password(password),
            leetcode_username=leetcode_username,
            display_name=display_name,
        )
    except (store.DuplicateEmail, store.DuplicateLeetCode) as exc:
        return reject(str(exc))

    await sync_before_redirect(user, settings)
    response = redirect(destination, f"Welcome, {user['display_name']}.")
    auth.set_session(response, user["id"])
    response.delete_cookie(SIGNUP_COOKIE, path="/")
    return response


@app.post("/start/forgot")
async def start_forgot(
    request: Request, email: str = Form(default=""), next: str = Form(default="/")
):
    """Send a reset code. Only reachable from the password step, which already
    revealed that the address exists, so this leaks nothing new."""
    settings = get_settings()
    destination = safe_next(next)
    address = store.normalise_email(email)
    user = store.get_user_by_email(address) if address else None

    if user is None or not user.get("password_hash"):
        return redirect("/start", "Start again from your email address.", "error")

    if wait := verification.seconds_until_resend(address, verification.PURPOSE_RESET):
        return start_page(
            request, "password", destination, email=address,
            error=f"Hold on {wait} more second(s) before asking for another code.",
            status=429,
        )

    try:
        await send_verification_code(settings, address, verification.PURPOSE_RESET)
    except mailer.MailError as exc:
        return start_page(
            request, "password", destination, email=address, error=str(exc), status=502
        )

    response = start_page(
        request, "verify", destination, email=address,
        notice=f"We sent a {verification.CODE_DIGITS}-digit code to {address}.",
    )
    set_signup_state(response, address, verified=False, purpose=verification.PURPOSE_RESET)
    return response


@app.post("/start/reset")
async def start_reset(
    request: Request,
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
    next: str = Form(default="/"),
):
    settings = get_settings()
    destination = safe_next(next)
    state = read_signup_state(request)
    address = state.get("email", "")

    # Same guard as signup: the verified flag is in a signed cookie, so posting
    # straight here without holding the emailed code gets you nothing.
    if (
        not address
        or not state.get("verified")
        or state.get("purpose") != verification.PURPOSE_RESET
    ):
        return redirect("/start", "Please verify your email address first.", "error")

    user = store.get_user_by_email(address)
    if user is None:
        return redirect("/start", "That account no longer exists.", "error")

    def reject(message: str):
        return start_page(
            request, "reset", destination, email=address, error=message, status=400
        )

    if problem := passwords.problem_with(password):
        return reject(problem)
    if password != password_confirm:
        return reject("The two passwords do not match.")

    store.set_password_hash(user["id"], passwords.hash_password(password))
    # A reset is the recovery path for a possibly-stolen account, so drop the
    # throttle counter that a would-be attacker may have run up.
    ratelimit.clear(f"login:{address}")

    response = redirect(destination, "Password changed. You are signed in.")
    auth.set_session(response, user["id"])
    response.delete_cookie(SIGNUP_COOKIE, path="/")
    return response


# ------------------------------------------------------------------------ onboarding


@app.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request, next: str = Query(default="/")):
    """One question, asked once: which LeetCode account are we watching?

    Signing up with email already collects this, so in practice this is where a fresh
    Google account lands. `next` is threaded through because people arrive here from
    an invite link, and dropping it would strand them on their own board having
    forgotten the group they were invited to.
    """
    user = require_user(request)
    destination = safe_next(next)
    if not user:
        return redirect(f"/start?next=/welcome")
    if user.get("leetcode_username"):
        return redirect(destination)
    return render(
        request,
        "welcome.html",
        {"user": user, "error": "", "value": "", "next_url": destination},
    )


@app.post("/welcome")
async def save_welcome(
    request: Request,
    leetcode_username: str = Form(default=""),
    next: str = Form(default="/"),
):
    user = require_user(request)
    if not user:
        return redirect("/start?next=/welcome")

    settings = get_settings()
    destination = safe_next(next)
    leetcode_username = leetcode_username.strip().lstrip("@")

    def reject(message: str):
        return render(
            request,
            "welcome.html",
            {
                "user": user,
                "error": message,
                "value": leetcode_username,
                "next_url": destination,
            },
            status=400,
        )

    if not leetcode_username:
        return reject("Enter your LeetCode username so we know whose squares to show.")

    try:
        async with httpx.AsyncClient() as client:
            profile = await leetcode_source.fetch_profile(client, leetcode_username)
        leetcode_username = profile.get("username") or leetcode_username
    except ProfileNotFound as exc:
        return reject(str(exc))
    except SourceError:
        log.warning("could not verify LeetCode user %r at onboarding", leetcode_username)

    try:
        store.update_profile(
            user["id"],
            display_name=user.get("display_name") or leetcode_username,
            leetcode_username=leetcode_username,
            github_login=user.get("github_login", ""),
            github_repo=user.get("github_repo", ""),
            timezone_name=user.get("timezone") or "UTC",
        )
    except store.DuplicateLeetCode as exc:
        return reject(str(exc))

    refreshed = store.get_user(user["id"])
    await sync_before_redirect(refreshed, settings)
    return redirect(destination, "You are all set.")


# --------------------------------------------------------------------- Google sign-in


@app.get("/auth/google")
async def google_start(request: Request, next: str = Query(default="/")):
    settings = get_settings()
    if not settings.google_oauth_enabled:
        return redirect("/start", "Google sign-in is not configured on this server.", "error")
    response = RedirectResponse("https://accounts.google.com", status_code=303)
    response.headers["location"] = auth.begin_oauth(response, "google", safe_next(next))
    return response


@app.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
):
    settings = get_settings()
    if error:
        return redirect("/start", f"Google sign-in was cancelled ({error}).", "error")
    try:
        next_url = auth.verify_state(request, state, "google")
        profile = await auth.exchange_google_code(code)
    except auth.AuthError as exc:
        return redirect("/start", str(exc), "error")

    address = store.normalise_email(profile.get("email", ""))
    existing = store.get_user_by_identity("google", profile["sub"]) or (
        store.get_user_by_email(address) if address else None
    )
    if existing is None and not settings.allow_registration:
        return redirect("/start", "This server is not accepting new accounts.", "error")

    user = store.upsert_oauth_user(
        provider="google",
        subject=profile["sub"],
        handle=address.split("@")[0] if address else f"google{profile['sub'][:8]}",
        display_name=profile.get("name") or "",
        avatar_url=profile.get("picture") or "",
        email=address,
    )

    # New Google accounts have no LeetCode username yet, and the board is useless
    # without one.
    onward = safe_next(next_url)
    destination = onward if user.get("leetcode_username") else f"/welcome?next={quote(onward)}"
    response = redirect(destination, f"Signed in as {user['display_name'] or user['handle']}.")
    auth.set_session(response, user["id"])
    response.delete_cookie(auth.STATE_COOKIE, path="/")
    return response


@app.get("/auth/github")
async def github_start(request: Request, next: str = Query(default="/")):
    settings = get_settings()
    if not settings.github_oauth_enabled:
        return redirect("/start", "GitHub sign-in is not configured on this server.", "error")
    response = RedirectResponse("https://github.com", status_code=303)
    url = auth.begin_oauth(response, safe_next(next))
    response.headers["location"] = url
    return response


@app.get("/auth/github/callback")
async def github_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
):
    if error:
        return redirect("/start", f"GitHub sign-in was cancelled ({error}).", "error")
    try:
        next_url = auth.verify_state(request, state)
        profile = await auth.exchange_code(code)
    except auth.AuthError as exc:
        return redirect("/start", str(exc), "error")

    login = profile.get("login") or ""
    user = store.upsert_oauth_user(
        provider="github",
        subject=str(profile.get("id")),
        handle=login or f"gh{profile.get('id')}",
        display_name=profile.get("name") or login,
        avatar_url=profile.get("avatar_url") or "",
        github_login=login,
    )
    onward = safe_next(next_url)
    destination = onward if user.get("leetcode_username") else f"/welcome?next={quote(onward)}"
    response = redirect(destination, f"Welcome, {user['display_name'] or user['handle']}.")
    auth.set_session(response, user["id"])
    response.delete_cookie(auth.STATE_COOKIE, path="/")
    return response


@app.post("/logout")
async def logout():
    response = redirect("/", "Signed out.")
    auth.clear_session(response)
    return response


# ------------------------------------------------------------------------ settings


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = require_user(request)
    if not user:
        return redirect("/start?next=/settings")
    states = store.sync_state_for_users([user["id"]]).get(user["id"], [])
    return render(
        request,
        "settings.html",
        {
            "user": user,
            "timezones": timezone_choices(user.get("timezone")),
            "sync_states": states,
            "providers": store.identity_providers(user["id"]),
        },
    )


@app.post("/settings")
async def save_settings(
    request: Request,
    display_name: str = Form(default=""),
    leetcode_username: str = Form(default=""),
    timezone_name: str = Form(default="UTC", alias="timezone"),
):
    user = require_user(request)
    if not user:
        return redirect("/start?next=/settings")

    settings = get_settings()
    leetcode_username = leetcode_username.strip().lstrip("@")

    # Never let a blank field unlink an account. The form always sends this, so an
    # empty value means a truncated or stale POST, not a considered choice - and
    # silently wiping it drops the person out of their group's "solved today" count.
    if not leetcode_username:
        return redirect(
            "/settings", "Enter your LeetCode username - it cannot be blank.", "error"
        )

    if timezone_name not in available_timezones():
        return redirect("/settings", f"Unknown timezone '{timezone_name}'.", "error")

    if leetcode_username and leetcode_username != user.get("leetcode_username"):
        try:
            async with httpx.AsyncClient() as client:
                await leetcode_source.fetch_profile(client, leetcode_username)
        except SourceError as exc:
            return redirect("/settings", str(exc), "error")

    try:
        store.update_profile(
            user["id"],
            # Display name is optional; the LeetCode handle is the natural fallback.
            display_name=display_name.strip() or leetcode_username or user["handle"],
            leetcode_username=leetcode_username,
            github_login=user.get("github_login", ""),
            github_repo=user.get("github_repo", ""),
            timezone_name=timezone_name,
        )
    except store.DuplicateLeetCode as exc:
        return redirect("/settings", str(exc), "error")

    refreshed = store.get_user(user["id"])
    await sync.sync_users([refreshed], settings)
    return redirect("/settings", "Saved. Your activity has been refreshed.")


@app.post("/settings/password")
async def change_password(
    request: Request,
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    new_password_confirm: str = Form(default=""),
):
    user = require_user(request)
    if not user:
        return redirect("/start?next=/settings")
    if not user.get("password_hash"):
        return redirect(
            "/settings", "This account signs in with GitHub, so it has no password.", "error"
        )

    throttle_key = f"password:{user['id']}"
    if ratelimit.is_blocked(throttle_key):
        return redirect("/settings", "Too many attempts. Try again later.", "error")

    if not passwords.verify_password(current_password, user["password_hash"]):
        ratelimit.record_failure(throttle_key)
        return redirect("/settings", "Your current password is not correct.", "error")

    if problem := passwords.problem_with(new_password):
        return redirect("/settings", problem, "error")
    if new_password != new_password_confirm:
        return redirect("/settings", "The two new passwords do not match.", "error")

    ratelimit.clear(throttle_key)
    store.set_password_hash(user["id"], passwords.hash_password(new_password))

    # Signing sessions carries no password material, so other devices stay signed in.
    return redirect("/settings", "Password changed.")


# -------------------------------------------------------------------------- groups


@app.post("/groups")
async def create_group(request: Request, name: str = Form(...)):
    user = require_user(request)
    if not user:
        return redirect("/start")
    cleaned = name.strip()[:60]
    if not cleaned:
        return redirect("/", "Give your group a name.", "error")
    group = store.create_group(cleaned, user["id"])
    return redirect(f"/g/{group['id']}", f"Created '{group['name']}'. Share the invite link.")


@app.get("/g/{group_id}", response_class=HTMLResponse)
async def group_board(
    request: Request, group_id: int, range: str = Query(default=board.DEFAULT_RANGE)
):
    settings = get_settings()
    user = require_user(request)
    if not user:
        return redirect(f"/start?next=/g/{group_id}")

    group = store.get_group(group_id)
    if not group or not store.is_member(group_id, user["id"]):
        return render(request, "not_found.html", {"user": user}, status=404)

    members = store.members_of(group_id)
    sync.refresh_stale_in_background(members, settings)

    started = store.parse_iso(group["created_at"])
    data = board.build_board(
        members,
        viewer_today(user),
        range,
        since=started.date() if started else None,
    )
    invite_url = f"{settings.base_url}/join/{group['invite_code']}"
    return render(
        request,
        "group.html",
        {
            "user": user,
            "group": group,
            "board": data,
            "invite_url": invite_url,
            "is_owner": group["owner_id"] == user["id"],
            "range_options": board.RANGE_OPTIONS,
        },
    )


@app.post("/g/{group_id}/refresh")
async def refresh_group(request: Request, group_id: int):
    user = require_user(request)
    if not user or not store.is_member(group_id, user["id"]):
        return redirect("/", "You are not in that group.", "error")
    members = store.members_of(group_id)
    await sync.sync_users(members, get_settings())
    return redirect(f"/g/{group_id}", "Refreshed everyone's activity.")


@app.post("/g/{group_id}/rename")
async def rename_group(request: Request, group_id: int, name: str = Form(...)):
    user = require_user(request)
    group = store.get_group(group_id)
    if not user or not group or group["owner_id"] != user["id"]:
        return redirect("/", "Only the group owner can do that.", "error")
    cleaned = name.strip()[:60]
    if not cleaned:
        return redirect(f"/g/{group_id}", "Group name cannot be empty.", "error")
    store.rename_group(group_id, cleaned)
    return redirect(f"/g/{group_id}", "Group renamed.")


@app.post("/g/{group_id}/invite/rotate")
async def rotate_invite(request: Request, group_id: int):
    user = require_user(request)
    group = store.get_group(group_id)
    if not user or not group or group["owner_id"] != user["id"]:
        return redirect("/", "Only the group owner can do that.", "error")
    store.rotate_invite(group_id)
    return redirect(f"/g/{group_id}", "New invite link generated. The old one no longer works.")


@app.post("/g/{group_id}/leave")
async def leave_group(request: Request, group_id: int):
    user = require_user(request)
    group = store.get_group(group_id)
    if not user or not group:
        return redirect("/")
    if group["owner_id"] == user["id"]:
        return redirect(
            f"/g/{group_id}",
            "You own this group. Delete it instead, or hand it over first.",
            "error",
        )
    store.remove_member(group_id, user["id"])
    return redirect("/", f"You left '{group['name']}'.")


@app.post("/g/{group_id}/members/{member_id}/remove")
async def remove_member(request: Request, group_id: int, member_id: int):
    user = require_user(request)
    group = store.get_group(group_id)
    if not user or not group or group["owner_id"] != user["id"]:
        return redirect("/", "Only the group owner can do that.", "error")
    if member_id == group["owner_id"]:
        return redirect(f"/g/{group_id}", "The owner cannot be removed.", "error")
    store.remove_member(group_id, member_id)
    return redirect(f"/g/{group_id}", "Member removed.")


@app.post("/g/{group_id}/delete")
async def delete_group(request: Request, group_id: int):
    user = require_user(request)
    group = store.get_group(group_id)
    if not user or not group or group["owner_id"] != user["id"]:
        return redirect("/", "Only the group owner can do that.", "error")
    store.delete_group(group_id)
    return redirect("/", f"Deleted '{group['name']}'.")


@app.get("/join/{code}", response_class=HTMLResponse)
async def join_preview(request: Request, code: str):
    group = store.get_group_by_invite(code)
    user = require_user(request)
    if not user:
        return redirect(f"/start?next=/join/{code}")
    if not group:
        return render(request, "not_found.html", {"user": user}, status=404)
    if store.is_member(group["id"], user["id"]):
        return redirect(f"/g/{group['id']}", "You are already in this group.")
    return render(
        request,
        "join.html",
        {"user": user, "group": group, "code": code, "members": store.members_of(group["id"])},
    )


@app.post("/join/{code}")
async def join_group(request: Request, code: str):
    user = require_user(request)
    if not user:
        return redirect(f"/start?next=/join/{code}")
    group = store.get_group_by_invite(code)
    if not group:
        return redirect("/", "That invite link is not valid any more.", "error")
    store.add_member(group["id"], user["id"])
    board = f"/g/{group['id']}"
    destination = board if user.get("leetcode_username") else f"/welcome?next={quote(board)}"
    return redirect(destination, f"You joined '{group['name']}'.")


# ----------------------------------------------------------------------------- api


@app.post("/api/cron/sync")
async def cron_sync(request: Request):
    settings = get_settings()
    if not settings.cron_token:
        return JSONResponse({"error": "CRON_TOKEN is not configured"}, status_code=404)
    header = request.headers.get("authorization", "")
    supplied = header.removeprefix("Bearer ").strip()
    if supplied != settings.cron_token:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    users = store.all_trackable_users()
    results = await sync.sync_users(users, settings)
    return JSONResponse(
        {
            "synced": len(results),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "results": {str(k): v for k, v in results.items()},
        }
    )


@app.get("/api/g/{group_id}.json")
async def group_json(
    request: Request, group_id: int, range: str = Query(default=board.DEFAULT_RANGE)
):
    user = require_user(request)
    if not user or not store.is_member(group_id, user["id"]):
        return JSONResponse({"error": "not found"}, status_code=404)

    group = store.get_group(group_id)
    started = store.parse_iso(group["created_at"]) if group else None
    data = board.build_board(
        store.members_of(group_id),
        viewer_today(user),
        range,
        since=started.date() if started else None,
    )
    return JSONResponse(
        {
            "group": {"id": group["id"], "name": group["name"]},
            "today": data.today.isoformat(),
            "members": [
                {
                    "handle": row.user["handle"],
                    "name": row.name,
                    "leetcode_username": row.user.get("leetcode_username") or None,
                    "sources": row.sources,
                    "current_streak": row.stats.current_streak,
                    "longest_streak": row.stats.longest_streak,
                    "active_days": row.stats.active_days,
                    "total_solved": row.stats.total_solved,
                    "done_today": row.stats.done_today,
                    "days": {
                        day.isoformat(): count for day, count in sorted(row.problems.items())
                    },
                }
                for row in data.members
            ],
        }
    )
