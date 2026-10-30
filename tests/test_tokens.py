"""The per-user token store: parsing, validation, lookup and the CLI."""

import datetime as dt
import os
import time

import pytest

from src import tokens
from src.tokens import (
    Principal,
    TokenFileError,
    TokenStore,
    generate_token,
    hash_token,
    parse_tokens,
)

ALICE = generate_token()
BOB = generate_token()


def yaml_for(*rows):
    lines = ["tokens:"]
    for row in rows:
        lines.append(f"  - name: {row['name']}")
        lines.append(f"    role: {row['role']}")
        digest = row["sha256"] if "sha256" in row else hash_token(row["token"])
        lines.append(f"    sha256: {digest}")
        if "expires" in row:
            lines.append(f"    expires: {row['expires']}")
    return "\n".join(lines) + "\n"


def two_users():
    return yaml_for(
        {"name": "alice", "role": "admin", "token": ALICE},
        {"name": "bob", "role": "viewer", "token": BOB},
    )


class TestTokens:
    def test_generated_tokens_are_long_and_different(self):
        a, b = generate_token(), generate_token()
        assert len(a) == 64 and a != b

    def test_hash_is_sha256_hex(self):
        assert hash_token("abc") == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )


class TestParsing:
    def test_a_valid_file(self):
        entries = parse_tokens(two_users())
        assert [(e.name, e.role) for e in entries] == [
            ("alice", "admin"),
            ("bob", "viewer"),
        ]
        assert entries[0].sha256 == hash_token(ALICE)

    def test_uppercase_hashes_are_accepted_and_normalised(self):
        text = yaml_for(
            {"name": "a", "role": "admin", "sha256": hash_token(ALICE).upper()}
        )
        assert parse_tokens(text)[0].sha256 == hash_token(ALICE)

    @pytest.mark.parametrize(
        "text, message",
        [
            ("not: a list", "'tokens:' list"),
            ("tokens: []", "empty"),
            ("tokens: [1]", "must be a mapping"),
            (": : :", "not valid YAML"),
        ],
    )
    def test_structural_errors(self, text, message):
        with pytest.raises(TokenFileError, match=message):
            parse_tokens(text)

    @pytest.mark.parametrize("name", ["", "has space", "-leading", "x" * 65, "a/b"])
    def test_bad_names(self, name):
        text = (
            f'tokens:\n  - name: "{name}"\n    role: admin\n'
            f"    sha256: {hash_token('x')}\n"
        )
        with pytest.raises(TokenFileError, match="name"):
            parse_tokens(text)

    @pytest.mark.parametrize("role", ["root", "Admin", "", "superuser"])
    def test_bad_roles(self, role):
        text = (
            f'tokens:\n  - name: a\n    role: "{role}"\n    sha256: {hash_token("x")}\n'
        )
        with pytest.raises(TokenFileError, match="role"):
            parse_tokens(text)

    @pytest.mark.parametrize(
        "digest", ["abc", "z" * 64, ALICE + "0", "", hash_token("x")[:-1]]
    )
    def test_bad_hashes_point_at_the_command_that_makes_one(self, digest):
        text = f'tokens:\n  - name: a\n    role: admin\n    sha256: "{digest}"\n'
        with pytest.raises(TokenFileError, match="python -m src.tokens new"):
            parse_tokens(text)

    def test_a_raw_token_pasted_instead_of_its_hash_is_rejected(self):
        """64 hex characters like a hash, but with a typo is the common mistake;
        a token of the wrong shape must not be accepted as a hash."""
        text = f"tokens:\n  - name: a\n    role: admin\n    sha256: {ALICE[:-2]}gg\n"
        with pytest.raises(TokenFileError):
            parse_tokens(text)

    def test_duplicate_names(self):
        text = yaml_for(
            {"name": "a", "role": "admin", "token": ALICE},
            {"name": "a", "role": "viewer", "token": BOB},
        )
        with pytest.raises(TokenFileError, match="duplicate name"):
            parse_tokens(text)

    def test_duplicate_hashes(self):
        text = yaml_for(
            {"name": "a", "role": "admin", "token": ALICE},
            {"name": "b", "role": "viewer", "token": ALICE},
        )
        with pytest.raises(TokenFileError, match="already used"):
            parse_tokens(text)

    def test_unknown_keys_are_an_error_not_ignored(self):
        text = two_users().replace("role: viewer", "role: viewer\n    scope: all")
        with pytest.raises(TokenFileError, match="unknown keys"):
            parse_tokens(text)

    def test_expiry_forms(self):
        for value in ("2030-01-01", "2030-01-01T12:00:00", "2030-01-01T12:00:00+02:00"):
            entry = parse_tokens(
                yaml_for(
                    {"name": "a", "role": "admin", "token": ALICE, "expires": value}
                )
            )[0]
            assert entry.expires.tzinfo is not None

    def test_a_bare_date_lasts_to_the_end_of_that_day(self):
        entry = parse_tokens(
            yaml_for(
                {"name": "a", "role": "admin", "token": ALICE, "expires": "2030-01-01"}
            )
        )[0]
        assert not entry.expired(dt.datetime(2030, 1, 1, 23, 0, tzinfo=dt.timezone.utc))
        assert entry.expired(dt.datetime(2030, 1, 2, 0, 0, tzinfo=dt.timezone.utc))

    def test_bad_expiry(self):
        text = yaml_for(
            {"name": "a", "role": "admin", "token": ALICE, "expires": "next week"}
        )
        with pytest.raises(TokenFileError, match="expires"):
            parse_tokens(text)


