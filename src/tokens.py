"""
Per-user API tokens and roles.

A single shared token tells you that *someone* with the secret called the API. A
token per person or system tells you who, lets you take access away from one of
them without touching the rest, and lets you give a dashboard read-only access.

Tokens live in a YAML file named by CHAOS_API_TOKENS_FILE. The file holds the
SHA-256 *hash* of each token, never the token itself, so a leaked config file or
backup does not hand out working credentials. (A fast hash is fine here because
tokens are 256 random bits, not human-chosen passwords.)

    tokens:
      - name: alice
        role: admin
        sha256: 3f1c...           # python -m src.tokens new --name alice --role admin
      - name: grafana
        role: viewer
        sha256: 9ab2...
        expires: 2027-06-30       # optional; after this date the token stops working

Roles, each including the ones before it:

    viewer    read status, config, limits and metrics summary
    operator  also start/stop the agent and run or abort injections
    admin     also change configuration, reset the kill switch and metrics,
              and read the audit log

Create a token and its file entry:

    python -m src.tokens new --name alice --role operator

Check a file before deploying it:

    python -m src.tokens check tokens.yaml

The file is re-read when it changes, so adding or revoking a token takes effect
without a restart. A file that cannot be parsed makes the API refuse every
request (fail closed) rather than keep serving from a stale copy.
"""

import argparse
import datetime as dt
import hashlib
import hmac
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

TOKENS_FILE_ENV = "CHAOS_API_TOKENS_FILE"

VIEWER = "viewer"
OPERATOR = "operator"
ADMIN = "admin"
ROLES = (VIEWER, OPERATOR, ADMIN)  # lowest to highest
_RANK = {role: rank for rank, role in enumerate(ROLES)}

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class TokenFileError(ValueError):
    """The tokens file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class Principal:
    """Who is making a request."""

    name: str
    role: str
    method: str  # token-file, shared-token or auth-disabled

    def allows(self, required_role: str) -> bool:
        return _RANK[self.role] >= _RANK[required_role]


@dataclass(frozen=True)
class TokenEntry:
    name: str
    role: str
    sha256: str
    expires: Optional[dt.datetime] = None  # timezone-aware UTC

    def expired(self, now: Optional[dt.datetime] = None) -> bool:
        if self.expires is None:
            return False
        return (now or dt.datetime.now(dt.timezone.utc)) >= self.expires


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    """A new random token: 256 bits, 64 hex characters."""
    return secrets.token_hex(32)


def _parse_expiry(value: object, where: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        # A bare date means the end of that day.
        parsed = dt.datetime.combine(value, dt.time.max)
    elif isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
        except ValueError:
            raise TokenFileError(
                f"{where}: 'expires' must be a date such as 2027-06-30, got {value!r}"
            ) from None
    else:
        raise TokenFileError(f"{where}: 'expires' must be a date, got {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_tokens(text: str, source: str = "tokens file") -> List[TokenEntry]:
    """Parse and validate the contents of a tokens file."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise TokenFileError(f"{source} is not valid YAML: {e}") from None

    if not isinstance(data, dict) or not isinstance(data.get("tokens"), list):
        raise TokenFileError(f"{source} must contain a 'tokens:' list")
    if not data["tokens"]:
        raise TokenFileError(f"{source} has an empty 'tokens:' list")

    entries: List[TokenEntry] = []
    names = set()
    hashes = set()
    for index, raw in enumerate(data["tokens"], start=1):
        where = f"{source}, entry {index}"
        if not isinstance(raw, dict):
            raise TokenFileError(f"{where} must be a mapping")
        unknown = set(raw) - {"name", "role", "sha256", "expires"}
        if unknown:
            raise TokenFileError(f"{where} has unknown keys: {sorted(unknown)}")

        name = raw.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.match(name):
            raise TokenFileError(
                f"{where}: 'name' must be 1-64 characters (letters, digits, "
                f"'_', '.', '@', '-'), got {name!r}"
            )
        if name in names:
            raise TokenFileError(f"{where}: duplicate name {name!r}")

        role = raw.get("role")
        if role not in _RANK:
            raise TokenFileError(
                f"{where}: 'role' must be one of {', '.join(ROLES)}, got {role!r}"
            )

        digest = raw.get("sha256")
        if not isinstance(digest, str) or not HASH_PATTERN.match(digest.lower()):
            raise TokenFileError(
                f"{where}: 'sha256' must be 64 hex characters (the hash of the "
                f"token, not the token). Create one with: "
                f"python -m src.tokens new --name {name} --role {role}"
            )
        digest = digest.lower()
        if digest in hashes:
            raise TokenFileError(f"{where}: this hash is already used by another entry")

        expires = (
            _parse_expiry(raw["expires"], where)
            if raw.get("expires") is not None
            else None
        )

        names.add(name)
        hashes.add(digest)
        entries.append(TokenEntry(name, role, digest, expires))
    return entries


