from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_harness.acquisition import PublicSource, RegisteredSource
from research_harness.acquisition.live_http import CredentialProfileUnavailable
from research_harness.agent_runtime import AgentUsage, CompletionResult
from research_harness.orchestrator.blind_mcp_adapter import (
    BlindMcpAdapterError,
    CodexBlindDirectionGenerator,
    CodexStructuralEquivalenceAssessor,
    SettingsCredentialProvider,
    build_acquisition_command,
)
from research_harness.orchestrator.blind_reorientation import (
    AcquisitionReserved,
    DirectionAttemptRef,
    ReorientationState,
    make_acquisition_reservation,
)
from research_harness.orchestrator.direction_generation import (
    DataNeed,
    make_direction_draft,
    make_direction_fingerprint,
    parse_direction_draft,
    serialize_direction_draft,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class _Completion:
    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = list(responses)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return CompletionResult(
            text=json.dumps(self.responses.pop(0)),
            usage=AgentUsage(output_tokens=10),
            thread_id=None,
        )


def _fingerprint(label: str):
    return make_direction_fingerprint(
        mechanism=f"{label} feedback",
        intervention=f"{label} scheduler",
        observables_and_data=f"{label} measurements",
        analysis_unit=f"{label} cohort",
        timescale=f"{label} month",
        system_boundary=f"{label} service",
    )


@pytest.mark.parametrize("intervention", ["adaptive scheduling", "sample tasks without replacement and adapt scheduling"])
def test_codex_direction_generator_receives_only_blind_request(intervention: str) -> None:
    proposal = {
        "claim": "Adaptive scheduling improves utility by at least 5%.",
        "fingerprint": {
            "mechanism": "closed-loop load feedback",
            "intervention": intervention,
            "observables_and_data": "public load and utility measurements",
            "analysis_unit": "daily service cohort",
            "timescale": "four weeks",
            "system_boundary": "regional service",
        },
        "experiment_objective": "Compare utility against every mandatory baseline.",
        "data_needs": [
            {
                "kind": "public_api",
                "description": "Daily load and utility measurements",
            }
        ],
        "predicted_outcomes": [
            "The intervention clears the utility margin.",
            "The intervention misses at least one mandatory baseline.",
        ],
    }
    completion = _Completion(proposal)
    generator = CodexBlindDirectionGenerator(
        repo_root=REPO_ROOT,
        model="gpt-test",
        transport=completion,
    )
    request = {
        "goal_contract": {"contract_id": "contract_test"},
        "random_perspective": {"category_code": "cs.AI"},
    }

    result = generator.generate(request)

    draft = parse_direction_draft(result)
    assert draft.claim == proposal["claim"]
    assert draft.data_needs[0].description == "Daily load and utility measurements"
    sent = completion.requests[0]
    assert json.loads(sent.prompt.input) == request
    assert set(json.loads(sent.prompt.input)) == {
        "goal_contract",
        "random_perspective",
    }
    assert sent.cwd != REPO_ROOT
    assert sent.output_schema.name == "blind_direction_proposal.schema.json"
    assert sent.allow_local_tools is False


@pytest.mark.parametrize(
    ("claim", "intervention"),
    [
        ("Do not use global smoothing.", "avoid global smoothing"),
        (
            "Eliminating global smoothing improves utility.",
            "elimination of global smoothing",
        ),
        (
            "Leaving global smoothing off improves utility.",
            "global smoothing kept off",
        ),
        (
            "Omitting audit checks reduces latency.",
            "omission of audit checks",
        ),
        (
            "Excluding hard cases improves accuracy.",
            "hard-case exclusion",
        ),
        (
            "Withholding fallback improves utility.",
            "fallback withholding",
        ),
        ("Suppressing cache writes reduces latency.", "suppress cache writes"),
        ("Foregoing audit checks reduces latency.", "forego audit checks"),
        ("Ceasing retries reduces latency.", "cease retries"),
        ("Halting validation reduces latency.", "halt validation"),
        ("Blocking telemetry reduces latency.", "block telemetry"),
        ("Curtailing logging reduces latency.", "curtail logging"),
        ("Adding B does not improve A.", "add B"),
        ("Adding B fails to improve A.", "add B"),
        ("Adding B is ineffective at improving A.", "add B"),
        ("Adding B cannot improve A.", "add B"),
        ("Doing fewer audits reduces latency.", "fewer audit checks"),
        ("Dormant cache writes reduce latency.", "dormant cache writes"),
    ],
)
def test_codex_direction_generator_rejects_prohibition_as_direction(
    claim: str,
    intervention: str,
) -> None:
    completion = _Completion(
        {
            "claim": claim,
            "fingerprint": {
                "mechanism": "global smoothing bias",
                "intervention": intervention,
                "observables_and_data": "held-out utility",
                "analysis_unit": "evaluation cohort",
                "timescale": "four weeks",
                "system_boundary": "regional service",
            },
            "experiment_objective": "Compare utility against every baseline.",
            "data_needs": [],
            "predicted_outcomes": ["the bar clears", "the bar misses"],
        }
    )
    generator = CodexBlindDirectionGenerator(
        repo_root=REPO_ROOT,
        model="gpt-test",
        transport=completion,
    )

    with pytest.raises(BlindMcpAdapterError, match="positive intervention"):
        generator.generate(
            {
                "goal_contract": {"contract_id": "contract_test"},
                "random_perspective": {"category_code": "cs.AI"},
            }
        )


def test_structural_assessor_binds_model_relations_to_fingerprint_pair() -> None:
    relations = {
        "mechanism": "changed",
        "intervention": "changed",
        "observables_and_data": "equivalent",
        "analysis_unit": "equivalent",
        "timescale": "changed",
        "system_boundary": "equivalent",
    }
    completion = _Completion(relations)
    assessor = CodexStructuralEquivalenceAssessor(
        repo_root=REPO_ROOT,
        model="gpt-test",
        transport=completion,
    )
    candidate = _fingerprint("candidate")
    reference = _fingerprint("reference")

    result = assessor.assess(candidate, reference)

    assert result.candidate_fingerprint_id == candidate.fingerprint_id
    assert result.reference_fingerprint_id == reference.fingerprint_id
    assert result.changed_axes == {"mechanism", "intervention", "timescale"}
    assert json.loads(completion.requests[0].prompt.input) == {
        "candidate": {
            "fingerprint_id": candidate.fingerprint_id,
            "mechanism": candidate.mechanism,
            "intervention": candidate.intervention,
            "observables_and_data": candidate.observables_and_data,
            "analysis_unit": candidate.analysis_unit,
            "timescale": candidate.timescale,
            "system_boundary": candidate.system_boundary,
        },
        "reference": {
            "fingerprint_id": reference.fingerprint_id,
            "mechanism": reference.mechanism,
            "intervention": reference.intervention,
            "observables_and_data": reference.observables_and_data,
            "analysis_unit": reference.analysis_unit,
            "timescale": reference.timescale,
            "system_boundary": reference.system_boundary,
        },
    }
    assert completion.requests[0].allow_local_tools is False


def test_settings_credential_provider_resolves_only_named_environment_secret() -> None:
    provider = SettingsCredentialProvider(
        {
            "acquisition": {
                "credential_profiles": {
                    "official": {
                        "header": "Authorization",
                        "env": "OFFICIAL_API_TOKEN",
                    }
                }
            }
        },
        environ={"OFFICIAL_API_TOKEN": "Bearer secret"},
    )

    assert provider.headers_for("official") == (
        ("Authorization", "Bearer secret"),
    )
    with pytest.raises(CredentialProfileUnavailable, match="not configured"):
        provider.headers_for("missing")


def test_acquisition_payload_resolves_pinned_registered_and_public_sources(
    tmp_path: Path,
) -> None:
    registered = tmp_path / "registered.csv"
    registered.write_text("day,value\n1,7\n", encoding="utf-8")
    content = registered.read_bytes()
    content_digest = hashlib.sha256(content).hexdigest()
    thread_dir = tmp_path / "thread"
    production = thread_dir / "production"
    production.mkdir(parents=True)
    snapshot_id = "as_" + "1" * 64
    (production / "adapter_snapshots.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshots": [
                    {
                        "snapshot_id": snapshot_id,
                        "adapter_id": "events",
                        "materializer_type": "raw_data",
                        "role": "evaluation",
                        "source": str(registered),
                        "provenance": "operator fixture",
                        "source_scope": "operator",
                        "content_sha256": content_digest,
                        "size_bytes": len(content),
                        "entry_count": 1,
                    }
                ],
                "problems": [],
            }
        ),
        encoding="utf-8",
    )
    direction = make_direction_draft(
        claim="The intervention improves utility.",
        fingerprint=_fingerprint("active"),
        experiment_objective="Measure the effect.",
        data_needs=(
            DataNeed(kind="registered_adapter", description="Local events"),
            DataNeed(kind="public_page", description="Public benchmark"),
        ),
        predicted_outcomes=("effect clears", "effect misses"),
    )
    direction_path = tmp_path / "direction.json"
    direction_path.write_text(
        json.dumps(serialize_direction_draft(direction)),
        encoding="utf-8",
    )
    attempt = DirectionAttemptRef(
        attempt_id="attempt_0",
        direction_id=direction.direction_id,
        fingerprint=direction.fingerprint,
        ordinal=0,
    )
    reservation = make_acquisition_reservation(
        expected_revision=0,
        request_digest="sha256:" + "a" * 64,
    )
    state = ReorientationState(
        version=2,
        contract_id="contract_" + "b" * 64,
        revision=1,
        closed_attempts=(),
        phase=AcquisitionReserved(active_attempt=attempt, reservation=reservation),
    )
    engine = SimpleNamespace(
        read_state=lambda: state,
        paths=SimpleNamespace(direction=lambda _direction_id: direction_path),
    )

    command = build_acquisition_command(
        engine,
        {
            "needs": [
                {
                    "need_index": 0,
                    "candidates": [
                        {"kind": "registered_adapter", "adapter_id": "events"}
                    ],
                },
                {
                    "need_index": 1,
                    "candidates": [
                        {
                            "kind": "public_page",
                            "uri": "https://example.com/data",
                            "license_evidence": "public dataset notice",
                        }
                    ],
                },
            ],
            "budget": {
                "max_requests": 4,
                "max_download_bytes": 4096,
                "max_wall_seconds": 30,
            },
        },
        thread_dir=thread_dir,
    )

    assert isinstance(command.needs[0].candidates[0], RegisteredSource)
    assert command.needs[0].candidates[0].snapshot_id == snapshot_id
    assert isinstance(command.needs[1].candidates[0], PublicSource)
    assert command.needs[1].candidates[0].uri == "https://example.com/data"
    assert command.budget.max_requests == 4

    with pytest.raises(BlindMcpAdapterError, match="cannot contain credentials"):
        build_acquisition_command(
            engine,
            {
                "needs": [
                    {
                        "need_index": 0,
                        "candidates": [
                            {"kind": "registered_adapter", "adapter_id": "events"}
                        ],
                    },
                    {
                        "need_index": 1,
                        "candidates": [
                            {
                                "kind": "public_page",
                                "uri": "https://token@example.com/data",
                            }
                        ],
                    },
                ]
            },
            thread_dir=thread_dir,
        )


