"""Run explicitly on the staged, approved public model and numeric interpreter."""

from pathlib import Path

from test_autonomous_cycle import (
    qualify_real_pretrained_cycle_fits_under_delegation_then_waits_for_slack,
)
from test_repair_bundle import (
    qualify_actual_pretrained_adapter_disconnected_recipient_qualified_replay,
)


def test_autonomous_source_to_real_fit(tmp_path: Path) -> None:
    qualify_real_pretrained_cycle_fits_under_delegation_then_waits_for_slack(tmp_path)


def test_disconnected_real_adapter_replay(tmp_path: Path) -> None:
    qualify_actual_pretrained_adapter_disconnected_recipient_qualified_replay(tmp_path)
