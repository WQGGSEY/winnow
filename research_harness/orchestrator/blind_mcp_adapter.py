from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Protocol

from research_harness.acquisition import (
    AcquisitionBudget,
    AcquisitionCommand,
    AcquisitionContractError,
    NeedPlan,
    PublicAcquisition,
    PublicSource,
    RegisteredSource,
    make_acquisition_command,
)
from research_harness.acquisition.live_http import CredentialProfileUnavailable
from research_harness.adapters.codex_cli import CodexCliAdapter
from research_harness.agent_runtime import AgentPrompt, CompletionRequest, CompletionResult
from research_harness.config import resolve_agent_model
from research_harness.data_adapters import (
    AdapterError,
    load_thread_adapter_snapshots,
)
from research_harness.orchestrator.blind_reorientation import (
    AcquisitionReserved,
    Checkpointed,
    HardExternalBlock,
)
from research_harness.orchestrator.blind_sequential_research import (
    BlindSequentialResearch,
)
from research_harness.orchestrator.direction_generation import (
    DataNeed,
    DirectionFingerprint,
    FINGERPRINT_AXES,
    StructuralEquivalenceAssessment,
    make_direction_draft,
    make_direction_fingerprint,
    make_structural_equivalence_assessment,
    parse_direction_draft,
    serialize_direction_draft,
    serialize_direction_fingerprint,
)
from research_harness.schemas.validator import validate_named_schema


class BlindMcpAdapterError(ValueError):
    pass


class CompletionTransport(Protocol):
    def complete(self, request: CompletionRequest) -> CompletionResult: ...


