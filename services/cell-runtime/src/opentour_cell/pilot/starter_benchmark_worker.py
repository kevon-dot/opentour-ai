"""Publish an installed worker's existing starter proposal or freeze to its benchmark."""

import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opentour_cell.pilot.intake_local import LocalStarterAuthority
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.slack_adapter import Settings
from opentour_cell.pilot.starter_benchmark_bridge import publish_starter_state
from opentour_cell.pilot.starter_map_confirmation import StarterCaseSession


class _RunSession(StarterCaseSession):
    """Check the requested run on every signed grant read before opening the policy."""

    def __init__(
        self,
        settings: Callable[[], Settings],
        actor: IntakePrincipal,
        run_ref: str,
        starter_revision: str,
    ) -> None:
        super().__init__(settings, actor)
        self.run_ref = run_ref
        self.starter_revision = starter_revision

    def _permit(self, cap: dict[str, Any], purpose: str) -> IntakePrincipal:
        if (
            cap["run_ref"] != self.run_ref
            or cap["starter_grant_revision"] != self.starter_revision
            or purpose != "materialize"
        ):
            raise IntakeError("starter_benchmark_unavailable")
        return super()._permit(cap, purpose)


def publish(config: Path, run_ref: str, *, completion: bool = False) -> str:
    require_ref(run_ref)
    if not config.is_absolute() or config.resolve(strict=True) != config:
        raise IntakeError("starter_benchmark_unavailable")
    installed = Settings.load(config)
    binding = installed.starter_setup
    if binding is None:
        raise IntakeError("starter_benchmark_unavailable")
    authority = LocalStarterAuthority(installed.directory / binding.selection_file, read_only=True)

    def settings() -> Settings:
        current = Settings.load(config)
        if current.binding != installed.binding or current.starter_setup != binding:
            raise IntakeError("starter_benchmark_unavailable")
        return current

    # The bridge takes its own current setup guard. Release the preflight guard
    # before it reacquires the registered-owner lock; retain the signed revision.
    with authority.setup_grant(authority.principal, read_only=True) as grant:
        session = _RunSession(settings, authority.principal, run_ref, grant.revision)
    if completion:
        from opentour_cell.pilot.starter_completion import adopt

        return adopt(session, run_ref)
    return publish_starter_state(session, run_ref).state


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        completion = args[:1] == ["--adopt-completion"]
        if completion:
            args = args[1:]
        if len(args) != 2:
            raise IntakeError("starter_benchmark_unavailable")
        state = publish(Path(args[0]), args[1], completion=completion)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        print("starter_benchmark_unavailable", file=sys.stderr)
        return 2
    print(json.dumps({"run_ref": args[1], "benchmark_state": state}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
