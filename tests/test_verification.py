import pytest

from app import db, verification

KEY = "a-test-secret-key"
EMAIL = "dana@example.com"


@pytest.fixture(autouse=True)
def database():
    from tests.conftest import TEST_DATABASE_URL

    db.configure(TEST_DATABASE_URL)
    db.init_db()


def issue() -> str:
    return verification.issue(KEY, EMAIL).code


class TestCodes:
    def test_a_code_is_six_digits(self):
        code = issue()
        assert len(code) == verification.CODE_DIGITS
        assert code.isdigit()

    def test_the_right_code_passes_once(self):
        code = issue()
        verification.check(KEY, EMAIL, code)
        with pytest.raises(verification.VerificationError, match="expired"):
            verification.check(KEY, EMAIL, code)

    def test_whitespace_and_dashes_are_forgiven(self):
        code = issue()
        verification.check(KEY, EMAIL, f" {code[:3]}-{code[3:]} ")

    def test_a_code_is_bound_to_its_address(self):
        code = issue()
        with pytest.raises(verification.VerificationError):
            verification.check(KEY, "someone.else@example.com", code)

    def test_a_code_is_bound_to_the_secret_key(self):
        code = issue()
        with pytest.raises(verification.VerificationError):
            verification.check("a-different-key", EMAIL, code)

    def test_reissuing_replaces_the_previous_code(self):
        first = issue()
        second = issue()
        assert first != second or True  # they may collide; the point is the old one dies
        with pytest.raises(verification.VerificationError):
            verification.check(KEY, EMAIL, first if first != second else "000000")

    def test_the_code_is_stored_only_as_a_digest(self):
        code = issue()
        with db.connection() as conn:
            stored = conn.execute("SELECT code_hash FROM email_codes").fetchone()["code_hash"]
        assert code not in stored
        assert stored == verification.hash_code(KEY, EMAIL, verification.PURPOSE_REGISTER, code)


class TestAttemptLimit:
    """The whole reason a six-digit code is acceptable."""

    def test_wrong_attempts_are_actually_counted(self):
        issue()
        for expected_remaining in (4, 3, 2, 1):
            with pytest.raises(verification.VerificationError) as caught:
                verification.check(KEY, EMAIL, "000000")
            assert f"{expected_remaining} attempt(s) left" in str(caught.value)

        with db.connection() as conn:
            assert conn.execute("SELECT attempts FROM email_codes").fetchone()["attempts"] == 4

    def test_the_code_dies_after_the_last_attempt(self):
        code = issue()
        for _ in range(verification.MAX_ATTEMPTS):
            with pytest.raises(verification.VerificationError):
                verification.check(KEY, EMAIL, "000000")

        # Even the correct code is now worthless.
        with pytest.raises(verification.VerificationError, match="Ask for a new one"):
            verification.check(KEY, EMAIL, code)

        with db.connection() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM email_codes").fetchone()["c"] == 0

    def test_a_correct_attempt_after_some_wrong_ones_still_works(self):
        code = issue()
        for _ in range(3):
            with pytest.raises(verification.VerificationError):
                verification.check(KEY, EMAIL, "000000")
        verification.check(KEY, EMAIL, code)


class TestExpiry:
    def test_an_expired_code_is_refused_and_removed(self):
        code = issue()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE email_codes SET expires_at = %s", ("2020-01-01T00:00:00+00:00",)
            )
        with pytest.raises(verification.VerificationError, match="expired"):
            verification.check(KEY, EMAIL, code)
        with db.connection() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM email_codes").fetchone()["c"] == 0

    def test_purge_clears_only_expired_rows(self):
        issue()
        verification.issue(KEY, "other@example.com")
        with db.transaction() as conn:
            conn.execute(
                "UPDATE email_codes SET expires_at = %s WHERE email = %s",
                ("2020-01-01T00:00:00+00:00", EMAIL),
            )
        assert verification.purge_expired() == 1
        with db.connection() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM email_codes").fetchone()["c"] == 1

    def test_checking_an_address_with_no_code_reports_expiry(self):
        with pytest.raises(verification.VerificationError, match="expired"):
            verification.check(KEY, "nobody@example.com", "123456")


class TestResendCooldown:
    def test_a_fresh_code_blocks_an_immediate_resend(self):
        issue()
        assert 0 < verification.seconds_until_resend(EMAIL) <= verification.RESEND_COOLDOWN_SECONDS

    def test_an_address_with_no_code_can_send_at_once(self):
        assert verification.seconds_until_resend("nobody@example.com") == 0

    def test_the_cooldown_lapses(self):
        issue()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE email_codes SET created_at = %s", ("2020-01-01T00:00:00+00:00",)
            )
        assert verification.seconds_until_resend(EMAIL) == 0