WriterLock = Callable[[], AbstractContextManager[None]]
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PUBLIC_SOURCE_KEYS = frozenset(
    {
        "kind",
        "uri",
        "credential_profile_name",
        "license_evidence",
        "crawl_max_pages",
        "crawl_max_depth",
        "crawl_max_total_bytes",
    }
)
_MAX_REQUESTS = 100
_MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
_MAX_WALL_SECONDS = 300
_NEGATIVE_FRAMING = re.compile(
    r"(?:\bdo(?:es)? not\b|\bdon't\b|\bdoesn't\b|\bcannot\b|\bcan't\b|"
    r"\bfails? to\b|\bineffective\b|\bno improvement\b|\bavoid\w*\b|"
    r"\bnever\b|\bban\w*\b|"
    r"\bprohibit\w*\b|\brefrain from\b|\bstop(?: using)?\b|\bwithout\b|"
    r"\bremov\w*\b|\beliminat\w*\b|\bdisabl\w*\b|\bomitt?\w*\b|"
    r"\bexclud\w*\b|\bwithhold\w*\b|\bkept? off\b|\binaction\b|"
    r"\babsence\b|\bsuppress\w*\b|\bforego\w*\b|\bcease\w*\b|"
    r"\bhalt\w*\b|\bblock\w*\b|\bcurtail\w*\b|하지 (?:말|않)|금지|"
    r"배제|제거|삭제|생략|제외|보류|중단|차단|억제|축소|비활성|없애|"
    r"사용하지 않|효과가? 없|개선하지 못|향상시키지 못)",
    re.IGNORECASE,
)
_EFFECT_CLAIM = re.compile(
    r"(?:improv|increase|decrease|reduce|outperform|beat|raise|lower|achiev|"
    r"cause|yield|enable|effective|prevent|향상|개선|증가|감소|낮추|높이|"
    r"능가|효과|달성|유도|가능)",
    re.IGNORECASE,
)
_POSITIVE_INTERVENTION = re.compile(
    r"(?:\badd\w*\b|\bappl\w*\b|\bintroduc\w*\b|\bimplement\w*\b|"
    r"\bdeploy\w*\b|\buse\w*\b|\bactivat\w*\b|\benabl\w*\b|"
    r"\btrain\w*\b|\boptimi[sz]\w*\b|\badapt\w*\b|\bschedul\w*\b|"
    r"\brout\w*\b|\ballocat\w*\b|\baugment\w*\b|\bcombin\w*\b|"
    r"\bcondition\w*\b|\bcalibrat\w*\b|\breweight\w*\b|"
    r"\bregulari[sz]\w*\b|\bensembl\w*\b|\bprioriti[sz]\w*\b|"
    r"\binstrument\w*\b|\bmonitor\w*\b|\bsampl\w*\b|\bsynthe\w*\b|"
    r"\bencod\w*\b|\bindex\w*\b|\bretriev\w*\b|\bcoordinat\w*\b|"
    r"\bcontrol\w*\b|\blearn\w*\b|\bestimat\w*\b|\bpredict\w*\b|"
    r"추가|적용|도입|구현|배포|사용|활성|학습|최적화|적응|스케줄|"
    r"라우팅|할당|증강|결합|조건|보정|재가중|정규화|앙상블|우선순위|"
    r"계측|모니터링|샘플링|합성|인코딩|색인|검색|조정|제어|추정|예측)",
    re.IGNORECASE,
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _json_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BlindMcpAdapterError(f"{label} did not return JSON") from exc
    if not isinstance(value, dict):
        raise BlindMcpAdapterError(f"{label} must return a JSON object")
    return value


class CodexBlindDirectionGenerator:
    def __init__(
        self,
        *,
        repo_root: Path,
        model: str,
        transport: CompletionTransport | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        self._repo_root = repo_root
        self._model = model
        self._transport = transport or CodexCliAdapter()
        self._timeout_seconds = timeout_seconds

    def generate(self, request: Mapping[str, object], /) -> Mapping[str, object]:
        if set(request) != {"solution_contract", "random_perspective"}:
            raise BlindMcpAdapterError(
                "blind generation request must contain only contract and perspective"
            )
        with tempfile.TemporaryDirectory(prefix="research-harness-direction-") as raw:
            result = self._transport.complete(
                CompletionRequest(
                    prompt=_direction_prompt(request),
                    model=self._model,
                    timeout_seconds=self._timeout_seconds,
                    output_schema=(
                        self._repo_root
                        / "research_harness"
                        / "schemas"
                        / "blind_direction_proposal.schema.json"
                    ),
                    cwd=Path(raw),
                    label="blind direction generator",
                    allow_local_tools=False,
                )
            )
        proposal = _json_object(result.text, "blind direction generator")
        validate_named_schema("blind_direction_proposal", proposal)
        _validate_positive_direction(proposal)
        fingerprint_raw = proposal["fingerprint"]
        fingerprint = make_direction_fingerprint(
            **{axis: fingerprint_raw[axis] for axis in FINGERPRINT_AXES}
        )
        draft = make_direction_draft(
            claim=proposal["claim"],
            fingerprint=fingerprint,
            experiment_objective=proposal["experiment_objective"],
            data_needs=tuple(
                DataNeed(kind=item["kind"], description=item["description"])
                for item in proposal["data_needs"]
            ),
            predicted_outcomes=proposal["predicted_outcomes"],
        )
        return serialize_direction_draft(draft)


class CodexStructuralEquivalenceAssessor:
    def __init__(
        self,
        *,
        repo_root: Path,
        model: str,
        transport: CompletionTransport | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self._repo_root = repo_root
        self._model = model
        self._transport = transport or CodexCliAdapter()
        self._timeout_seconds = timeout_seconds

    def assess(
        self,
        candidate: DirectionFingerprint,
        reference: DirectionFingerprint,
        /,
    ) -> StructuralEquivalenceAssessment:
        payload = {
            "candidate": serialize_direction_fingerprint(candidate),
            "reference": serialize_direction_fingerprint(reference),
        }
        with tempfile.TemporaryDirectory(prefix="research-harness-assessor-") as raw:
            result = self._transport.complete(
                CompletionRequest(
                    prompt=_assessment_prompt(payload),
                    model=self._model,
                    timeout_seconds=self._timeout_seconds,
                    output_schema=(
                        self._repo_root
                        / "research_harness"
                        / "schemas"
                        / "structural_axis_relations.schema.json"
                    ),
                    cwd=Path(raw),
                    label="structural equivalence assessor",
                    allow_local_tools=False,
                )
            )
        relations = _json_object(result.text, "structural equivalence assessor")
        validate_named_schema("structural_axis_relations", relations)
        return make_structural_equivalence_assessment(
            candidate=candidate,
            reference=reference,
            evidence_source_digest=_digest(relations),
            **{axis: relations[axis] for axis in FINGERPRINT_AXES},
        )


def _direction_prompt(request: Mapping[str, object]) -> AgentPrompt:
    return AgentPrompt(
        instructions=(
            "Generate exactly one actionable, positive causal research direction. "
            "State an intervention B that could improve the target A, with a "
            "falsifiable experiment against every mandatory baseline. Do not "
            "substitute a prohibition, warning, restatement, or mere risk list for "
            "the intervention. Use only the supplied JSON. Do not inspect files, "
            "tools, environment variables, or external context. Return only the "
            "schema-conforming JSON object."
        ),
        input=_canonical_json(request),
    )


def _assessment_prompt(payload: Mapping[str, object]) -> AgentPrompt:
    return AgentPrompt(
        instructions=(
            "Compare the candidate and reference research directions by semantic "
            "structure. For each named axis return changed only when the causal or "
            "operational meaning materially differs; lexical paraphrases are "
            "equivalent. Use only the supplied JSON and return only the six schema "
            "fields."
        ),
        input=_canonical_json(payload),
    )


def _validate_positive_direction(proposal: Mapping[str, Any]) -> None:
    claim = proposal["claim"].strip()
    intervention = proposal["fingerprint"]["intervention"].strip()
    if _NEGATIVE_FRAMING.search(claim) or _NEGATIVE_FRAMING.search(
        intervention
    ):
        raise BlindMcpAdapterError(
            "direction must propose a positive intervention, not a prohibition"
        )
    if _POSITIVE_INTERVENTION.search(intervention) is None:
        raise BlindMcpAdapterError(
            "direction must name an active positive intervention operation"
        )
    if _EFFECT_CLAIM.search(claim) is None:
        raise BlindMcpAdapterError(
            "direction claim must state the intervention's expected effect"
        )


class SettingsCredentialProvider:
    def __init__(
        self,
        settings: Mapping[str, Any],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        acquisition = settings.get("acquisition", {})
        profiles = acquisition.get("credential_profiles", {}) if isinstance(
            acquisition, Mapping
        ) else {}
        if not isinstance(profiles, Mapping):
            raise BlindMcpAdapterError(
                "acquisition.credential_profiles must be an object"
            )
        self._profiles = profiles
        self._environ = environ if environ is not None else os.environ

    def headers_for(self, profile_name: str) -> tuple[tuple[str, str], ...]:
        raw = self._profiles.get(profile_name)
        if not isinstance(raw, Mapping) or set(raw) != {"header", "env"}:
            raise CredentialProfileUnavailable(
                f"credential profile {profile_name!r} is not configured"
            )
        header = raw["header"]
        env_name = raw["env"]
        if (
            not isinstance(header, str)
            or header.lower() not in {"authorization", "x-api-key"}
            or not isinstance(env_name, str)
            or _ENV_NAME.fullmatch(env_name) is None
        ):
            raise CredentialProfileUnavailable(
                f"credential profile {profile_name!r} is malformed"
            )
        value = self._environ.get(env_name)
        if not value:
            raise CredentialProfileUnavailable(
                f"credential environment variable {env_name!r} is unavailable"
            )
        return ((header, value),)


def build_blind_research_engine(
    *,
    repo_root: Path,
    thread_dir: Path,
    writer_lock: WriterLock,
    settings: Mapping[str, Any],
    direction_transport: CompletionTransport | None = None,
    assessment_transport: CompletionTransport | None = None,
) -> BlindSequentialResearch:
    direction_model = resolve_agent_model(settings, "direction_generator")
    assessor_model = resolve_agent_model(settings, "structural_assessor")
    return BlindSequentialResearch(
        repo_root=repo_root,
        thread_dir=thread_dir,
        writer_lock=writer_lock,
        direction_generator=CodexBlindDirectionGenerator(
            repo_root=repo_root,
            model=direction_model,
            transport=direction_transport,
        ),
        structural_assessor=CodexStructuralEquivalenceAssessor(
            repo_root=repo_root,
            model=assessor_model,
            transport=assessment_transport,
        ),
        acquisition=PublicAcquisition.live(
            thread_dir / "production" / "reorientation" / "acquisition_cache",
            credential_provider=SettingsCredentialProvider(settings),
        ),
        perspective_seed=int(
            hashlib.sha256(thread_dir.name.encode("utf-8")).hexdigest()[:16],
            16,
        ),
    )


def build_acquisition_command(
    engine: BlindSequentialResearch,
    payload: object,
    *,
    thread_dir: Path,
) -> AcquisitionCommand:
    if not isinstance(payload, Mapping):
        raise BlindMcpAdapterError("acquisition must be an object")
    if set(payload) - {"needs", "budget"} or "needs" not in payload:
        raise BlindMcpAdapterError("acquisition must contain needs and optional budget")
    state = engine.read_state()
    if state is None:
        raise BlindMcpAdapterError("reorientation state is missing")
    phase = state.phase
    if isinstance(phase, (Checkpointed, HardExternalBlock)):
        phase = phase.continuation
    if not isinstance(phase, AcquisitionReserved):
        raise BlindMcpAdapterError("research is not waiting for an acquisition plan")
    direction_path = engine.paths.direction(phase.active_attempt.direction_id)
    try:
        direction = parse_direction_draft(
            json.loads(direction_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise BlindMcpAdapterError("active direction artifact is invalid") from exc
    raw_needs = payload["needs"]
    if not isinstance(raw_needs, list) or len(raw_needs) != len(direction.data_needs):
        raise BlindMcpAdapterError(
            "acquisition needs must match every active direction data need"
        )
    snapshot_path = thread_dir / "production" / "adapter_snapshots.json"
    try:
        snapshots = (
            load_thread_adapter_snapshots(thread_dir)
            if snapshot_path.exists()
            else {"snapshots": []}
        )
    except AdapterError as exc:
        raise BlindMcpAdapterError("thread adapter snapshot is invalid") from exc
    snapshot_by_adapter = {
        item["adapter_id"]: item for item in snapshots["snapshots"]
    }
    plans: list[NeedPlan] = []
    for index, (raw_need, need) in enumerate(
        zip(raw_needs, direction.data_needs, strict=True)
    ):
        if (
            not isinstance(raw_need, Mapping)
            or set(raw_need) != {"need_index", "candidates"}
            or raw_need["need_index"] != index
            or not isinstance(raw_need["candidates"], list)
        ):
            raise BlindMcpAdapterError(
                "acquisition needs must use ordered indexes and candidate arrays"
            )
        candidates = tuple(
            _source_candidate(raw, snapshot_by_adapter)
            for raw in raw_need["candidates"]
        )
        plans.append(NeedPlan(need_index=index, need=need, candidates=candidates))
    budget = _acquisition_budget(payload.get("budget"))
    try:
        return make_acquisition_command(
            reservation_id=phase.reservation.reservation_id,
            node_id=f"n_blind_{direction.direction_id[10:]}",
            attempt_id=phase.active_attempt.attempt_id,
            direction=direction,
            needs=plans,
            budget=budget,
        )
    except AcquisitionContractError as exc:
        raise BlindMcpAdapterError(str(exc)) from exc


def _source_candidate(
    raw: object,
    snapshots: Mapping[str, Mapping[str, Any]],
) -> RegisteredSource | PublicSource:
    if not isinstance(raw, Mapping):
        raise BlindMcpAdapterError("acquisition candidate must be an object")
    kind = raw.get("kind")
    if kind == "registered_adapter":
        if set(raw) != {"kind", "adapter_id"}:
            raise BlindMcpAdapterError(
                "registered candidate requires only kind and adapter_id"
            )
        adapter_id = raw.get("adapter_id")
        snapshot = snapshots.get(adapter_id) if isinstance(adapter_id, str) else None
        if snapshot is None:
            raise BlindMcpAdapterError(
                f"registered adapter {adapter_id!r} is absent from the thread snapshot"
            )
        try:
            return RegisteredSource(
                adapter_id=snapshot["adapter_id"],
                snapshot_id=snapshot["snapshot_id"],
                path=snapshot["source"],
                content_sha256=f"sha256:{snapshot['content_sha256']}",
                size_bytes=snapshot["size_bytes"],
                entry_count=snapshot["entry_count"],
                provenance=snapshot["provenance"],
                retrieved_at=f"thread_snapshot:{snapshot['snapshot_id']}",
            )
        except (AcquisitionContractError, KeyError) as exc:
            raise BlindMcpAdapterError(
                f"registered adapter {adapter_id!r} has an invalid snapshot"
            ) from exc
    if kind not in {"public_api", "public_page", "crawl"}:
        raise BlindMcpAdapterError(f"unsupported acquisition source kind {kind!r}")
    if set(raw) - _PUBLIC_SOURCE_KEYS or "uri" not in raw:
        raise BlindMcpAdapterError("public acquisition candidate has an invalid shape")
    try:
        return PublicSource(
            kind=kind,
            uri=raw["uri"],
            credential_profile_name=raw.get("credential_profile_name"),
            license_evidence=raw.get("license_evidence"),
            crawl_max_pages=raw.get("crawl_max_pages", 25),
            crawl_max_depth=raw.get("crawl_max_depth", 1),
            crawl_max_total_bytes=raw.get(
                "crawl_max_total_bytes",
                50 * 1024 * 1024,
            ),
        )
    except AcquisitionContractError as exc:
        raise BlindMcpAdapterError(str(exc)) from exc


def _acquisition_budget(raw: object) -> AcquisitionBudget:
    if raw is None:
        values = {
            "max_requests": 25,
            "max_download_bytes": 50 * 1024 * 1024,
            "max_wall_seconds": 60,
        }
    elif isinstance(raw, Mapping) and set(raw) <= {
        "max_requests",
        "max_download_bytes",
        "max_wall_seconds",
    }:
        values = {
            "max_requests": raw.get("max_requests", 25),
            "max_download_bytes": raw.get(
                "max_download_bytes",
                50 * 1024 * 1024,
            ),
            "max_wall_seconds": raw.get("max_wall_seconds", 60),
        }
    else:
        raise BlindMcpAdapterError("acquisition budget has an invalid shape")
    bounds = {
        "max_requests": _MAX_REQUESTS,
        "max_download_bytes": _MAX_DOWNLOAD_BYTES,
        "max_wall_seconds": _MAX_WALL_SECONDS,
    }
    for key, maximum in bounds.items():
        value = values[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= maximum
        ):
            raise BlindMcpAdapterError(
                f"{key} must be an integer between 0 and {maximum}"
            )
    try:
        return AcquisitionBudget(**values)
    except AcquisitionContractError as exc:
        raise BlindMcpAdapterError(str(exc)) from exc


__all__ = [
    "BlindMcpAdapterError",
    "CodexBlindDirectionGenerator",
    "CodexStructuralEquivalenceAssessor",
    "SettingsCredentialProvider",
    "build_acquisition_command",
    "build_blind_research_engine",
]
