"""Explicit local installation: the only place a service credential is ever generated."""
from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path

from adaptive_planning.config import Config
from adaptive_planning.types import DomainError

TOKEN_FILENAME = "service-token"
_MAX_TOKEN_FILE_BYTES = 1024
_TOKEN = re.compile(rb"[\x21-\x7e]{32,512}")


def _token_path(config: Config) -> Path:
    return config.data_dir / TOKEN_FILENAME


def initialize_installation(config: Config) -> None:
    """Create the data directory and a private random credential if none exists.

    An existing credential is validated and preserved byte for byte, never regenerated.
    """
    try:
        load_service_token(config)
        return
    except DomainError as error:
        if error.code != "not_initialized":
            raise
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _publish_token(config.data_dir, _token_path(config))
    except OSError:
        raise DomainError("init_failed", "The service data directory could not be initialized.") from None
    load_service_token(config)


def _publish_token(directory: Path, path: Path) -> None:
    # The complete private file is created under a temporary name and hard-linked into place:
    # readers never see a partial credential and a concurrent initializer cannot overwrite it.
    temporary = directory / f".{TOKEN_FILENAME}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_urlsafe(32).encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def load_service_token(config: Config) -> str:
    """Read the existing credential; never creates one. Errors never include path or content."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(_token_path(config), flags)
    except FileNotFoundError:
        raise DomainError("not_initialized", "Service credential is not initialized; run explicit initialization.") from None
    except OSError:
        raise _insecure() from None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise _insecure()
        if os.name != "nt" and (details.st_uid != os.geteuid() or details.st_mode & 0o177):
            raise _insecure()
        if details.st_size > _MAX_TOKEN_FILE_BYTES:
            raise _invalid()
        content = b""
        while len(content) <= _MAX_TOKEN_FILE_BYTES:
            chunk = os.read(descriptor, _MAX_TOKEN_FILE_BYTES + 1 - len(content))
            if not chunk:
                break
            content += chunk
    except OSError:
        raise _insecure() from None
    finally:
        os.close(descriptor)
    token = content.removesuffix(b"\n").removesuffix(b"\r")
    if len(content) > _MAX_TOKEN_FILE_BYTES or _TOKEN.fullmatch(token) is None:
        raise _invalid()
    return token.decode("ascii")


def _insecure() -> DomainError:
    return DomainError(
        "insecure_credential", "Service credential must be a regular private file owned by the service user."
    )


def _invalid() -> DomainError:
    return DomainError("invalid_credential", "Service credential content is not valid.")
