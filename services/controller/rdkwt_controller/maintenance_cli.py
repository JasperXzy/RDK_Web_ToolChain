from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from rdkwt_controller import __version__
from rdkwt_controller.infrastructure.archives import ArchiveError, BackupArchive
from rdkwt_controller.settings import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rdkwt-maintenance",
        description="Offline backup verification and restore for RDK WebToolChain",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="create a complete local backup")
    backup.add_argument("--without-runs", action="store_true")
    backup.add_argument("--without-credentials", action="store_true")
    commands.add_parser("list", help="list and verify backups")
    verify = commands.add_parser("verify", help="verify a backup without restoring it")
    verify.add_argument("--archive", required=True)
    restore = commands.add_parser("restore", help="restore a verified backup while stopped")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--confirm", required=True)
    return parser


def _archive_path(manager: BackupArchive, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or "/" in value:
        return candidate
    return manager.resolve(value)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    arguments = _parser().parse_args()
    settings = Settings.from_env()
    settings.ensure_directories()
    manager = BackupArchive(settings, app_version=__version__)
    try:
        if arguments.command == "backup":
            _emit(
                manager.create(
                    include_runs=not arguments.without_runs,
                    include_credentials=not arguments.without_credentials,
                )
            )
        elif arguments.command == "list":
            _emit(manager.list())
        elif arguments.command == "verify":
            _emit(manager.verify(_archive_path(manager, arguments.archive)))
        elif arguments.command == "restore":
            _emit(
                manager.restore(
                    _archive_path(manager, arguments.archive),
                    confirmation=arguments.confirm,
                )
            )
    except (ArchiveError, FileNotFoundError) as exc:
        code = exc.code if isinstance(exc, ArchiveError) else "BACKUP_NOT_FOUND"
        _emit({"ok": False, "code": code, "detail": str(exc)})
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
