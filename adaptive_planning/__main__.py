"""Shared planning command entry point."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence
from urllib.parse import urlsplit

import uvicorn

from adaptive_planning.admin import initialize_installation
from adaptive_planning.app import build_app
from adaptive_planning.config import Config, load_config
from adaptive_planning.types import DomainError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error includes rejected peer input.
        self.exit(2, "Invalid command. Use adaptive-planning --help.\n")


def build_server(config: Config) -> uvicorn.Server:
    app = build_app(config)
    try:
        return uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=urlsplit(config.base_url).port,
                                            access_log=False, proxy_headers=False, lifespan="on"))
    except BaseException:
        app.state.close()
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(prog="adaptive-planning", description="Shared adaptive planning service")
    parser.add_argument("command", choices=("init", "serve"))
    arguments = parser.parse_args(argv)
    try:
        config = load_config(os.environ)
        if arguments.command == "init":
            initialize_installation(config)
            print("Installation initialized.")
        else:
            server = build_server(config)
            try:
                server.run()
            finally:
                server.config.app.state.close()
    except DomainError as error:
        code = error.code if error.code in {"not_initialized", "insecure_credential", "invalid_credential",
                                           "invalid_config", "init_failed"} else "startup_failed"
        print(f"Service startup failed: {code}.", file=sys.stderr)
        return 1
    except Exception:
        print("Service startup failed: internal_error.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
