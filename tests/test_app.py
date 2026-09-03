"""End-to-end tests over the real routes, with the network sources stubbed out."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "")
    # Blank these too, or a developer's real .env decides how the tests behave.
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("RESEND_API_KEY", "")

    from app import config, main, passwords, ratelimit, sync

    config.get_settings.cache_clear()
    ratelimit.reset_all()

    # Capture outgoing mail instead of sending it, and make codes predictable.
    sent: list[dict] = []

    async def fake_send(settings, to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(main.mailer, "send", fake_send)
    monkeypatch.setattr(main.verification, "generate_code", lambda: CODE)

    # Nothing in the test suite should reach the network.
    async def no_network(users, settings, concurrency=4):
        return {}

    async def fake_profile(http_client, username):
        if username.startswith("ghost"):
            from app.sources import ProfileNotFound

            raise ProfileNotFound(f"No LeetCode user named '{username}'.")
        return {"username": username, "real_name": "", "avatar_url": "", "total_solved": 0}

    monkeypatch.setattr(sync, "sync_users", no_network)
    monkeypatch.setattr(main.sync, "sync_users", no_network)
    monkeypatch.setattr(main.sync, "schedule", lambda users, settings: None)
    monkeypatch.setattr(main.leetcode_source, "fetch_profile", fake_profile)

    # scrypt is deliberately slow; the tests are not measuring OpenSSL.
    monkeypatch.setattr(passwords, "SCRYPT_N", 2**8)

    with TestClient(main.app) as test_client:
        test_client.sent = sent
        yield test_client

    config.get_settings.cache_clear()
    ratelimit.reset_all()


PASSWORD = "correct-horse-battery"
CODE = "123456"


def start(client: TestClient, email: str, next: str = "/"):
    """Step one of the unified page: hand over an email address."""
    client.cookies.clear()
    return client.post("/start", data={"email": email, "next": next})


def register(
    client: TestClient,
    handle: str,
    password: str = PASSWORD,
    *,
    email: str | None = None,
    code: str = CODE,
    **overrides,
):
    """Walk the whole signup: email -> code -> password + profile.

    Returns as soon as a step does not lead to the next one, so callers can assert on
    whichever rejection they are testing.
    """
    address = email if email is not None else f"{handle}@example.com"

    response = start(client, address)
    if "Check your email" not in response.text:
        return response

    response = client.post("/start/verify", data={"code": code, "next": "/"})
    if "Finish setting up" not in response.text:
        return response

    payload = {
        "password": password,
        "password_confirm": password,
        "leetcode_username": handle,
        "display_name": "",
        "next": "/",
    }
    payload.update(overrides)
    return client.post("/start/complete", data=payload)


def sign_in(client: TestClient, handle: str, password: str = PASSWORD) -> None:
    """Sign in, registering the account first if it does not exist yet."""
    from app import store

    address = f"{handle}@example.com"
    if store.get_user_by_email(address) is None:
        response = register(client, handle, password)
        assert response.status_code == 200, response.text
        return

    response = start(client, address)
    assert "Welcome back" in response.text, response.text
    response = client.post(
        "/start/password", data={"email": address, "password": password, "next": "/"}
    )
    assert response.status_code == 200
    assert "Welcome back" in response.text, response.text


def backdate_group(group_id: int, days: int) -> None:
    """Move a group's creation date into the past."""
    from datetime import datetime, timedelta, timezone

    from app import db

    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute("UPDATE groups SET created_at = %s WHERE id = %s", (stamp, group_id))


def sign_in_as(client: TestClient, user_id: int) -> None:
    """Plant a session cookie directly, for accounts made without the password flow."""
    from app import auth, config

    settings = config.get_settings()
    token = auth._serializer(settings, "session").dumps({"uid": user_id})
    client.cookies.set(auth.SESSION_COOKIE, token)


def link_leetcode(handle: str, username: str | None = None) -> int:
    """Attach a LeetCode account to a user, the way /settings would."""
    from app import store

    user = store.get_user_by_handle(handle)
    store.update_profile(
        user["id"],
        display_name=user["display_name"],
        leetcode_username=username if username is not None else handle,
        github_login="",
        github_repo="",
        timezone_name="UTC",
    )
    return user["id"]


def unlink_leetcode(handle: str) -> int:
    """Leave someone in the group with no connected account at all."""
    return link_leetcode(handle, username="")


def seed_activity(handle: str, days_back: int, source: str = "leetcode") -> int:
    """Give a user a solve on each of the last `days_back` days."""
    from app import store

    user_id = store.get_user_by_handle(handle)["id"]
    today = datetime.now(timezone.utc).date()
    counts = {today - timedelta(days=offset): 1 for offset in range(days_back)}
    store.replace_activity(user_id, source, counts)
    return user_id