def test_acquisition_payload_rejects_unbounded_cycle_budget(tmp_path: Path) -> None:
    direction = make_direction_draft(
        claim="The intervention improves utility.",
        fingerprint=_fingerprint("active"),
        experiment_objective="Measure the effect.",
        predicted_outcomes=("effect clears", "effect misses"),
    )
    direction_path = tmp_path / "direction.json"
    direction_path.write_text(
        json.dumps(serialize_direction_draft(direction)),
        encoding="utf-8",
    )
    attempt = DirectionAttemptRef(
        attempt_id="attempt_0",
        direction_id=direction.direction_id,
        fingerprint=direction.fingerprint,
        ordinal=0,
    )
    state = ReorientationState(
        version=2,
        contract_id="contract_" + "b" * 64,
        revision=1,
        closed_attempts=(),
        phase=AcquisitionReserved(
            active_attempt=attempt,
            reservation=make_acquisition_reservation(
                expected_revision=0,
                request_digest="sha256:" + "a" * 64,
            ),
        ),
    )
    engine = SimpleNamespace(
        read_state=lambda: state,
        paths=SimpleNamespace(direction=lambda _direction_id: direction_path),
    )

    with pytest.raises(BlindMcpAdapterError, match="max_wall_seconds"):
        build_acquisition_command(
            engine,
            {
                "needs": [],
                "budget": {"max_wall_seconds": 301},
            },
            thread_dir=tmp_path / "thread",
        )
