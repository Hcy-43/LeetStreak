import pytest

from app import passwords
from app.store import EMAIL_RE, normalise_email, problem_with_email


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    """scrypt is deliberately slow; these tests are about correctness, not cost."""
    monkeypatch.setattr(passwords, "SCRYPT_N", 2**8)


class TestHashing:
    def test_a_password_verifies_against_its_own_hash(self):
        encoded = passwords.hash_password("correct horse battery staple")
        assert passwords.verify_password("correct horse battery staple", encoded)

    def test_a_wrong_password_does_not_verify(self):
        encoded = passwords.hash_password("correct horse battery staple")
        assert not passwords.verify_password("Correct horse battery staple", encoded)
        assert not passwords.verify_password("", encoded)

    def test_the_same_password_hashes_differently_every_time(self):
        first = passwords.hash_password("same password")
        second = passwords.hash_password("same password")
        assert first != second, "salt is missing"
        assert passwords.verify_password("same password", first)
        assert passwords.verify_password("same password", second)

    def test_the_plaintext_never_appears_in_the_hash(self):
        encoded = passwords.hash_password("hunter2-hunter2")
        assert "hunter2" not in encoded

    def test_the_encoding_records_its_parameters(self):
        scheme, n, r, p, salt, digest = passwords.hash_password("whatever-long").split("$")
        assert scheme == "scrypt"
        assert (int(n), int(r), int(p)) == (passwords.SCRYPT_N, passwords.SCRYPT_R, passwords.SCRYPT_P)
        assert salt and digest

    def test_hashes_made_with_older_parameters_still_verify(self):
        """Raising the cost later must not lock existing users out."""
        encoded = passwords.hash_password("a-durable-password")
        passwords.SCRYPT_N = 2**9
        assert passwords.verify_password("a-durable-password", encoded)

    @pytest.mark.parametrize(
        "encoded",
        ["", "garbage", "scrypt$nope", "bcrypt$1$2$3$aaaa$bbbb", "scrypt$x$8$1$aaaa$bbbb"],
    )
    def test_malformed_hashes_return_false_rather_than_raising(self, encoded):
        assert passwords.verify_password("anything", encoded) is False

    def test_unicode_passwords_round_trip(self):
        encoded = passwords.hash_password("pässwörd-日本語-🔐")
        assert passwords.verify_password("pässwörd-日本語-🔐", encoded)

    def test_waste_time_is_harmless(self):
        passwords.waste_time()


class TestPolicy:
    def test_short_passwords_are_rejected(self):
        assert "10 characters" in passwords.problem_with("short")

    def test_whitespace_only_is_rejected(self):
        assert passwords.problem_with(" " * 20) is not None

    def test_an_absurdly_long_password_is_rejected(self):
        assert passwords.problem_with("x" * 5000) is not None

    def test_a_reasonable_password_passes(self):
        assert passwords.problem_with("correct horse battery staple") is None


class TestEmail:
    @pytest.mark.parametrize(
        "raw", ["  Dana@Example.COM ", "dana@example.com", "DANA@EXAMPLE.COM"]
    )
    def test_addresses_normalise_to_one_form(self, raw):
        assert normalise_email(raw) == "dana@example.com"

    @pytest.mark.parametrize(
        "address",
        ["dana@example.com", "d.w+leetcode@gmail.com", "someone@mail.co.uk"],
    )
    def test_real_addresses_are_accepted(self, address):
        assert EMAIL_RE.match(address)
        assert problem_with_email(address) is None

    @pytest.mark.parametrize(
        "address", ["", "dana", "dana@", "@example.com", "dana@example", "a b@example.com"]
    )
    def test_nonsense_is_rejected(self, address):
        assert problem_with_email(address) is not None

    def test_an_over_long_address_is_rejected(self):
        assert problem_with_email("a" * 250 + "@example.com") is not None
