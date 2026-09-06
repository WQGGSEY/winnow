"""Develop and critique prospective hypotheses before baseline qualification."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from research_harness.adapters.codex_cli import CodexCliAdapter
from research_harness.agent_runtime import research_model, model_reasoning_effort, AgentPrompt, CompletionRequest
from research_harness.memory.baseline_dossier import dossier_path, load_baseline_dossier
from research_harness.schemas.validator import load_schema, validate_named_schema


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def hypothesis_context(repo: Path, thread: Path) -> dict[str, Any]:
    from research_harness.runner.baseline_preflight import baseline_preparation_state
    from research_harness.orchestrator.research_knowledge import research_brief, brief_context

    connector = _read(thread / 'connector/connector_session.json')
    brief = _read(thread / 'market/market_research_brief.json')
    dossier = load_baseline_dossier(repo, brief['baseline_dossier_id']) if brief.get('baseline_dossier_id') else {}
    packets = [attempt['perspective_packet'] for attempt in connector.get('attempts', []) if attempt.get('perspective_packet')]
    sources = {}
    for packet in packets:
        sources[packet['packet_id']] = packet
    for source in dossier.get('source_index', []):
        sources[source['id']] = source
    envelope = _read(thread / 'production/feasibility_envelope.json')
    for source in envelope.get('data_sources_available', []):
        sources[source['id']] = {'evidence_kind': 'configured_resource_not_scientific_evidence', **source}
    reviews = sorted((thread / 'market/baseline_reviews').glob('*.review.json'), key=lambda path: path.stat().st_mtime)
    if reviews:
        sources['baseline_preparation_review'] = {'evidence_kind': 'implementation_review_not_paper_evidence', 'review': _read(reviews[-1])}
    preparation = baseline_preparation_state(thread)
    if preparation['attempt_count']:
        sources['baseline_preparation_runs'] = {'evidence_kind': 'execution_diagnostics_not_scientific_approval', **preparation}
    details = {}
    if dossier:
        base = dossier_path(repo, brief['baseline_dossier_id']).parent
        details = {candidate['id']: (base / candidate['detail_file']).read_text() for candidate in dossier['candidates_index']}
    return {
        'research_brief': brief_context(thread, research_brief(thread)),
        'research_question': _read(thread / 'thread.json').get('user_goal', ''),
        'problem_definition': _read(thread / 'grilling/grilling_session.json').get('extracted', {}),
        'connector_candidates': connector.get('claims', []),
        'sources': sources,
        'baseline_method_notes': details,
        'resources': {key: envelope.get(key) for key in ('data_sources_available', 'compute_budget', 'execution_constraints')},
    }


def _records(candidates: list[dict[str, Any]], critique: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    assessments = {row['candidate_index']: row for row in critique['assessments']} if critique else {}
    return [{
        'id': 'hypothesis_' + _digest(candidate)[:16], 'status': 'proposed',
        'claim_under_test': candidate['proposal']['claim'], 'candidate': candidate,
        'critique': assessments.get(index),
        'selected_for_diagnostic': bool(critique and critique['preferred_index'] == index and assessments[index]['ready_for_test']),
        'scientific_support': 'unverified',
    } for index, candidate in enumerate(candidates)]


def develop_hypotheses(repo: Path, thread: Path, *, revision_request: str = '', run_id: str | None = None, transport=None) -> dict[str, Any]:
    """Persist drafts immediately, critique alternatives, and revise once if needed.

    Readiness here means worth testing, never demonstrated truth or permission
    to bypass experiment/claim-validation gates.
    """
    root = thread / 'production/hypotheses'
    current = root / 'current.json'
    previous = _read(current)
    if run_id is None and previous.get('status') in {
        'developing', 'awaiting_critique', 'awaiting_revision', 'awaiting_revision_critique',
    }:
        run_id = previous['context_digest']
    if run_id is not None:
        if len(run_id) != 64 or any(c not in '0123456789abcdef' for c in run_id):
            raise ValueError('invalid hypothesis run_id')
        context = _read(root / run_id / 'context.json')
        if not context or _digest(context) != run_id:
            raise ValueError('hypothesis run_id has no matching frozen context')
        digest = run_id
    else:
        context = hypothesis_context(repo, thread)
        if revision_request:
            context['revision_request'] = revision_request
        digest = _digest(context)
    if not context['research_question']:
        raise ValueError('hypothesis development requires a research question')
    run = root / digest
    completed = _read(run / 'result.json')
    if completed:
        _write(current, completed)
        return completed
    _write(run / 'context.json', context)
    # Existing connector ideas become visible without waiting for a model call.
    seeds = [{
        'id': 'hypothesis_' + _digest(item)[:16], 'status': 'proposed',
        'claim_under_test': item['claim_contract']['claim_under_test'],
        'origin': 'connector', 'scientific_support': 'unverified',
        'candidate': item, 'selected_for_diagnostic': False,
    } for item in context['connector_candidates']]
    if not previous or previous.get('context_digest') != digest:
        _write(current, {'status': 'developing', 'context_digest': digest, 'candidates': seeds})
    client = transport or CodexCliAdapter()

    def complete(stage: str, schema: str, instructions: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = run / f'{stage}.json'
        if path.exists():
            return _read(path)['response']
        rejected = _read(run / f'{stage}.rejected.json')
        submitted = {'task': payload, 'previous_invalid_response': rejected} if rejected else payload
        output_schema = load_schema(schema)
        if schema == 'hypothesis_candidates':
            source_field = output_schema['properties']['candidates']['items']['properties']['source_ids']
            if context['sources']:
                source_field['items']['enum'] = sorted(context['sources'])
            else:
                source_field['maxItems'] = 0
        schema_path = run / f'{stage}.schema.json'
        _write(schema_path, output_schema)
        with tempfile.TemporaryDirectory(prefix='research-hypotheses-') as temporary:
            result = client.complete(CompletionRequest(
                prompt=AgentPrompt(instructions=instructions, input=json.dumps(submitted, ensure_ascii=False)),
                model=research_model(), timeout_seconds=240,
                output_schema=schema_path.resolve(),
                cwd=Path(temporary), label=f'hypothesis-{stage}', allow_local_tools=False,
            ))
        try:
            response = json.loads(result.text)
            validate_named_schema(schema, response)
        except ValueError as exc:
            _write(run / f'{stage}.rejected.json', {'raw_response': result.text, 'validation_error': str(exc)})
            raise
        if schema == 'hypothesis_candidates':
            if sum(candidate['explanation_role'] == 'null_or_confound' for candidate in response['candidates']) != 1:
                _write(run / f'{stage}.rejected.json', {'response': response, 'validation_error': 'Include exactly one null_or_confound explanation and two distinct mechanism explanations.'})
                raise ValueError('hypotheses must include one null or confound explanation')
            ceiling = (context['resources'].get('compute_budget') or {}).get('max_runner_seconds_per_node', 900)
            for candidate in response['candidates']:
                if set(candidate['source_ids']) - context['sources'].keys():
                    _write(run / f'{stage}.rejected.json', {'response': response, 'validation_error': 'Cite only source IDs in context.sources.'})
                    raise ValueError('hypothesis cites sources absent from the evidence packet')
                if candidate['test_budget_seconds'] > min(120, ceiling):
                    _write(run / f'{stage}.rejected.json', {'response': response, 'validation_error': f'Diagnostic budget must be <= {min(120, ceiling)} seconds.'})
                    raise ValueError('the first discriminating test must fit the diagnostic budget')
        else:
            if {item['candidate_index'] for item in response['assessments']} != {0, 1, 2}:
                _write(run / f'{stage}.rejected.json', {'response': response, 'validation_error': 'Assess each of candidate indices 0, 1, 2 exactly once.'})
                raise ValueError('critique must assess every candidate exactly once')
            if any(item['ready_for_test'] and item['required_changes'] for item in response['assessments']):
                _write(run / f'{stage}.rejected.json', {'response': response, 'validation_error': 'Resolve required changes or mark the candidate not ready for test.'})
                raise ValueError('test-ready candidates cannot have unresolved required changes')
        _write(path, {'model': research_model(), 'reasoning_effort': model_reasoning_effort(research_model()), 'instructions': instructions,
                      'input': submitted, 'response': response, 'usage': result.usage.as_dict(), 'thread_id': result.thread_id})
        return response

    generation = (
        'Develop exactly three prospective, causally distinct hypotheses from the supplied research question and evidence. '
        'Do not assume the user-suspected cause or observed phenomenon has been established. '
        'Treat connector analogies as inspiration, not empirical support. Distinguish source metadata from retrieved findings; '
        'never infer a paper method from its title. Cite only supplied source IDs; state missing evidence. '
        'Include exactly one strongest null/confound explanation, such as measurement, implementation or insufficient optimization, '
        'and two genuinely distinct domain mechanisms. Do not turn all three candidates into elaborate interventions for the assumed cause. '
        'Compare explanations rather than three parameter variants of one solution. For each, explain the causal chain, '
        'closest-work difference, critical assumptions, strongest competing explanation, and a small test whose outcomes distinguish them. '
        'The first test must take at most 120 seconds and must respect the supplied resources. It can test a necessary premise '
        'without a qualified baseline. Separate implementation sanity checks, learning sufficiency and the scientific mechanism. '
        'Do not prescribe a large training run before validating the relevant measurement/implementation premise. '
        'List prerequisites explicitly; do not pretend any experiments have run. A hypothesis is a prediction, not a supported claim. '
        'Return schema-conforming JSON.'
    )
    review_instructions = (
        'Independently critique all three prospective hypotheses using the supplied evidence. Assess causal specificity, '
        'genuine diversity, source fidelity, novelty uncertainty, resource feasibility, and whether each small test distinguishes '
        'its proposed mechanism from the strongest alternative. Check for circular diagnostics and confounded outcome predictions. '
        'These are untested hypotheses: do not require completed training, positive results or baseline qualification. '
        'ready_for_test means scientifically useful and executable once its explicit prerequisites are satisfied, not true. '
        'Set ready_for_test=true only when required_changes is empty. Put optional suggestions in reason, not required_changes. '
        'Give concrete required changes for weak candidates and select the most informative next diagnostic. Do not ask a human. '
        'Return schema-conforming JSON.'
    )
    def checkpoint(status: str, candidates: list[dict[str, Any]], critique=None):
        value = {'status': status, 'context_digest': digest, 'candidates': _records(candidates, critique),
                 'next_tool_to_call': 'develop_research_hypotheses',
                 'run_id': digest,
                 'next_step': 'Resume this run_id to advance the next independent stage. Its context and original revision request remain frozen until completion. These are unverified candidates.'}
        _write(current, value)
        return value

    had_draft = (run / 'draft.json').exists()
    draft = complete('draft', 'hypothesis_candidates', generation, context)
    if not had_draft:
        return checkpoint('awaiting_critique', draft['candidates'])
    had_critique = (run / 'critique.json').exists()
    critique = complete('critique', 'hypothesis_critique', review_instructions, {'context': context, **draft})
    if any(item['required_changes'] or not item['ready_for_test'] for item in critique['assessments']):
        if not had_critique:
            return checkpoint('awaiting_revision', draft['candidates'], critique)
        had_revision = (run / 'revision.json').exists()
        draft = complete('revision', 'hypothesis_candidates', generation + ' Address the critique concretely; retain valid ideas and revise deficient ones.',
                         {'context': context, 'draft': draft, 'critique': critique})
        if not had_revision:
            return checkpoint('awaiting_revision_critique', draft['candidates'])
        critique = complete('revision_critique', 'hypothesis_critique', review_instructions, {'context': context, **draft})
    records = _records(draft['candidates'], critique)
    result = {'status': 'ready_for_diagnostic' if any(row['selected_for_diagnostic'] for row in records) else 'needs_revision',
              'context_digest': digest, 'run_id': digest, 'candidates': records, 'selection_reason': critique['selection_reason'],
              'next_step': ('Run the selected small diagnostic and resolve its prerequisites; baseline qualification remains required for scientific comparison.'
                            if any(row['selected_for_diagnostic'] for row in records)
                            else 'Resolve the critique with source or diagnostic evidence, then call develop_research_hypotheses with a concrete revision_request. Do not treat these candidates as test-ready.')}
    _write(run / 'result.json', result)
    _write(current, result)
    return result