def load_tokens(path: str) -> List[TokenEntry]:
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise TokenFileError(
            f"cannot read tokens file {path}: {e.strerror or e}"
        ) from e
    return parse_tokens(text, source=path)


class TokenStore:
    """
    The tokens from a file, reloaded whenever the file changes.

    Each lookup compares the presented token against every entry, with no early
    exit, so the time taken does not reveal which user (if any) matched.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._entries: List[TokenEntry] = []
        self._stamp: Optional[Tuple[int, int]] = None

    def entries(self) -> List[TokenEntry]:
        """The current entries. Raises TokenFileError if the file is bad."""
        try:
            stat = Path(self.path).stat()
        except OSError as e:
            self._stamp = None
            raise TokenFileError(
                f"cannot read tokens file {self.path}: {e.strerror or e}"
            ) from e
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp != self._stamp:
            self._entries = load_tokens(self.path)
            self._stamp = stamp
        return self._entries

    def lookup(
        self, token: str, now: Optional[dt.datetime] = None
    ) -> Tuple[Optional[Principal], Optional[str], Optional[str]]:
        """
        Identify the owner of ``token``.

        Returns (principal, reason, name). On success reason is None. On failure
        principal is None and reason is "invalid" (no such token) or "expired"
        (a real token past its expiry date, with name set so it can be logged).
        """
        digest = hash_token(token)
        match: Optional[TokenEntry] = None
        for entry in self.entries():
            if hmac.compare_digest(digest, entry.sha256):
                match = entry
        if match is None:
            return None, "invalid", None
        if match.expired(now):
            return None, "expired", match.name
        return Principal(match.name, match.role, "token-file"), None, match.name


def format_entry(name: str, role: str, token: str) -> str:
    """The YAML lines to add to a tokens file for ``token``."""
    return (
        f"  - name: {name}\n" f"    role: {role}\n" f"    sha256: {hash_token(token)}\n"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.tokens", description="Manage API tokens"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create a token and its tokens-file entry")
    new.add_argument("--name", required=True)
    new.add_argument("--role", required=True, choices=ROLES)

    check = sub.add_parser("check", help="validate a tokens file")
    check.add_argument("file")

    args = parser.parse_args(argv)

    if args.command == "new":
        if not NAME_PATTERN.match(args.name):
            print(f"error: invalid name {args.name!r}", file=sys.stderr)
            return 2
        token = generate_token()
        print("Token (shown once, give it to the user, do not store it):")
        print(f"  {token}\n")
        print(
            f"Add this to the 'tokens:' list in the file {TOKENS_FILE_ENV} points to:"
        )
        print(format_entry(args.name, args.role, token))
        return 0

    try:
        entries = load_tokens(args.file)
    except TokenFileError as e:
        print(f"invalid: {e}", file=sys.stderr)
        return 1
    for entry in entries:
        expiry = f", expires {entry.expires.date()}" if entry.expires else ""
        print(f"ok: {entry.name} ({entry.role}{expiry})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