class TestLanding:
    def test_landing_page_is_public(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Keep the streak" in response.text

    def test_old_entry_points_lead_to_the_one_page(self, client):
        for path in ("/login", "/register"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/start")

    def test_protected_pages_redirect_to_start(self, client):
        response = client.get("/settings", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/start?next=/settings"


class TestTheOneBox:
    """POST /start decides whether this is a sign-in or a sign-up."""

    def test_an_unknown_address_starts_a_signup(self, client):
        response = start(client, "nobody@example.com")
        assert "Check your email" in response.text
        assert len(client.sent) == 1
        assert client.sent[0]["to"] == "nobody@example.com"
        assert CODE in client.sent[0]["body"]

    def test_a_known_address_asks_for_the_password_instead(self, client):
        register(client, "dana")
        client.sent.clear()

        response = start(client, "dana@example.com")
        assert "Welcome back" in response.text
        assert 'name="password"' in response.text
        assert client.sent == [], "signing in must not send a code"

    def test_the_address_is_matched_case_insensitively(self, client):
        register(client, "dana")
        response = start(client, "  DANA@Example.COM  ")
        assert "Welcome back" in response.text

    def test_a_bad_address_is_rejected_before_any_email_goes_out(self, client):
        response = start(client, "not-an-email")
        assert response.status_code == 400
        assert "does not look like an email" in response.text
        assert client.sent == []

    def test_an_account_with_no_password_is_pointed_at_its_provider(self, client):
        from app import store

        store.upsert_oauth_user(
            provider="google",
            subject="google-sub-1",
            handle="gjones",
            display_name="G Jones",
            avatar_url="",
            email="gjones@example.com",
        )
        response = start(client, "gjones@example.com")
        assert "Use your provider" in response.text
        assert "Google" in response.text

    def test_the_lookup_is_throttled(self, client, monkeypatch):
        from app import ratelimit

        monkeypatch.setattr(ratelimit, "LOOKUP_ATTEMPTS", 3)
        for _ in range(3):
            start(client, "someone@example.com")
        response = start(client, "someone@example.com")
        assert response.status_code == 429
        assert "Too many attempts" in response.text

    def test_registration_can_be_closed(self, client, monkeypatch):
        from app import config

        monkeypatch.setenv("ALLOW_REGISTRATION", "false")
        config.get_settings.cache_clear()

        response = start(client, "newcomer@example.com")
        assert response.status_code == 403
        assert "not accepting new accounts" in response.text
        assert client.sent == []

    def test_a_closed_server_still_lets_existing_people_in(self, client, monkeypatch):
        from app import config

        register(client, "dana")
        monkeypatch.setenv("ALLOW_REGISTRATION", "false")
        config.get_settings.cache_clear()

        assert "Welcome back" in start(client, "dana@example.com").text


class TestEmailVerification:
    def test_the_right_code_moves_you_on(self, client):
        start(client, "dana@example.com")
        response = client.post("/start/verify", data={"code": CODE, "next": "/"})
        assert "Finish setting up" in response.text
        assert "verified" in response.text

    def test_a_wrong_code_is_refused_and_counts_down(self, client):
        start(client, "dana@example.com")
        response = client.post("/start/verify", data={"code": "000000", "next": "/"})
        assert response.status_code == 400
        assert "not right" in response.text
        assert "4 attempt(s) left" in response.text

    def test_codes_are_spaced_and_dashed_forgivingly(self, client):
        start(client, "dana@example.com")
        response = client.post("/start/verify", data={"code": " 123-456 ", "next": "/"})
        assert "Finish setting up" in response.text

    def test_a_code_cannot_be_used_twice(self, client):
        start(client, "dana@example.com")
        client.post("/start/verify", data={"code": CODE, "next": "/"})
        response = client.post("/start/verify", data={"code": CODE, "next": "/"})
        assert response.status_code == 400
        assert "expired" in response.text

    def test_too_many_wrong_codes_burns_the_code(self, client):
        start(client, "dana@example.com")
        for _ in range(5):
            client.post("/start/verify", data={"code": "000000", "next": "/"})
        response = client.post("/start/verify", data={"code": CODE, "next": "/"})
        assert "Ask for a new one" in response.text

    def test_verification_cannot_be_skipped(self, client):
        """Posting straight to the final step without a verified cookie must fail."""
        start(client, "dana@example.com")
        response = client.post(
            "/start/complete",
            data={
                "password": PASSWORD,
                "password_confirm": PASSWORD,
                "leetcode_username": "dana",
                "next": "/",
            },
        )
        assert "verify your email" in response.text
        from app import store

        assert store.get_user_by_email("dana@example.com") is None

    def test_a_resend_is_rate_limited(self, client):
        start(client, "dana@example.com")
        response = client.post("/start/resend", data={"next": "/"})
        assert response.status_code == 429
        assert "Hold on" in response.text

    def test_the_code_is_not_stored_in_the_clear(self, client):
        start(client, "dana@example.com")
        from app import db

        with db.connection() as conn:
            row = conn.execute("SELECT code_hash FROM email_codes").fetchone()
        assert CODE not in row["code_hash"]
        assert len(row["code_hash"]) == 64


class TestRegistration:
    def test_a_full_signup_creates_the_account(self, client):
        response = register(client, "dana")
        assert response.status_code == 200
        from app import store

        user = store.get_user_by_email("dana@example.com")
        assert user is not None
        assert user["leetcode_username"] == "dana"
        assert user["password_hash"].startswith("scrypt$")
        assert store.identity_providers(user["id"]) == ["password"]

    def test_display_name_defaults_to_the_leetcode_username(self, client):
        register(client, "dana")
        from app import store

        assert store.get_user_by_email("dana@example.com")["display_name"] == "dana"

    def test_a_given_display_name_wins(self, client):
        register(client, "dana", display_name="Dana Whitfield")
        from app import store

        assert store.get_user_by_email("dana@example.com")["display_name"] == "Dana Whitfield"

    def test_the_password_is_never_stored_in_the_clear(self, client):
        register(client, "dana")
        from app import db

        with db.connection() as conn:
            row = conn.execute("SELECT password_hash FROM users").fetchone()
        assert PASSWORD not in row["password_hash"]

    def test_two_people_cannot_claim_the_same_leetcode_account(self, client):
        register(client, "dana")
        response = register(client, "sam", leetcode_username="dana")
        assert response.status_code == 400
        assert "already claimed" in response.text

    def test_a_nonexistent_leetcode_username_is_caught(self, client):
        response = register(client, "dana", leetcode_username="ghost-user")
        assert response.status_code == 400
        assert "No LeetCode user" in response.text
        from app import store

        assert store.get_user_by_email("dana@example.com") is None

    def test_leetcode_username_is_required(self, client):
        response = register(client, "dana", leetcode_username="")
        assert response.status_code == 400
        assert "LeetCode username" in response.text

    def test_short_passwords_are_rejected(self, client):
        response = register(client, "dana", password="short")
        assert response.status_code == 400
        assert "at least 10 characters" in response.text

    def test_mismatched_passwords_are_rejected(self, client):
        start(client, "dana@example.com")
        client.post("/start/verify", data={"code": CODE, "next": "/"})
        response = client.post(
            "/start/complete",
            data={
                "password": PASSWORD,
                "password_confirm": "something-else-entirely",
                "leetcode_username": "dana",
                "next": "/",
            },
        )
        assert "do not match" in response.text

    def test_the_form_keeps_your_answers_when_it_rejects_you(self, client):
        response = register(
            client, "dana", password="short", display_name="Dana Whitfield"
        )
        assert "dana@example.com" in response.text
        assert "Dana Whitfield" in response.text
        assert 'value="dana"' in response.text


class TestSigningIn:
    def test_the_right_password_signs_you_in(self, client):
        register(client, "dana")
        sign_in(client, "dana")
        assert "Your groups" in client.get("/").text

    def test_a_wrong_password_does_not(self, client):
        register(client, "dana")
        client.cookies.clear()
        response = client.post(
            "/start/password",
            data={"email": "dana@example.com", "password": "wrong-password-x"},
        )
        assert "Email or password is incorrect" in response.text
        assert "Keep the streak" in client.get("/").text

    def test_an_unknown_email_gives_the_same_message_as_a_wrong_password(self, client):
        register(client, "dana")
        client.cookies.clear()
        unknown = client.post(
            "/start/password", data={"email": "nobody@example.com", "password": PASSWORD}
        )
        wrong = client.post(
            "/start/password",
            data={"email": "dana@example.com", "password": "wrong-password-x"},
        )
        assert "Email or password is incorrect" in unknown.text
        assert "Email or password is incorrect" in wrong.text

    def test_repeated_failures_are_throttled(self, client):
        register(client, "dana")
        client.cookies.clear()
        for _ in range(8):
            client.post(
                "/start/password",
                data={"email": "dana@example.com", "password": "wrong-password-x"},
            )
        blocked = client.post(
            "/start/password", data={"email": "dana@example.com", "password": PASSWORD}
        )
        assert "Too many failed attempts" in blocked.text

    def test_the_throttle_is_cleared_by_a_successful_sign_in(self, client):
        from app import ratelimit

        register(client, "dana")
        client.cookies.clear()
        for _ in range(3):
            client.post(
                "/start/password",
                data={"email": "dana@example.com", "password": "wrong-password-x"},
            )
        sign_in(client, "dana")
        assert not ratelimit.is_blocked("login:dana@example.com")

    def test_sign_out_clears_the_session(self, client):
        sign_in(client, "dana")
        client.post("/logout")
        assert "Keep the streak" in client.get("/").text


class TestAccountLinking:
    """Signing in with Google must join an existing account, not duplicate it."""

    def test_google_links_to_the_account_with_the_same_address(self, client):
        from app import db, store

        register(client, "dana")
        original = store.get_user_by_email("dana@example.com")

        linked = store.upsert_oauth_user(
            provider="google",
            subject="google-sub-1",
            handle="dana",
            display_name="Dana From Google",
            avatar_url="https://example.com/a.png",
            email="dana@example.com",
        )

        assert linked["id"] == original["id"]
        with db.connection() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1
        assert sorted(store.identity_providers(original["id"])) == ["google", "password"]

    def test_linking_does_not_overwrite_a_chosen_display_name(self, client):
        from app import store

        register(client, "dana", display_name="Dana Whitfield")
        store.upsert_oauth_user(
            provider="google",
            subject="google-sub-1",
            handle="dana",
            display_name="Dana From Google",
            avatar_url="https://example.com/a.png",
            email="dana@example.com",
        )
        user = store.get_user_by_email("dana@example.com")
        assert user["display_name"] == "Dana Whitfield"
        # ...but it does fill in what was empty.
        assert user["avatar_url"] == "https://example.com/a.png"

    def test_the_same_google_subject_returns_the_same_account(self, client):
        from app import db, store

        for _ in range(2):
            store.upsert_oauth_user(
                provider="google",
                subject="google-sub-1",
                handle="gjones",
                display_name="G Jones",
                avatar_url="",
                email="gjones@example.com",
            )
        with db.connection() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1

    def test_a_network_failure_is_a_sign_in_error_not_a_500(self, client, monkeypatch):
        """A dropped connection to Google must not surface as Internal Server Error."""
        import httpx

        from app import auth

        async def boom(code, settings):
            raise httpx.ConnectTimeout("timed out")

        monkeypatch.setattr(auth, "_exchange_google", boom)
        with pytest.raises(auth.AuthError, match="Could not reach Google"):
            import asyncio

            asyncio.run(auth.exchange_google_code("abc"))

    def test_unreadable_json_is_a_sign_in_error(self, client, monkeypatch):
        from app import auth

        async def garbage(code, settings):
            raise ValueError("not json")

        monkeypatch.setattr(auth, "_exchange_google", garbage)
        with pytest.raises(auth.AuthError, match="unreadable"):
            import asyncio

            asyncio.run(auth.exchange_google_code("abc"))

    def test_google_sign_in_is_refused_when_not_configured(self, client):
        response = client.get("/auth/google", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/start"


class TestPersonalHome:
    def test_home_shows_your_own_streak(self, client):
        register(client, "dana")
        seed_activity("dana", days_back=6)

        response = client.get("/")
        assert response.status_code == 200
        assert "6" in response.text
        assert "day streak" in response.text
        assert response.text.count('class="heatmap"') == 1

    def test_home_tells_you_when_today_is_still_open(self, client):
        from datetime import datetime, timedelta, timezone

        from app import store

        register(client, "dana")
        user = store.get_user_by_email("dana@example.com")
        today = datetime.now(timezone.utc).date()
        store.replace_activity(
            user["id"], "leetcode", {today - timedelta(days=n): 1 for n in (1, 2, 3)}
        )

        response = client.get("/")
        assert "breaks at midnight" in response.text

    def test_home_lists_your_groups(self, client):
        register(client, "dana")
        client.post("/groups", data={"name": "Daily grind"})
        assert "Daily grind" in client.get("/").text

    def test_the_nav_points_the_username_at_settings(self, client):
        register(client, "dana")
        response = client.get("/")
        assert '<a class="whoami" href="/settings"' in response.text
        assert '<a class="navlink" href="/">Home</a>' in response.text

    def test_sign_out_lives_on_the_settings_page(self, client):
        register(client, "dana")
        response = client.get("/settings")
        assert 'action="/logout"' in response.text
        assert "Sign-in methods" in response.text


class TestOnboarding:
    def _google_user(self, client):
        """A Google sign-in creates an account with no LeetCode username yet."""
        from app import store

        user = store.upsert_oauth_user(
            provider="google", subject="g-1", handle="newcomer",
            display_name="New Comer", avatar_url="", email="newcomer@example.com",
        )
        sign_in_as(client, user["id"])
        return user

    def test_a_fresh_account_is_asked_for_its_leetcode_username(self, client):
        self._google_user(client)
        response = client.get("/welcome")
        assert response.status_code == 200
        assert "What&rsquo;s your LeetCode username?" in response.text or \
               "LeetCode username" in response.text

    def test_home_sends_an_unlinked_account_to_welcome(self, client):
        self._google_user(client)
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/welcome"

    def test_welcome_asks_for_nothing_else(self, client):
        """The whole point is that it is one question, not the settings form."""
        self._google_user(client)
        body = client.get("/welcome").text
        for field in ("github_login", "github_repo", "timezone", "display_name"):
            assert f'name="{field}"' not in body

    def test_saving_it_links_the_account_and_moves_on(self, client):
        from app import store

        user = self._google_user(client)
        response = client.post(
            "/welcome", data={"leetcode_username": "newcomer"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert store.get_user(user["id"])["leetcode_username"] == "newcomer"

    def test_the_board_is_populated_before_you_land_on_it(self, client, monkeypatch):
        """Onboarding must not redirect to an empty grid that needs a refresh."""
        from app import main, store

        synced: list[str] = []

        async def recording_sync(users, settings, concurrency=4):
            for u in users:
                synced.append(u["handle"])
                store.replace_activity(u["id"], "leetcode", {date.today(): 3})
            return {}

        monkeypatch.setattr(main.sync, "sync_users", recording_sync)

        user = store.upsert_oauth_user(
            provider="google", subject="g-sync", handle="newcomer",
            display_name="New Comer", avatar_url="", email="newcomer@example.com",
        )
        sign_in_as(client, user["id"])
        response = client.post(
            "/welcome", data={"leetcode_username": "newcomer"}, follow_redirects=True
        )

        assert synced == ["newcomer"], "the first sync must be awaited, not backgrounded"
        # The board that renders on arrival already has the day on it.
        assert "day streak" in response.text
        assert store.activity_for_users([user["id"]])[user["id"]]["leetcode"]

    def test_a_slow_first_sync_does_not_hang_the_request(self, client, monkeypatch):
        """If LeetCode stalls we give up waiting and finish in the background."""
        import asyncio as _asyncio

        from app import main, store

        backgrounded: list[int] = []

        async def never_returns(users, settings, concurrency=4):
            await _asyncio.sleep(60)

        monkeypatch.setattr(main.sync, "sync_users", never_returns)
        monkeypatch.setattr(main.sync, "schedule",
                            lambda users, settings: backgrounded.append(users[0]["id"]))
        monkeypatch.setattr(main, "FIRST_SYNC_WAIT_SECONDS", 0.05)

        user = store.upsert_oauth_user(
            provider="google", subject="g-slow", handle="slowpoke",
            display_name="Slow Poke", avatar_url="", email="slow@example.com",
        )
        sign_in_as(client, user["id"])
        response = client.post(
            "/welcome", data={"leetcode_username": "slowpoke"}, follow_redirects=False
        )

        assert response.status_code == 303
        assert backgrounded == [user["id"]]

    def test_a_bad_username_is_caught_here_too(self, client):
        self._google_user(client)
        response = client.post("/welcome", data={"leetcode_username": "ghost1"})
        assert response.status_code == 400
        assert "No LeetCode user" in response.text

    def test_a_blank_username_is_refused(self, client):
        self._google_user(client)
        response = client.post("/welcome", data={"leetcode_username": "  "})
        assert response.status_code == 400

    def test_an_oauth_display_name_survives_onboarding(self, client):
        from app import store

        user = self._google_user(client)
        client.post("/welcome", data={"leetcode_username": "newcomer"})
        assert store.get_user(user["id"])["display_name"] == "New Comer"

    def test_a_linked_account_is_not_asked_again(self, client):
        sign_in(client, "dana")
        response = client.get("/welcome", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


class TestInviteThenSignUp:
    """Opening an invite link before you have an account must still land you there."""

    def _invite(self, client):
        from app import store

        sign_in(client, "dana")
        client.post("/groups", data={"name": "Daily grind"})
        group = store.groups_for_user(store.get_user_by_handle("dana")["id"])[0]
        client.cookies.clear()
        return group

    def test_an_anonymous_visitor_is_sent_to_sign_in_with_the_invite_kept(self, client):
        group = self._invite(client)
        response = client.get(f"/join/{group['invite_code']}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/start?next=/join/{group['invite_code']}"

    def test_email_signup_returns_to_the_invite(self, client):
        group = self._invite(client)
        target = f"/join/{group['invite_code']}"

        client.post("/start", data={"email": "newbie@example.com", "next": target})
        client.post("/start/verify", data={"code": CODE, "next": target})
        response = client.post(
            "/start/complete",
            data={"password": PASSWORD, "password_confirm": PASSWORD,
                  "leetcode_username": "newbie", "display_name": "", "next": target},
            follow_redirects=False,
        )
        assert response.headers["location"] == target

    def test_google_signup_keeps_the_invite_through_the_welcome_page(self, client):
        """The bug: /welcome dropped `next`, stranding people on their own board."""
        from app import store

        group = self._invite(client)
        target = f"/join/{group['invite_code']}"

        user = store.upsert_oauth_user(
            provider="google", subject="g-invited", handle="invited",
            display_name="Invited", avatar_url="", email="invited@example.com",
        )
        sign_in_as(client, user["id"])

        page = client.get(f"/welcome?next={target}")
        assert f'value="{target}"' in page.text, "the form must carry the destination"

        response = client.post(
            "/welcome",
            data={"leetcode_username": "invited", "next": target},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == target

    def test_welcome_defaults_home_without_an_invite(self, client):
        from app import store

        user = store.upsert_oauth_user(
            provider="google", subject="g-plain", handle="plain",
            display_name="Plain", avatar_url="", email="plain@example.com",
        )
        sign_in_as(client, user["id"])
        response = client.post(
            "/welcome", data={"leetcode_username": "plain"}, follow_redirects=False
        )
        assert response.headers["location"] == "/"

    def test_an_offsite_next_is_refused(self, client):
        """`next` is attacker-controllable via a crafted link."""
        from app import store

        user = store.upsert_oauth_user(
            provider="google", subject="g-eve", handle="eve",
            display_name="Eve", avatar_url="", email="eve@example.com",
        )
        sign_in_as(client, user["id"])
        response = client.post(
            "/welcome",
            data={"leetcode_username": "eve", "next": "https://evil.example.com/phish"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/"

    def test_a_linked_account_opening_welcome_still_honours_the_invite(self, client):
        group = self._invite(client)
        target = f"/join/{group['invite_code']}"
        sign_in(client, "sam")
        response = client.get(f"/welcome?next={target}", follow_redirects=False)
        assert response.headers["location"] == target


class TestSettingsSimplification:
    def test_the_github_fields_are_gone(self, client):
        sign_in(client, "dana")
        body = client.get("/settings").text
        assert 'name="github_login"' not in body
        assert 'name="github_repo"' not in body
        assert "solutions repo" not in body.lower()

    def test_timezone_is_a_real_dropdown(self, client):
        sign_in(client, "dana")
        body = client.get("/settings").text
        assert '<select id="timezone" name="timezone">' in body
        assert "<datalist" not in body
        assert "Asia/Taipei" in body

    def test_the_current_zone_is_preselected(self, client):
        sign_in(client, "dana")
        client.post(
            "/settings",
            data={"display_name": "", "leetcode_username": "dana", "timezone": "Asia/Taipei"},
        )
        body = client.get("/settings").text
        assert '<option value="Asia/Taipei" selected>' in body

    def test_an_unlisted_zone_still_appears(self, client):
        """A zone set before the short list existed must not vanish from the dropdown."""
        from app import store

        sign_in(client, "dana")
        user = store.get_user_by_email("dana@example.com")
        store.update_profile(
            user["id"], display_name="Dana", leetcode_username="dana",
            github_login="", github_repo="", timezone_name="Pacific/Chatham",
        )
        body = client.get("/settings").text
        assert '<option value="Pacific/Chatham" selected>' in body

    def test_a_bogus_zone_is_still_refused(self, client):
        sign_in(client, "dana")
        response = client.post(
            "/settings",
            data={"display_name": "", "leetcode_username": "dana", "timezone": "Mars/Olympus"},
            follow_redirects=True,
        )
        assert "Unknown timezone" in response.text

    def test_saving_settings_keeps_stored_github_values(self, client):
        """The fields are hidden, not wiped - re-enabling the source must not cost data."""
        from app import store

        sign_in(client, "dana")
        user = store.get_user_by_email("dana@example.com")
        store.update_profile(
            user["id"], display_name="Dana", leetcode_username="dana",
            github_login="octocat", github_repo="octocat/leetcode", timezone_name="UTC",
        )
        client.post(
            "/settings",
            data={"display_name": "Dana", "leetcode_username": "dana", "timezone": "UTC"},
        )
        after = store.get_user(user["id"])
        assert after["github_login"] == "octocat"
        assert after["github_repo"] == "octocat/leetcode"

    def test_a_blank_username_cannot_unlink_an_account(self, client):
        """A truncated POST must not silently drop someone off their group board."""
        from app import store

        sign_in(client, "dana")
        user = store.get_user_by_email("dana@example.com")
        assert user["leetcode_username"] == "dana"

        response = client.post(
            "/settings",
            data={"display_name": "Dana", "timezone": "UTC"},
            follow_redirects=True,
        )
        assert "cannot be blank" in response.text
        assert store.get_user(user["id"])["leetcode_username"] == "dana"

    def test_an_all_whitespace_username_is_refused_too(self, client):
        from app import store

        sign_in(client, "dana")
        user = store.get_user_by_email("dana@example.com")
        client.post(
            "/settings",
            data={"display_name": "Dana", "leetcode_username": "   ", "timezone": "UTC"},
        )
        assert store.get_user(user["id"])["leetcode_username"] == "dana"

    def test_an_unlinked_member_is_still_counted(self, client):
        """Sanity-check the denominator that the wipe corrupted."""
        from app import sync

        assert sync.is_linked({"leetcode_username": "dana"}) is True
        assert sync.is_linked({"leetcode_username": ""}) is False

    def test_the_github_source_is_parked(self, client):
        from app import sync

        assert sync.GITHUB_SOURCE_ENABLED is False
        assert sync.is_linked({"leetcode_username": "dana"}) is True
        assert sync.is_linked({"github_login": "octocat", "leetcode_username": ""}) is False


class TestGroups:
    def test_create_group_then_see_it_on_the_board(self, client):
        sign_in(client, "dana")
        response = client.post("/groups", data={"name": "Daily grind"})
        assert response.status_code == 200
        assert "Daily grind" in response.text
        assert "Invite" in response.text

    def test_non_members_cannot_see_a_board(self, client):
        sign_in(client, "dana")
        client.post("/groups", data={"name": "Private crew"})
        from app import store

        group = store.groups_for_user(store.get_user_by_handle("dana")["id"])[0]

        sign_in(client, "mallory")
        response = client.get(f"/g/{group['id']}")
        assert response.status_code == 404
        assert "Private crew" not in response.text

    def test_invite_link_lets_a_friend_join(self, client):
        sign_in(client, "dana")
        client.post("/groups", data={"name": "Daily grind"})
        from app import store

        group = store.groups_for_user(store.get_user_by_handle("dana")["id"])[0]
        code = group["invite_code"]

        sign_in(client, "sam")
        preview = client.get(f"/join/{code}")
        assert "Join" in preview.text and "Daily grind" in preview.text

        client.post(f"/join/{code}")
        board = client.get(f"/g/{group['id']}")
        assert board.status_code == 200
        assert "sam" in board.text and "dana" in board.text

    def test_rotating_the_invite_kills_the_old_link(self, client):
        sign_in(client, "dana")
        client.post("/groups", data={"name": "Daily grind"})
        from app import store

        user_id = store.get_user_by_handle("dana")["id"]
        old_code = store.groups_for_user(user_id)[0]["invite_code"]
        group_id = store.groups_for_user(user_id)[0]["id"]

        client.post(f"/g/{group_id}/invite/rotate")

        sign_in(client, "sam")
        response = client.get(f"/join/{old_code}")
        assert response.status_code == 404

    def test_only_the_owner_can_rename(self, client):
        sign_in(client, "dana")
        client.post("/groups", data={"name": "Daily grind"})
        from app import store

        group = store.groups_for_user(store.get_user_by_handle("dana")["id"])[0]
        client.post(f"/join/{group['invite_code']}")

        sign_in(client, "sam")
        client.post(f"/join/{group['invite_code']}")
        client.post(f"/g/{group['id']}/rename", data={"name": "Hijacked"})

        assert store.get_group(group["id"])["name"] == "Daily grind"

    def test_owner_cannot_leave_but_can_delete(self, client):
        sign_in(client, "dana")
        client.post("/groups", data={"name": "Daily grind"})
        from app import store

        group = store.groups_for_user(store.get_user_by_handle("dana")["id"])[0]

        leave = client.post(f"/g/{group['id']}/leave")
        assert "You own this group" in leave.text
        assert store.get_group(group["id"]) is not None

        client.post(f"/g/{group['id']}/delete")
        assert store.get_group(group["id"]) is None


class TestBoard:
    def _group_with_two_members(self, client, started_days_ago: int = 800):
        """Backdated by default, so range tests are not clamped by the group's age."""
        sign_in(client, "dana")
        client.post("/groups", data={"name": "Daily grind"})
        from app import store

        group = store.groups_for_user(store.get_user_by_handle("dana")["id"])[0]
        sign_in(client, "sam")
        client.post(f"/join/{group['invite_code']}")
        backdate_group(group["id"], started_days_ago)
        return store.get_group(group["id"])

    def test_streaks_render_side_by_side(self, client):
        group = self._group_with_two_members(client)
        seed_activity("dana", days_back=5)
        seed_activity("sam", days_back=2)

        sign_in(client, "sam")
        response = client.get(f"/g/{group['id']}")
        assert response.status_code == 200
        # Both members' grids are present.
        assert response.text.count('class="heatmap"') == 2
        assert "2 of 2 solved today" in response.text

    def test_json_api_reports_streak_numbers(self, client):
        group = self._group_with_two_members(client)
        seed_activity("dana", days_back=5)

        sign_in(client, "dana")
        payload = client.get(f"/api/g/{group['id']}.json").json()
        dana = next(m for m in payload["members"] if m["handle"] == "dana")

        assert dana["current_streak"] == 5
        assert dana["done_today"] is True
        assert dana["sources"] == ["leetcode"]

    def test_leaderboard_puts_the_longest_streak_first(self, client):
        group = self._group_with_two_members(client)
        seed_activity("dana", days_back=2)
        seed_activity("sam", days_back=9)

        sign_in(client, "dana")
        payload = client.get(f"/api/g/{group['id']}.json").json()
        assert [m["handle"] for m in payload["members"]] == ["sam", "dana"]

    def test_a_new_group_only_shows_itself(self, client):
        """A group made today has no history to show beyond this week."""
        group = self._group_with_two_members(client, started_days_ago=0)
        response = client.get(f"/g/{group['id']}?range=1y")
        assert "--weeks: 1" in response.text

    def test_the_window_grows_with_the_group(self, client):
        group = self._group_with_two_members(client, started_days_ago=20)
        response = client.get(f"/g/{group['id']}?range=1y")
        # Three or four columns depending on which weekday the group was created on.
        assert ("--weeks: 3" in response.text) or ("--weeks: 4" in response.text)

    def test_a_short_range_still_wins_over_an_old_group(self, client):
        group = self._group_with_two_members(client, started_days_ago=800)
        assert "--weeks: 2" in client.get(f"/g/{group['id']}?range=2w").text

    def test_the_board_says_when_the_group_started(self, client):
        group = self._group_with_two_members(client, started_days_ago=30)
        assert "since" in client.get(f"/g/{group['id']}").text

    def test_your_own_history_is_not_clamped(self, client):
        """Home is your account, not a group, so it keeps the full year."""
        self._group_with_two_members(client, started_days_ago=0)
        sign_in(client, "dana")
        assert "--weeks: 53" in client.get("/?range=1y").text

    def test_totals_are_scoped_to_the_window(self, client):
        """"248 active days" must not sit beside a five-square grid."""
        group = self._group_with_two_members(client, started_days_ago=800)
        seed_activity("dana", days_back=40)
        sign_in(client, "dana")

        def active_days(range_key):
            payload = client.get(f"/api/g/{group['id']}.json?range={range_key}").json()
            return {m["handle"]: m["active_days"] for m in payload["members"]}

        wide = active_days("1y")
        narrow = active_days("1w")
        assert narrow["dana"] < wide["dana"]
        assert narrow["dana"] <= 7

    def test_a_streak_is_not_clipped_by_the_window(self, client):
        """Totals are windowed; a streak is a real fact about the person."""
        from datetime import date

        from app.streaks import compute_stats

        from datetime import timedelta

        today = date(2026, 9, 3)
        days = {today - timedelta(days=i): 1 for i in range(15)}
        scoped = compute_stats(days, today, since=date(2026, 9, 1))
        assert scoped.current_streak == 15  # measured over everything
        assert scoped.active_days == 3  # Sep 1, 2 and 3 only

    def test_a_new_group_offers_only_the_ranges_it_can_fill(self, client):
        group = self._group_with_two_members(client, started_days_ago=0)
        body = client.get(f"/g/{group['id']}").text
        assert "?range=1w" in body
        assert "?range=1y" not in body
        assert "?range=6m" not in body

    def test_an_old_group_offers_everything(self, client):
        group = self._group_with_two_members(client, started_days_ago=800)
        body = client.get(f"/g/{group['id']}").text
        for key in ("1w", "2w", "1m", "3m", "6m", "1y"):
            assert f"?range={key}" in body

    def test_range_selector_changes_the_grid_width(self, client):
        group = self._group_with_two_members(client)
        short = client.get(f"/g/{group['id']}?range=3m")
        wide = client.get(f"/g/{group['id']}?range=1y")

        assert "--weeks: 13" in short.text
        assert "--weeks: 53" in wide.text

    @pytest.mark.parametrize(
        ("key", "weeks"),
        [("1w", 1), ("2w", 2), ("1m", 5), ("3m", 13), ("6m", 26), ("1y", 53)],
    )
    def test_every_range_renders_its_width(self, client, key, weeks):
        group = self._group_with_two_members(client)
        response = client.get(f"/g/{group['id']}?range={key}")
        assert response.status_code == 200
        assert f"--weeks: {weeks}" in response.text

    def test_a_week_is_the_shortest_window(self, client):
        group = self._group_with_two_members(client)
        response = client.get(f"/g/{group['id']}?range=1w")
        assert "--weeks: 1" in response.text
        # Seven days, one per member, and nothing beyond the current week.
        assert response.text.count('class="hm-col"') == 2

    @pytest.mark.parametrize(("old_key", "weeks"), [("12w", 13), ("26w", 26)])
    def test_old_range_links_still_work(self, client, old_key, weeks):
        """Bookmarks and shared links from before the labels changed."""
        group = self._group_with_two_members(client)
        response = client.get(f"/g/{group['id']}?range={old_key}")
        assert response.status_code == 200
        assert f"--weeks: {weeks}" in response.text

    def test_short_ranges_render_larger_squares(self, client):
        group = self._group_with_two_members(client)
        assert "heatmap narrow" in client.get(f"/g/{group['id']}?range=1w").text
        assert "heatmap narrow" in client.get(f"/g/{group['id']}?range=1m").text
        assert "heatmap narrow" not in client.get(f"/g/{group['id']}?range=3m").text

    def test_a_short_range_still_says_which_month_it_is(self, client):
        """A wide grid skips the first column's label; a one-week grid must not."""
        from datetime import date

        from app.streaks import build_calendar

        cal = build_calendar({}, date(2026, 9, 3), weeks=1)
        assert [label.text for label in cal.month_labels] == ["Aug"]

    def test_unknown_range_falls_back_to_the_default(self, client):
        group = self._group_with_two_members(client)
        response = client.get(f"/g/{group['id']}?range=nonsense")
        assert "--weeks: 26" in response.text  # the 6m default

    def test_members_with_no_linked_account_still_appear(self, client):
        group = self._group_with_two_members(client)
        seed_activity("dana", days_back=3)
        unlink_leetcode("sam")

        sign_in(client, "dana")
        response = client.get(f"/g/{group['id']}")
        # sam has nothing connected: shown, but not counted as owing a solve.
        assert "no account linked" in response.text
        assert "1 of 1 solved today" in response.text

    def test_a_linked_member_with_no_solves_yet_still_owes_today(self, client):
        """The state every new joiner is in: linked, but nothing solved."""
        group = self._group_with_two_members(client)
        seed_activity("dana", days_back=3)

        sign_in(client, "dana")
        response = client.get(f"/g/{group['id']}")
        assert "1 of 2 solved today" in response.text

        payload = client.get(f"/api/g/{group['id']}.json").json()
        sam = next(member for member in payload["members"] if member["handle"] == "sam")
        assert sam["current_streak"] == 0
        assert sam["done_today"] is False


class TestPasswordReset:
    def _request_reset(self, client, address="dana@example.com"):
        register(client, "dana")
        client.cookies.clear()
        start(client, address)
        return client.post("/start/forgot", data={"email": address, "next": "/"})

    def test_a_reset_code_is_emailed(self, client):
        response = self._request_reset(client)
        assert "Check your email" in response.text
        assert client.sent[-1]["to"] == "dana@example.com"

    def test_the_code_lets_you_set_a_new_password(self, client):
        self._request_reset(client)
        response = client.post("/start/verify", data={"code": CODE, "next": "/"})
        assert "Choose a new password" in response.text

        response = client.post(
            "/start/reset",
            data={"password": "brand-new-password", "password_confirm": "brand-new-password",
                  "next": "/"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Password changed" in response.text

    def test_the_old_password_stops_working_after_a_reset(self, client):
        self._request_reset(client)
        client.post("/start/verify", data={"code": CODE, "next": "/"})
        client.post(
            "/start/reset",
            data={"password": "brand-new-password", "password_confirm": "brand-new-password",
                  "next": "/"},
        )
        client.cookies.clear()

        start(client, "dana@example.com")
        stale = client.post(
            "/start/password", data={"email": "dana@example.com", "password": PASSWORD, "next": "/"}
        )
        assert stale.status_code == 400

        fresh = client.post(
            "/start/password",
            data={"email": "dana@example.com", "password": "brand-new-password", "next": "/"},
            follow_redirects=True,
        )
        assert "Welcome back" in fresh.text or fresh.status_code == 200
        assert client.cookies.get("sb_session")

    def test_a_reset_cannot_be_completed_without_the_code(self, client):
        self._request_reset(client)
        response = client.post(
            "/start/reset",
            data={"password": "brand-new-password", "password_confirm": "brand-new-password",
                  "next": "/"},
            follow_redirects=True,
        )
        assert "verify your email" in response.text

        from app import passwords, store

        user = store.get_user_by_email("dana@example.com")
        assert passwords.verify_password(PASSWORD, user["password_hash"])

    def test_a_signup_code_cannot_be_spent_on_a_reset(self, client):
        """The purpose is bound into the code hash, so codes are not interchangeable."""
        from app import verification

        register(client, "dana")
        client.cookies.clear()
        start(client, "dana@example.com")
        client.post("/start/forgot", data={"email": "dana@example.com", "next": "/"})

        with_wrong_purpose = verification.hash_code(
            "test-secret-key", "dana@example.com", verification.PURPOSE_REGISTER, CODE
        )
        with_right_purpose = verification.hash_code(
            "test-secret-key", "dana@example.com", verification.PURPOSE_RESET, CODE
        )
        assert with_wrong_purpose != with_right_purpose

    def test_a_weak_new_password_is_refused(self, client):
        self._request_reset(client)
        client.post("/start/verify", data={"code": CODE, "next": "/"})
        response = client.post(
            "/start/reset", data={"password": "short", "password_confirm": "short", "next": "/"}
        )
        assert response.status_code == 400
        assert "at least 10 characters" in response.text.lower()

    def test_mismatched_new_passwords_are_refused(self, client):
        self._request_reset(client)
        client.post("/start/verify", data={"code": CODE, "next": "/"})
        response = client.post(
            "/start/reset",
            data={"password": "brand-new-password", "password_confirm": "something-else",
                  "next": "/"},
        )
        assert response.status_code == 400
        assert "do not match" in response.text

    def test_reset_is_offered_on_the_password_step(self, client):
        register(client, "dana")
        client.cookies.clear()
        response = start(client, "dana@example.com")
        assert "/start/forgot" in response.text


class TestPasswordChange:
    def test_changing_the_password_takes_effect(self, client):
        register(client, "dana")
        response = client.post(
            "/settings/password",
            data={
                "current_password": PASSWORD,
                "new_password": "a-brand-new-password",
                "new_password_confirm": "a-brand-new-password",
            },
        )
        assert "Password changed" in response.text

        sign_in(client, "dana", password="a-brand-new-password")

    def test_the_old_password_stops_working(self, client):
        register(client, "dana")
        client.post(
            "/settings/password",
            data={
                "current_password": PASSWORD,
                "new_password": "a-brand-new-password",
                "new_password_confirm": "a-brand-new-password",
            },
        )
        client.cookies.clear()
        response = client.post(
            "/start/password", data={"email": "dana@example.com", "password": PASSWORD}
        )
        assert "Email or password is incorrect" in response.text

    def test_the_current_password_is_required(self, client):
        register(client, "dana")
        response = client.post(
            "/settings/password",
            data={
                "current_password": "not-my-password",
                "new_password": "a-brand-new-password",
                "new_password_confirm": "a-brand-new-password",
            },
        )
        assert "current password is not correct" in response.text
        sign_in(client, "dana")  # unchanged

    def test_a_weak_new_password_is_refused(self, client):
        register(client, "dana")
        response = client.post(
            "/settings/password",
            data={
                "current_password": PASSWORD,
                "new_password": "short",
                "new_password_confirm": "short",
            },
        )
        assert "at least 10 characters" in response.text


class TestProductionGuards:
    """A misconfigured public deploy must fail at boot, not quietly serve a broken app."""

    def _boot(self, tmp_path, monkeypatch, **env):
        from starlette.testclient import TestClient

        from app import config, main

        from tests.conftest import TEST_DATABASE_URL

        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
        monkeypatch.setenv("SMTP_HOST", "")
        monkeypatch.setenv("RESEND_API_KEY", "")
        monkeypatch.setenv("ALLOW_REGISTRATION", "true")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("_LEETSTREAK_EPHEMERAL_SECRET", raising=False)
        config.get_settings.cache_clear()
        with TestClient(main.app) as client:
            return client.get("/healthz").status_code

    def test_https_without_a_secret_key_refuses_to_boot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "")
        with pytest.raises(RuntimeError, match="SECRET_KEY is unset"):
            self._boot(tmp_path, monkeypatch, BASE_URL="https://streaks.example.com",
                       SMTP_HOST="smtp.example.com")

    def test_https_with_open_registration_and_no_mail_refuses_to_boot(self, tmp_path, monkeypatch):
        with pytest.raises(RuntimeError, match="no mail provider"):
            self._boot(tmp_path, monkeypatch, BASE_URL="https://streaks.example.com",
                       SECRET_KEY="a" * 64)

    def test_https_with_registration_closed_is_allowed_without_mail(self, tmp_path, monkeypatch):
        code = self._boot(tmp_path, monkeypatch, BASE_URL="https://streaks.example.com",
                          SECRET_KEY="a" * 64, ALLOW_REGISTRATION="false")
        assert code == 200

    def test_a_properly_configured_deploy_boots(self, tmp_path, monkeypatch):
        code = self._boot(tmp_path, monkeypatch, BASE_URL="https://streaks.example.com",
                          SECRET_KEY="a" * 64, SMTP_HOST="smtp.example.com")
        assert code == 200

    def test_localhost_still_boots_with_nothing_configured(self, tmp_path, monkeypatch):
        """The whole point of the console fallback is a zero-setup laptop run."""
        monkeypatch.setenv("SECRET_KEY", "")
        code = self._boot(tmp_path, monkeypatch, BASE_URL="http://localhost:8000")
        assert code == 200


class TestCron:
    def test_cron_endpoint_is_off_without_a_token(self, client):
        response = client.post("/api/cron/sync")
        assert response.status_code == 404

    def test_cron_endpoint_rejects_a_wrong_token(self, client, monkeypatch):
        from app import config

        monkeypatch.setenv("CRON_TOKEN", "s3cret")
        config.get_settings.cache_clear()

        assert client.post("/api/cron/sync").status_code == 401
        assert (
            client.post(
                "/api/cron/sync", headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/cron/sync", headers={"Authorization": "Bearer s3cret"}
            ).status_code
            == 200
        )