class TestRoles:
    @pytest.mark.parametrize(
        "role, needed, allowed",
        [
            ("viewer", "viewer", True),
            ("viewer", "operator", False),
            ("viewer", "admin", False),
            ("operator", "viewer", True),
            ("operator", "operator", True),
            ("operator", "admin", False),
            ("admin", "viewer", True),
            ("admin", "operator", True),
            ("admin", "admin", True),
        ],
    )
    def test_each_role_includes_the_ones_below(self, role, needed, allowed):
        assert Principal("x", role, "token-file").allows(needed) is allowed


class TestStore:
    @pytest.fixture
    def path(self, tmp_path):
        p = tmp_path / "tokens.yaml"
        p.write_text(two_users())
        return str(p)

    def test_lookup_finds_the_owner(self, path):
        principal, reason, name = TokenStore(path).lookup(ALICE)
        assert principal == Principal("alice", "admin", "token-file")
        assert reason is None and name == "alice"

    def test_each_user_gets_their_own_role(self, path):
        principal, _, _ = TokenStore(path).lookup(BOB)
        assert (principal.name, principal.role) == ("bob", "viewer")

    def test_an_unknown_token_is_invalid(self, path):
        assert TokenStore(path).lookup(generate_token()) == (None, "invalid", None)

    def test_the_hash_is_not_a_token(self, path):
        """Knowing the file is not enough to log in."""
        assert TokenStore(path).lookup(hash_token(ALICE))[0] is None

    def test_an_expired_token_is_refused_and_names_its_owner(self, tmp_path):
        p = tmp_path / "t.yaml"
        p.write_text(
            yaml_for(
                {
                    "name": "old",
                    "role": "admin",
                    "token": ALICE,
                    "expires": "2020-01-01",
                }
            )
        )
        assert TokenStore(str(p)).lookup(ALICE) == (None, "expired", "old")

    def test_a_token_that_expires_later_works_now(self, tmp_path):
        p = tmp_path / "t.yaml"
        p.write_text(
            yaml_for(
                {"name": "a", "role": "admin", "token": ALICE, "expires": "2999-01-01"}
            )
        )
        assert TokenStore(str(p)).lookup(ALICE)[0] is not None

    def test_every_entry_is_compared_without_an_early_exit(self, path, monkeypatch):
        """The time taken must not reveal which entry matched."""
        calls = []
        real = tokens.hmac.compare_digest
        monkeypatch.setattr(
            tokens.hmac,
            "compare_digest",
            lambda a, b: calls.append(b) or real(a, b),
        )
        TokenStore(path).lookup(ALICE)  # matches the first entry
        assert len(calls) == 2

    def test_changes_to_the_file_are_picked_up(self, path):
        store = TokenStore(path)
        assert store.lookup(BOB)[0] is not None
        os.utime(path, (time.time() + 5, time.time() + 5))
        with open(path, "w") as f:
            f.write(yaml_for({"name": "alice", "role": "admin", "token": ALICE}))
        os.utime(path, (time.time() + 10, time.time() + 10))
        assert store.lookup(BOB)[0] is None  # revoked without a restart
        assert store.lookup(ALICE)[0] is not None

    def test_a_file_broken_after_loading_fails_closed(self, path):
        store = TokenStore(path)
        store.lookup(ALICE)
        with open(path, "w") as f:
            f.write("tokens: [")
        os.utime(path, (time.time() + 10, time.time() + 10))
        with pytest.raises(TokenFileError):
            store.lookup(ALICE)

    def test_a_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(TokenFileError, match="cannot read"):
            TokenStore(str(tmp_path / "nope.yaml")).lookup(ALICE)


class TestCommandLine:
    def test_new_prints_a_token_and_a_matching_entry(self, capsys):
        assert tokens.main(["new", "--name", "carol", "--role", "operator"]) == 0
        out = capsys.readouterr().out
        token = next(w for w in out.split() if len(w) == 64 and w.isalnum())
        assert "name: carol" in out and "role: operator" in out
        assert f"sha256: {hash_token(token)}" in out
        assert token not in out.split("sha256:")[1]  # the entry has the hash only

    def test_the_printed_entry_is_valid_in_a_file(self, capsys, tmp_path):
        tokens.main(["new", "--name", "carol", "--role", "admin"])
        out = capsys.readouterr().out
        entry = out[out.index("  - name:") :]
        entries = parse_tokens("tokens:\n" + entry)
        assert entries[0].name == "carol"

    def test_new_rejects_a_bad_name(self, capsys):
        assert tokens.main(["new", "--name", "bad name", "--role", "admin"]) == 2

    def test_new_rejects_a_bad_role(self):
        with pytest.raises(SystemExit):
            tokens.main(["new", "--name", "a", "--role", "root"])

    def test_check_accepts_a_good_file(self, tmp_path, capsys):
        p = tmp_path / "t.yaml"
        p.write_text(two_users())
        assert tokens.main(["check", str(p)]) == 0
        out = capsys.readouterr().out
        assert "ok: alice (admin)" in out and "ok: bob (viewer)" in out

    def test_check_reports_a_bad_file(self, tmp_path, capsys):
        p = tmp_path / "t.yaml"
        p.write_text("tokens: []")
        assert tokens.main(["check", str(p)]) == 1
        assert "invalid" in capsys.readouterr().err
