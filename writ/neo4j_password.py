"""`writ neo4j set-password`: replace the published development password, once, safely.

Program item 3 (docs/programs/knowledge-engine-program.md, section 3). DEFAULT_NEO4J_PASSWORD
is published in this repository and every Neo4jConnection refuses it
(writ.config.refuse_dev_password). Replacing it means authenticating WITH it, so this module is
the one place that opens a driver directly instead of through Neo4jConnection.

The order of operations is the safety property:
  1. Everything checkable without the database is checked first: the resolved instance is the
     one writ.toml names and the directory is writable.
  2. An exclusive lock beside writ.toml is taken (bounded wait), so concurrent runs from several
     sessions or bootstraps on one install take turns. Everything below runs inside it, starting
     with re-reading writ.toml, because the previous holder may just have changed it.
  3. The new file text is rendered and verified in memory, and the configured password is
     proven to authenticate (waiting for a booting server if asked).
  4. The new file is staged: a 0600 temp file beside writ.toml, written and fsynced.
  5. ALTER CURRENT USER changes the password in the database.
  6. The staged file replaces writ.toml (os.replace, then the directory is fsynced).
A failure in 1 to 5 leaves the database and the file exactly as they were. Between 5 and 6 is
the only place the two can disagree. Any exception there, an interruption included, changes the
database back; only when that also fails is the new password shown, because it then exists
nowhere else. A kill that runs no Python code at all leaves the staged file, which holds the new
password, so even then it is not lost. The staged file is removed on every failure path.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import secrets
import string
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from writ.config import (
    DEFAULT_NEO4J_PASSWORD,
    get_config_path,
    get_neo4j_password,
    get_neo4j_uri,
    get_neo4j_user,
    get_production_neo4j_uri,
)

GENERATED_LENGTH = 32
# Letters and digits only: Compose interpolates the value into a shell healthcheck and it is
# written into a TOML basic string, and this alphabet needs quoting in neither. 62**32 is about
# 190 bits.
GENERATED_ALPHABET = string.ascii_letters + string.digits
CONFIG_FILE_MODE = 0o600
RETRY_INTERVAL_SECONDS = 1.0
# Beside writ.toml, so every run on one install contends for the same lock; matched by the
# `.writ.toml.*` .gitignore line. It stays after a run: unlinking a flock file lets a waiter that
# already opened it lock the unlinked inode while a third run locks a fresh one.
LOCK_FILE_NAME = ".writ.toml.lock"
STAGED_FILE_PREFIX = ".writ.toml."
DEFAULT_LOCK_TIMEOUT_SECONDS = 60.0
LOCK_POLL_SECONDS = 0.1

_NEO4J_HEADER = re.compile(r"^\s*\[\s*neo4j\s*\]\s*(?:#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")
_ASSIGNMENT_LINE = re.compile(r"^\s*password\s*=")


class PasswordChangeError(RuntimeError):
    """Refused or failed in steps 1 to 5: the database and writ.toml are unchanged."""


class PasswordStoreFailed(RuntimeError):
    """Step 6 failed after the database accepted the new password.

    str() never carries a password, so a traceback or a log line cannot leak one. report() is
    what the CLI prints, and it includes the new password ONLY when the rollback failed too.
    """

    def __init__(
        self, *, config_path: str, uri: str, cause: BaseException, rolled_back: bool,
        new_password: str | None,
    ) -> None:
        super().__init__(
            f"writing {config_path} failed after Neo4j at {uri} accepted a new password"
        )
        self.config_path = config_path
        self.uri = uri
        self.cause = cause
        self.rolled_back = rolled_back
        self.new_password = new_password

    def _cause_text(self) -> str:
        return str(self.cause) or type(self.cause).__name__

    def report(self) -> str:
        if self.rolled_back:
            return (
                f"error: Neo4j at {self.uri} accepted the new password, then writing "
                f"{self.config_path} failed ({self._cause_text()}).\n"
                "The database password was changed back, so nothing changed. Fix the cause and "
                "run `writ neo4j set-password` again."
            )
        return "\n".join([
            "ERROR: NEO4J AND WRIT.TOML NOW DISAGREE.",
            f"Neo4j at {self.uri} accepted a new password, writing {self.config_path} failed "
            f"({self._cause_text()}), and changing the database back failed too.",
            "The new password exists only here:",
            "",
            f"    {self.new_password}",
            "",
            "Recover before closing this terminal, either way:",
            f"  1. Store it by hand: under [neo4j] in {self.config_path} set password to the",
            f"     value above, then run: chmod 600 {self.config_path}",
            "  2. Or fix the cause and run, with the value above:",
            "       WRIT_NEO4J_PASSWORD='<value above>' writ neo4j set-password",
            "     It authenticates with that value and stores a fresh one.",
        ])


@dataclass(frozen=True)
class PasswordChange:
    """What a successful run changed, for the next-steps text. Holds no password.

    changed is False only for an if_default run that found a private password already set.
    """

    config_path: str
    uri: str
    env_override: bool
    changed: bool = True


def generate_password() -> str:
    """A fresh random password: GENERATED_LENGTH characters from GENERATED_ALPHABET."""
    return "".join(secrets.choice(GENERATED_ALPHABET) for _ in range(GENERATED_LENGTH))


def render_config(existing: str | None, new: str) -> str:
    """writ.toml text with [neo4j] password set to `new`, every other byte kept.

    Line-based because tomllib cannot write and re-serialising would drop the comments and
    ordering an operator relies on. The edit is then VERIFIED: the result must parse to the
    original with exactly neo4j.password replaced. A form the line edit cannot express (an
    inline `neo4j = {...}` table, a dotted `neo4j.password` key, a multi-line string) fails that
    check and is refused rather than guessed at. Raises PasswordChangeError; touches no file.
    """
    line = f'password = "{new}"\n'
    if existing is None:
        return f"[neo4j]\n{line}"
    try:
        before = tomllib.loads(existing)
    except tomllib.TOMLDecodeError as exc:
        raise PasswordChangeError(
            f"writ.toml is not valid TOML ({exc}). Fix it first; nothing was changed."
        ) from exc
    section = before.get("neo4j", {})
    if not isinstance(section, dict):
        raise PasswordChangeError(
            "writ.toml has a `neo4j` key that is not a table. Fix it first; nothing was changed."
        )
    lines = existing.splitlines(keepends=True)
    header = next((i for i, text in enumerate(lines) if _NEO4J_HEADER.match(text)), None)
    if header is None:
        separator = "\n" if existing and not existing.endswith("\n") else ""
        rendered = f"{existing}{separator}\n[neo4j]\n{line}"
    else:
        if not lines[header].endswith("\n"):
            lines[header] += "\n"
        end = next(
            (i for i in range(header + 1, len(lines)) if _ANY_HEADER.match(lines[i])),
            len(lines),
        )
        target = next(
            (i for i in range(header + 1, end) if _ASSIGNMENT_LINE.match(lines[i])), None
        )
        if target is None:
            lines.insert(header + 1, line)
        else:
            lines[target] = line
        rendered = "".join(lines)
    expected = {**before, "neo4j": {**section, "password": new}}
    try:
        after = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError:
        after = None
    if after != expected:
        raise PasswordChangeError(
            "writ.toml stores [neo4j] in a form this command cannot edit safely (an inline "
            "table, a dotted key or a multi-line value). Rewrite it as a plain [neo4j] section "
            "and run again; nothing was changed."
        )
    return rendered


def stage_config(path: str, text: str, prefix: str = STAGED_FILE_PREFIX) -> str:
    """Write `text` to a new temp file beside `path`, fsynced, mode 0600; return its path.

    mkstemp creates it 0600 in the SAME directory, so the later rename is atomic and never
    exposes a partial or world-readable copy. On failure nothing is left behind.
    `prefix` names the staged file; the BM25 CURRENT pointer (writ/retrieval/pipeline.py)
    reuses this stage-then-commit write with its own prefix.
    """
    directory = os.path.dirname(path) or "."
    fd, staged = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, CONFIG_FILE_MODE)
    except BaseException:
        _discard(staged)
        raise
    return staged


def commit_config(staged: str, path: str) -> None:
    """Rename the staged file over `path`, then fsync the directory so the rename is durable."""
    os.replace(staged, path)
    try:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # The rename already happened: file and database agree. A filesystem that cannot fsync
        # a directory must not turn that into a reported failure.
        pass


def _discard(staged: str) -> None:
    try:
        os.unlink(staged)
    except FileNotFoundError:
        pass


async def _locked(directory: str, timeout: float) -> int:
    """An fd holding the exclusive set-password lock for `directory`, waiting up to `timeout`."""
    fd = os.open(os.path.join(directory, LOCK_FILE_NAME), os.O_RDWR | os.O_CREAT, CONFIG_FILE_MODE)
    try:
        os.fchmod(fd, CONFIG_FILE_MODE)
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PasswordChangeError(
                        f"another `writ neo4j set-password` has held "
                        f"{os.path.join(directory, LOCK_FILE_NAME)} for over {timeout:g}s. Let it "
                        "finish, then run this again. Nothing was changed."
                    ) from None
            await asyncio.sleep(LOCK_POLL_SECONDS)
    except BaseException:
        os.close(fd)
        raise


def _driver(uri: str, user: str, secret: str):
    from neo4j import AsyncGraphDatabase

    return AsyncGraphDatabase.driver(uri, auth=(user, secret))


async def probe(uri: str, user: str, secret: str, wait_seconds: float = 0.0) -> None:
    """Return once `secret` authenticates at `uri` (RETURN 1 on the default database).

    Retries only while the server is unreachable or still starting, until `wait_seconds` have
    passed. An authentication failure raises at once: retrying it would only trip Neo4j's
    failed-login lockout.
    """
    from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        driver = _driver(uri, user, secret)
        try:
            async with driver.session() as session:
                result = await session.run("RETURN 1")
                await result.consume()
            return
        except (ServiceUnavailable, SessionExpired, TransientError, OSError):
            if time.monotonic() >= deadline:
                raise
        finally:
            await driver.close()
        await asyncio.sleep(RETRY_INTERVAL_SECONDS)


async def alter_password(uri: str, user: str, old: str, new: str) -> None:
    """ALTER CURRENT USER on the system database, where Neo4j requires it. Parameters only:
    neither value is ever interpolated into the Cypher text."""
    driver = _driver(uri, user, old)
    try:
        async with driver.session(database="system") as session:
            result = await session.run(
                "ALTER CURRENT USER SET PASSWORD FROM $old TO $new", old=old, new=new
            )
            await result.consume()
    finally:
        await driver.close()


def _scrub(exc: BaseException, *values: str) -> str:
    text = str(exc)
    for value in values:
        if value:
            text = text.replace(value, "***")
    return text


def _probe_failure(uri: str, exc: BaseException, old: str) -> str:
    from neo4j.exceptions import AuthError

    if isinstance(exc, AuthError):
        return (
            f"The configured Neo4j password does not authenticate at {uri}. If the database uses "
            "a different one, run this once with WRIT_NEO4J_PASSWORD set to it. "
            "Nothing was changed."
        )
    return (
        f"Neo4j at {uri} could not be used ({type(exc).__name__}: {_scrub(exc, old)}). If it is "
        "starting, retry or pass --wait SECONDS; if it is stopped, run docker start writ-neo4j. "
        "Nothing was changed."
    )


def _read_existing(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise PasswordChangeError(f"{path} could not be read ({exc}). Nothing was changed.") from exc


async def set_password(
    config_path: str | None = None,
    *,
    wait_seconds: float = 0.0,
    if_default: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    probe_fn: Callable[[str, str, str, float], Awaitable[None]] = probe,
    alter_fn: Callable[[str, str, str, str], Awaitable[None]] = alter_password,
    stage_fn: Callable[[str, str], str] = stage_config,
    commit_fn: Callable[[str, str], None] = commit_config,
) -> PasswordChange:
    """Replace the configured Neo4j password. See the module docstring for the order.

    The old password is the one this process would connect with (WRIT_NEO4J_PASSWORD, then the
    file, then the default), which is what makes the env variable the recovery path when the
    file and the database disagree. if_default makes a run that finds anything but the
    development default (read inside the lock) change nothing, so concurrent bootstraps rotate
    once. The *_fn parameters exist for the ordering tests; the real ALTER and rollback are
    tested against the isolated Neo4j.
    """
    from writ.graph.db._safety import instance_key

    path = os.path.realpath(config_path or get_config_path())
    uri = get_neo4j_uri(path)
    user = get_neo4j_user(path)

    file_uri = get_production_neo4j_uri(path)
    if instance_key(uri) is None or instance_key(uri) != instance_key(file_uri):
        raise PasswordChangeError(
            f"WRIT_NEO4J_URI points at {uri}, but {path} configures {file_uri}: the new password "
            "would be saved for a different instance than the one it was set on. Unset "
            "WRIT_NEO4J_URI, or put that URI in the file first. Nothing was changed."
        )

    directory = os.path.dirname(path)
    if not os.access(directory, os.W_OK):
        raise PasswordChangeError(
            f"{directory} is not writable, so writ.toml could not be saved afterwards. "
            "Nothing was changed."
        )

    lock_fd = await _locked(directory, lock_timeout)
    try:
        return await _change_locked(
            path, uri, user, wait_seconds=wait_seconds, if_default=if_default,
            probe_fn=probe_fn, alter_fn=alter_fn, stage_fn=stage_fn, commit_fn=commit_fn,
        )
    finally:
        os.close(lock_fd)


async def _change_locked(
    path: str, uri: str, user: str, *, wait_seconds: float, if_default: bool,
    probe_fn: Callable[[str, str, str, float], Awaitable[None]],
    alter_fn: Callable[[str, str, str, str], Awaitable[None]],
    stage_fn: Callable[[str, str], str],
    commit_fn: Callable[[str, str], None],
) -> PasswordChange:
    """Steps 3 to 6, with the lock held by the caller."""
    env_override = bool((os.environ.get("WRIT_NEO4J_PASSWORD") or "").strip())
    old = get_neo4j_password(path)
    if if_default and old != DEFAULT_NEO4J_PASSWORD:
        return PasswordChange(config_path=path, uri=uri, env_override=env_override, changed=False)

    existing = _read_existing(path)
    new = generate_password()
    text = render_config(existing, new)

    try:
        await probe_fn(uri, user, old, wait_seconds)
    except Exception as exc:  # noqa: BLE001 - classified and re-raised with the remedy
        raise PasswordChangeError(_probe_failure(uri, exc, old)) from None

    try:
        staged = stage_fn(path, text)
    except OSError as exc:
        raise PasswordChangeError(
            f"the new {path} could not be staged ({exc}). Nothing was changed."
        ) from None

    try:
        try:
            await alter_fn(uri, user, old, new)
        except Exception as exc:  # noqa: BLE001
            raise PasswordChangeError(
                f"Neo4j at {uri} refused the change ({type(exc).__name__}: "
                f"{_scrub(exc, old, new)}). Nothing was changed."
            ) from None

        try:
            commit_fn(staged, path)
        except BaseException as exc:
            if not os.path.exists(staged):
                # The rename landed before the exception: file and database agree on the new
                # password, so changing the database back is what would make them disagree.
                raise
            try:
                await alter_fn(uri, user, new, old)
                rolled_back = True
            except Exception:  # noqa: BLE001 - reported through rolled_back
                rolled_back = False
            if rolled_back and not isinstance(exc, OSError):
                raise
            raise PasswordStoreFailed(
                config_path=path, uri=uri, cause=exc, rolled_back=rolled_back,
                new_password=None if rolled_back else new,
            ) from None
    finally:
        _discard(staged)

    return PasswordChange(config_path=path, uri=uri, env_override=env_override)


def next_steps_text(change: PasswordChange, install_dir: str) -> str:
    """What to do after a successful change. Never contains the password."""
    lines = [
        f"Neo4j password changed at {change.uri} and saved to {change.config_path} (mode 0600).",
        "It was not printed; `writ neo4j password` prints it when you need it.",
        "",
        "Next steps:",
        "  1. Re-create the Neo4j container so its healthcheck uses the new password and its",
        "     ports are bound to 127.0.0.1 only (the data volume is kept):",
        f'       WRIT_NEO4J_PASSWORD="$(writ neo4j password)" docker compose -f '
        f"{install_dir}/docker-compose.yml up -d neo4j",
        '     If Compose reports that the name "/writ-neo4j" is already in use, the container',
        '     belongs to an older Compose project: see docs/install.md, "Securing an existing install".',
        "  2. Restart the Writ daemon so it reconnects with the new password:",
        "       systemctl --user restart writ-server",
        f"     or, without the service: bash {install_dir}/scripts/stop-server.sh "
        f"&& bash {install_dir}/scripts/ensure-server.sh",
    ]
    if change.env_override:
        lines += [
            "",
            "Note: WRIT_NEO4J_PASSWORD is set in this environment and overrides writ.toml.",
            "      Unset it before starting anything from this shell.",
        ]
    return "\n".join(lines)
