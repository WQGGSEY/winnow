"""Phase B tests: critic profile loader rejects non-practitioner voices;
extra_persona_dirs from settings get picked up."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_harness.config import ConfigError, load_critic_profile
from research_harness.critics.governance import select_critics


def test_existing_critics_all_load_under_practitioner_contract():
    repo_root = Path(__file__).resolve().parents[1]
    critic_files = [
        p for p in (repo_root / "critics").rglob("*.md")
        if p.name != "PRACTITIONER_PERSONA.md"
    ]
    assert critic_files, "no critic profiles found"
    for path in critic_files:
        load_critic_profile(path)  # must not raise


def test_critic_profile_rejected_without_practitioner_voice(tmp_path):
    bad = tmp_path / "fake.md"
    bad.write_text(
        "---\ncritic_profile_id: fake_v1\n---\n\n# Body without practitioner voice\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="practitioner"):
        load_critic_profile(bad)


def test_critic_profile_accepts_explicit_override_section(tmp_path):
    ok = tmp_path / "ok.md"
    ok.write_text(
        "---\ncritic_profile_id: explicit_override_v1\n---\n\n"
        "# Body\n\n# Practitioner Lens override\n\nMy take on so_what...\n",
        encoding="utf-8",
    )
    profile = load_critic_profile(ok)
    assert profile["critic_profile_id"] == "explicit_override_v1"


def test_extra_persona_dirs_get_scanned(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    extra = tmp_path / "team_critics"
    (extra / "always").mkdir(parents=True)
    user_critic = extra / "always" / "user_added.md"
    user_critic.write_text(
        "---\ncritic_profile_id: user_added_v1\npersona_voice: practitioner\n---\n\n# user-added critic\n",
        encoding="utf-8",
    )
    settings = {"critics": {"extra_persona_dirs": [str(extra)]}}
    node = {
        "id": "n_x", "type": "validity",
        "domain": "machine_alpha_signal_vs_datamining_discrimination",
        "stage": "promotion",
        "claim_contract": {"mandatory_baselines": [], "claim_under_test": "x"},
        "baseline_refs": [],
    }
    out = select_critics(repo_root, node, settings)
    applied_ids = {c["critic_id"] for c in out["applied_critics"]}
    assert "user_added_v1" in applied_ids
