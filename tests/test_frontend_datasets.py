"""Tests for the operator-scope data adapter management (Phase 1b).

Covers the standalone module API (datasets.py) and the FastAPI routes
that expose it under ``/datasets``.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from research_harness.frontend import datasets
from research_harness.frontend.server import create_app


def _seed_project_settings(repo: Path, registered: list[dict] | None = None) -> None:
    data = {"data_adapters": {"registered": registered or []}}
    (repo / "settings.json").write_text(json.dumps(data), encoding="utf-8")


def _read_local(repo: Path) -> dict:
    p = repo / "settings.local.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


class DatasetsModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    # ---- upload_file

    def test_upload_writes_file_and_computes_sha(self) -> None:
        payload = b"hello,world\n1,2\n3,4\n"
        result = datasets.upload_file(
            self.repo,
            adapter_id="my_csv",
            original_filename="data.csv",
            source=io.BytesIO(payload),
        )
        self.assertEqual(result.adapter_id, "my_csv")
        self.assertEqual(result.size_bytes, len(payload))
        self.assertEqual(result.original_filename, "data.csv")
        self.assertTrue(result.materialized_path.exists())
        self.assertEqual(result.materialized_path.read_bytes(), payload)
        # sha256 of payload (computed independently)
        import hashlib

        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())

    def test_upload_rejects_disallowed_extension(self) -> None:
        with self.assertRaises(datasets.DatasetError):
            datasets.upload_file(
                self.repo,
                adapter_id="evil",
                original_filename="payload.sh",
                source=io.BytesIO(b"#!/bin/sh\nrm -rf /"),
            )

    def test_upload_strips_path_components_from_filename(self) -> None:
        # Path traversal in original_filename must not escape the upload dir.
        result = datasets.upload_file(
            self.repo,
            adapter_id="safe",
            original_filename="../../etc/passwd.csv",
            source=io.BytesIO(b"a,b\n"),
        )
        # The file ended up under the adapter's directory with the stripped name.
        self.assertEqual(result.materialized_path.name, "passwd.csv")
        self.assertIn("operator_uploads", str(result.materialized_path))

    def test_upload_rejects_invalid_adapter_id(self) -> None:
        for bad in ["", "a b", "../oops", "-leading-hyphen"]:
            with self.assertRaises(datasets.DatasetError):
                datasets.upload_file(
                    self.repo,
                    adapter_id=bad,
                    original_filename="x.csv",
                    source=io.BytesIO(b"x"),
                )

    def test_upload_cleans_up_partial_file_on_oversize(self) -> None:
        # Forge a too-large payload by overriding MAX_UPLOAD_BYTES via monkeypatch.
        original = datasets.MAX_UPLOAD_BYTES
        try:
            datasets.MAX_UPLOAD_BYTES = 8  # bytes
            with self.assertRaises(datasets.DatasetError):
                datasets.upload_file(
                    self.repo,
                    adapter_id="big",
                    original_filename="big.csv",
                    source=io.BytesIO(b"1234567890ABCDEF"),  # 16 bytes
                )
            # No leftover .upload-tmp inside the adapter dir
            adapter_dir = datasets.upload_root(self.repo) / "big"
            if adapter_dir.exists():
                leftovers = list(adapter_dir.glob("*.upload-tmp"))
                self.assertEqual(leftovers, [], "partial upload tmp should be cleaned up")
        finally:
            datasets.MAX_UPLOAD_BYTES = original

    # ---- register_adapter

    def test_register_appends_to_operator_scope(self) -> None:
        datasets.register_adapter(
            self.repo,
            adapter_id="local_x",
            materializer_type="custom",
            role="other",
            source="file:///abs/local/x",
            provenance="manually placed",
        )
        local = _read_local(self.repo)
        regs = local["data_adapters"]["registered"]
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["id"], "local_x")
        self.assertEqual(regs[0]["source"], "file:///abs/local/x")

    def test_register_replaces_in_place_on_duplicate_id(self) -> None:
        datasets.register_adapter(
            self.repo,
            adapter_id="dup",
            materializer_type="custom",
            role="other",
            source="file:///v1",
            provenance="first",
        )
        datasets.register_adapter(
            self.repo,
            adapter_id="dup",
            materializer_type="raw_data",
            role="training",
            source="file:///v2",
            provenance="second",
        )
        regs = _read_local(self.repo)["data_adapters"]["registered"]
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["source"], "file:///v2")
        self.assertEqual(regs[0]["materializer_type"], "raw_data")
        self.assertEqual(regs[0]["role"], "training")

    def test_register_preserves_other_operator_keys(self) -> None:
        # Pre-existing operator state (e.g. ack settings) must survive registration.
        (self.repo / "settings.local.json").write_text(
            json.dumps({"frontend": {"subscription_ack_at": "2026-01-01T00:00:00+00:00"}}),
            encoding="utf-8",
        )
        datasets.register_adapter(
            self.repo,
            adapter_id="a",
            materializer_type="custom",
            role="other",
            source="/abs/a",
            provenance="p",
        )
        local = _read_local(self.repo)
        self.assertEqual(
            local["frontend"]["subscription_ack_at"], "2026-01-01T00:00:00+00:00"
        )
        self.assertEqual(local["data_adapters"]["registered"][0]["id"], "a")

    def test_register_rejects_invalid_kind(self) -> None:
        with self.assertRaises(datasets.DatasetError):
            datasets.register_adapter(
                self.repo,
                adapter_id="x",
                materializer_type="bogus_kind",
                role="other",
                source="/abs/x",
                provenance="p",
            )

    def test_register_rejects_non_local_source(self) -> None:
        # http URLs are not what the local materializer handles; reject early.
        with self.assertRaises(datasets.DatasetError):
            datasets.register_adapter(
                self.repo,
                adapter_id="x",
                materializer_type="custom",
                role="other",
                source="https://example.com/data.csv",
                provenance="p",
            )

    # ---- list_adapters

    def test_list_merges_project_and_operator_scope(self) -> None:
        _seed_project_settings(
            self.repo,
            [
                {"id": "proj_a", "materializer_type": "benchmark", "role": "evaluation", "source": "/missing/project", "provenance": "pa"},
                {"id": "both", "materializer_type": "custom", "role": "other", "source": "/proj/path", "provenance": "from-project"},
            ],
        )
        datasets.register_adapter(
            self.repo,
            adapter_id="op_b",
            materializer_type="custom",
            role="other",
            source="/op/path",
            provenance="from-operator",
        )
        datasets.register_adapter(
            self.repo,
            adapter_id="both",
            materializer_type="custom",
            role="other",
            source="/op/override",
            provenance="overridden",
        )

        adapters = datasets.list_adapters(self.repo)
        by_id = {a["id"]: a for a in adapters}
        self.assertEqual(set(by_id.keys()), {"proj_a", "both", "op_b"})
        self.assertEqual(by_id["proj_a"]["_scope"], "project")
        self.assertEqual(by_id["op_b"]["_scope"], "operator")
        # operator overrides project for matching id
        self.assertEqual(by_id["both"]["_scope"], "operator")
        self.assertEqual(by_id["both"]["source"], "/op/override")

    # ---- delete_adapter

    def test_delete_removes_operator_entry_and_uploaded_files(self) -> None:
        # Upload then delete
        result = datasets.upload_file(
            self.repo,
            adapter_id="del_me",
            original_filename="x.csv",
            source=io.BytesIO(b"x,y\n1,2\n"),
        )
        datasets.register_adapter(
            self.repo,
            adapter_id="del_me",
            materializer_type="custom",
            role="other",
            source=f"file://{result.materialized_path}",
            provenance="to be deleted",
        )
        self.assertTrue(result.materialized_path.exists())

        removed = datasets.delete_adapter(self.repo, "del_me")
        self.assertTrue(removed)
        # registry entry gone
        regs = _read_local(self.repo)["data_adapters"]["registered"]
        self.assertEqual(regs, [])
        # uploaded files cleaned up
        self.assertFalse((datasets.upload_root(self.repo) / "del_me").exists())

    def test_delete_returns_false_on_missing(self) -> None:
        self.assertFalse(datasets.delete_adapter(self.repo, "never_existed"))

    def test_delete_does_not_remove_arbitrary_paths(self) -> None:
        # Register a path OUTSIDE operator_uploads. Delete must remove the
        # registry entry but never touch the external file.
        external = self.repo / "external_data" / "panel.csv"
        external.parent.mkdir(parents=True)
        external.write_text("a,b\n")
        datasets.register_adapter(
            self.repo,
            adapter_id="external",
            materializer_type="custom",
            role="other",
            source=f"file://{external}",
            provenance="path-registered, lives outside uploads",
        )
        self.assertTrue(datasets.delete_adapter(self.repo, "external"))
        self.assertTrue(external.exists(), "external file must not be deleted")


class DatasetsRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.client = TestClient(create_app(self.repo))

    def test_page_renders(self) -> None:
        resp = self.client.get("/datasets")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Registered adapters", resp.text)
        self.assertIn("Upload a file", resp.text)
        self.assertIn("Register by path", resp.text)

    def test_upload_endpoint_writes_and_registers(self) -> None:
        resp = self.client.post(
            "/datasets/upload",
            data={
                "adapter_id": "upload_test",
                "materializer_type": "custom",
                "role": "other",
                "provenance": "test upload",
            },
            files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Uploaded and registered upload_test", resp.text)
        self.assertIn("upload_test", resp.text)
        self.assertIn("ready", resp.text)
        self.assertIn('/datasets/upload_test/delete', resp.text)
        # File on disk
        target = datasets.upload_root(self.repo) / "upload_test" / "data.csv"
        self.assertTrue(target.exists())
        # Registry entry
        regs = _read_local(self.repo)["data_adapters"]["registered"]
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["id"], "upload_test")
        self.assertEqual(regs[0]["materializer_type"], "custom")
        self.assertEqual(regs[0]["role"], "other")
        self.assertIn("_upload", regs[0])

    def test_upload_rejects_bad_extension_via_route(self) -> None:
        resp = self.client.post(
            "/datasets/upload",
            data={"adapter_id": "evil", "materializer_type": "custom", "role": "other", "provenance": "p"},
            files={"file": ("payload.sh", b"#!/bin/sh\n", "application/x-sh")},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("file extension not allowed", resp.text)
        # No registry entry created
        self.assertEqual(_read_local(self.repo), {})

    def test_register_by_path_route(self) -> None:
        resp = self.client.post(
            "/datasets/register",
            data={
                "adapter_id": "path_reg",
                "materializer_type": "custom",
                "role": "other",
                "source": "/abs/some/path",
                "provenance": "registered by path",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Registered path_reg", resp.text)
        regs = _read_local(self.repo)["data_adapters"]["registered"]
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0]["source"], "/abs/some/path")
        self.assertNotIn("_upload", regs[0])

    def test_delete_route_round_trip(self) -> None:
        # register then delete
        self.client.post(
            "/datasets/register",
            data={
                "adapter_id": "del_test",
                "materializer_type": "custom",
                "role": "other",
                "source": "/abs/x",
                "provenance": "to be deleted",
            },
        )
        resp = self.client.post("/datasets/del_test/delete")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Removed del_test", resp.text)
        regs = _read_local(self.repo).get("data_adapters", {}).get("registered", [])
        self.assertEqual(regs, [])

    def test_delete_route_on_missing_returns_error_message(self) -> None:
        resp = self.client.post("/datasets/nonexistent/delete")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("not found at operator scope", resp.text)

    def test_topbar_link_present_on_index(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('href="/datasets"', resp.text)


if __name__ == "__main__":
    unittest.main()
