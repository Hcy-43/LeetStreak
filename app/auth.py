from __future__ import annotations

import secrets
from typing import Any

import httpx
from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import store
from .config import Settings, get_settings

SESSION_COOKIE = "sb_session"
STATE_COOKIE = "sb_oauth_state"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class AuthError(RuntimeError):
    pass


def _serializer(settings: Settings, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


def _secure(settings: Settings) -> bool:
    return settings.base_url.startswith("https://")


def set_session(response: Response, user_id: int) -> None:
    settings = get_settings()
    token = _serializer(settings, "session").dumps({"uid": user_id})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_secure(settings),
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = _serializer(get_settings(), "session").loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user_id = data.get("uid")
    return store.get_user(user_id) if isinstance(user_id, int) else None


# ---------------------------------------------------------------- GitHub OAuth flow


def begin_oauth(response: Response, provider: str = "github", next_url: str = "/") -> str:
    """Returns the provider's authorize URL and stashes a signed state cookie."""
    settings = get_settings()
    nonce = secrets.token_urlsafe(16)
    payload = _serializer(settings, "oauth-state").dumps(
        {"n": nonce, "next": next_url, "p": provider}
    )
    response.set_cookie(
        STATE_COOKIE,
        payload,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=_secure(settings),
        path="/",
    )
    if provider == "google":
        params = {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": nonce,
            # Ask Google to show the chooser rather than silently reusing one account.
            "prompt": "select_account",
        }
        return f"{GOOGLE_AUTHORIZE}?{httpx.QueryParams(params)}"

    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.oauth_redirect_uri,
        "scope": "read:user",
        "state": nonce,
        "allow_signup": "true",
    }
    return f"{GITHUB_AUTHORIZE}?{httpx.QueryParams(params)}"


def verify_state(request: Request, state: str, provider: str = "github") -> str:
    """Checks the callback state against the cookie; returns the `next` URL."""
    cookie = request.cookies.get(STATE_COOKIE)
    if not cookie:
        raise AuthError("Sign-in expired before it finished. Please try again.")
    try:
        payload = _serializer(get_settings(), "oauth-state").loads(cookie, max_age=600)
    except (BadSignature, SignatureExpired) as exc:
        raise AuthError("Sign-in expired before it finished. Please try again.") from exc
    if not state or not secrets.compare_digest(str(payload.get("n", "")), state):
        raise AuthError("Sign-in state did not match. Please try again.")
    # A state minted for one provider must not be redeemed at another's callback.
    if payload.get("p", "github") != provider:
        raise AuthError("Sign-in state did not match. Please try again.")
    next_url = payload.get("next") or "/"
    return next_url if next_url.startswith("/") else "/"


async def exchange_code(code: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return await _exchange_github(code, settings)
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach GitHub to finish signing you in: {exc}") from exc
    except ValueError as exc:  # malformed JSON
        raise AuthError("GitHub returned something unreadable.") from exc


async def _exchange_github(code: str, settings) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        token_response = await client.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.oauth_redirect_uri,
            },
        )
        if token_response.status_code >= 400:
            raise AuthError("GitHub rejected the sign-in request.")
        token_payload = token_response.json()
        access_token = token_payload.get("access_token")
        if not access_token:
            raise AuthError(token_payload.get("error_description") or "GitHub did not return a token.")

        user_response = await client.get(
            GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "leetstreak",
            },
        )
        if user_response.status_code >= 400:
            raise AuthError("Could not read your GitHub profile.")
        return user_response.json()


async def exchange_google_code(code: str) -> dict[str, Any]:
    """Confidential-client code exchange, then the OIDC userinfo endpoint.

    The token comes straight from Google over TLS using our client secret, so the
    userinfo response is trustworthy without separately verifying an id_token
    signature - which keeps a JWT library out of the dependency list.
    """
    settings = get_settings()
    try:
        return await _exchange_google(code, settings)
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach Google to finish signing you in: {exc}") from exc
    except ValueError as exc:  # malformed JSON
        raise AuthError("Google returned something unreadable.") from exc


async def _exchange_google(code: str, settings) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        token_response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.google_redirect_uri,
            },
        )
        if token_response.status_code >= 400:
            raise AuthError("Google rejected the sign-in request.")
        access_token = token_response.json().get("access_token")
        if not access_token:
            raise AuthError("Google did not return an access token.")

        info_response = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if info_response.status_code >= 400:
            raise AuthError("Could not read your Google profile.")

    profile = info_response.json()
    if not profile.get("sub"):
        raise AuthError("Google did not identify the account.")
    # Only a verified address is safe to match against an existing account.
    if not profile.get("email_verified"):
        raise AuthError("That Google account does not have a verified email address.")
    return profile
