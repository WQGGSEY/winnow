from __future__ import annotations

import json

import pytest

from research_harness.data_adapters import (
    AdapterError,
    ensure_thread_adapter_snapshots,
    probe_registered_adapters,
    resolve_registered_adapters,
    select_snapshot,
)


def _entry(source, **overrides):
    return {
        "id": "local_data",
        "materializer_type": "benchmark",
        "role": "evaluation",
        "source": str(source),
        "provenance": "test fixture",
        **overrides,
    }


def _write_settings(repo, project, operator=None):
    (repo / "settings.json").write_text(
        json.dumps({"data_adapters": {"registered": project}}), encoding="utf-8"
    )
    if operator is not None:
        (repo / "settings.local.json").write_text(
            json.dumps({"data_adapters": {"registered": operator}}), encoding="utf-8"
        )


def test_operator_override_is_resolved_per_id(tmp_path):
    project_source = tmp_path / "project.json"
    operator_source = tmp_path / "operator.json"
    project_source.write_text("project", encoding="utf-8")
    operator_source.write_text("operator", encoding="utf-8")
    _write_settings(
        tmp_path,
        [_entry(project_source)],
        [_entry(operator_source, provenance="operator fixture")],
    )

    resolved = resolve_registered_adapters(tmp_path)

    assert resolved.ready["local_data"].source == str(operator_source)
    assert resolved.ready["local_data"].scope == "operator"


def test_invalid_operator_override_fails_closed(tmp_path):
    source = tmp_path / "project.json"
    source.write_text("project", encoding="utf-8")
    _write_settings(
        tmp_path,
        [_entry(source)],
        [_entry(source, materializer_type="real_panel")],
    )

    resolved = resolve_registered_adapters(tmp_path)

    assert "local_data" not in resolved.ready
    assert resolved.unavailable["local_data"].code == "unsupported_type"


def test_probe_snapshot_identity_tracks_content(tmp_path):
    source = tmp_path / "data.json"
    source.write_text("one", encoding="utf-8")
    _write_settings(tmp_path, [_entry(source)])
    first = probe_registered_adapters(tmp_path)["snapshots"][0]
    source.write_text("two", encoding="utf-8")
    second = probe_registered_adapters(tmp_path)["snapshots"][0]

    assert first["snapshot_id"] != second["snapshot_id"]
    assert first["content_sha256"] != second["content_sha256"]


def test_thread_snapshot_is_immutable_and_selection_is_explicit(tmp_path):
    source = tmp_path / "data.json"
    source.write_text("one", encoding="utf-8")
    second_source = tmp_path / "other.json"
    second_source.write_text("other", encoding="utf-8")
    _write_settings(
        tmp_path,
        [_entry(source), _entry(second_source, id="other_data")],
    )
    thread_dir = tmp_path / "runs" / "threads" / "t1"
    first = ensure_thread_adapter_snapshots(tmp_path, thread_dir)
    source.write_text("changed", encoding="utf-8")
    second = ensure_thread_adapter_snapshots(tmp_path, thread_dir)

    assert second == first
    with pytest.raises(AdapterError, match="multiple adapters"):
        select_snapshot(first, None)
    assert select_snapshot(first, "other_data")["adapter_id"] == "other_data"


def test_unreadable_source_is_unavailable(tmp_path):
    _write_settings(tmp_path, [_entry(tmp_path / "missing.json")])

    document = probe_registered_adapters(tmp_path)

    assert document["snapshots"] == []
    assert document["problems"][0]["adapter_id"] == "local_data"


def test_top_level_symlink_source_is_unavailable(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("data", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    _write_settings(tmp_path, [_entry(link)])

    document = probe_registered_adapters(tmp_path)

    assert document["snapshots"] == []
    assert "symlink" in document["problems"][0]["message"]
