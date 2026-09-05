"""Connect the reviewed manuscript to a reproducible venue submission package."""
from __future__ import annotations

from pathlib import Path
import shutil
import zipfile
from typing import Any

from research_harness.confirmation_sampling import _hash
from research_harness.orchestrator.research_control import _read, _write
from research_harness.publishing.integrity import json_digest
from research_harness.publishing.review_runner import replay_scientific_reviews, run_scientific_reviews
from research_harness.publishing.venue_export import VenueTarget, export_venue_package, supported_targets, load_venue_profiles
from research_harness.workers.workspace import ensure_path_inside


def verify_submission(publication: Path) -> bool:
    try:
        receipt = _read(publication / 'submission_receipt.json')
        target = _read(publication.parent / 'submission_target.json')
        if target and receipt.get('target') != {key: target[key] for key in ('venue', 'year', 'track')}:
            return False
        if receipt.get('kind') != 'reviewed_submission_package':
            return False
        if (_hash(publication / 'paper.html') != receipt['manuscript_sha256']
                or _hash(publication / '_drafts/evidence_ledger.json') != receipt['ledger_sha256']):
            return False
        review_root = publication / receipt['review_directory']
        ensure_path_inside(review_root, publication / 'submission_reviews', 'submission reviews')
        if (_hash(review_root / 'paper.html') != receipt['manuscript_sha256']
                or _hash(review_root / '_drafts/evidence_ledger.json') != receipt['ledger_sha256']):
            return False
        if any(receipt[key] not in receipt['files'] for key in ('pdf_path', 'archive_path')):
            return False
        reviewed = replay_scientific_reviews(review_root)['readiness']
        if not reviewed['ready'] or json_digest(reviewed) != receipt['readiness_sha256']:
            return False
        for relative, digest in receipt['files'].items():
            path = publication / relative
            ensure_path_inside(path, publication, 'submission artifact')
            if _hash(path) != digest:
                return False
        return bool(receipt['files'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def finalize_submission(repo: Path, publication: Path, *, target: VenueTarget,
                        worker_report: dict[str, Any], model: str) -> dict[str, Any]:
    if target.profile_key not in supported_targets():
        raise ValueError('Unsupported submission target')
    draft = publication / '_drafts'
    outline = _read(draft / 'outline.json')
    sections = {path.stem: _read(path) for path in (draft / 'sections').glob('*.json')}
    profile = load_venue_profiles()[target.profile_key]
    if profile.get('requires_ai_use_statement') and (
            'ai_use_statement' not in {item['section_id'] for item in outline['section_outline']}
            or not sections.get('ai_use_statement', {}).get('prose_html')):
        raise ValueError('Include an authored ai_use_statement in the outline and rendered manuscript before submission review')
    draft_hashes = {str(path.relative_to(publication)): _hash(path) for path in draft.rglob('*.json')}
    manuscript_hash = _hash(publication / 'paper.html')
    ledger_hash = _hash(publication / '_drafts/evidence_ledger.json')
    prior = _read(publication / 'submission_receipt.json')
    target_record = {'venue': target.venue, 'year': target.year, 'track': target.track}
    if prior.get('target') == target_record and verify_submission(publication):
        return {'status': 'completed', 'receipt': prior}
    review_root = publication / 'submission_reviews' / json_digest([manuscript_hash, ledger_hash])
    if not review_root.exists():
        (review_root / '_drafts').mkdir(parents=True)
        shutil.copyfile(publication / 'paper.html', review_root / 'paper.html')
        shutil.copyfile(publication / '_drafts/evidence_ledger.json', review_root / '_drafts/evidence_ledger.json')
    if all((review_root / 'scientific_reviews' / f'review-{index}/run_receipt.json').exists() for index in (1, 2)):
        reviews = replay_scientific_reviews(review_root)
    else:
        reviews = run_scientific_reviews(repo, review_root, model)
    status = {'status': 'reviewed' if reviews['readiness']['ready'] else 'revision_required',
              'review_directory': str(review_root.relative_to(publication)),
              'readiness': reviews['readiness'],
              'reviews': reviews['assess_readiness_args']['review_records']}
    _write(publication / 'submission_status.json', status)
    if not reviews['readiness']['ready']:
        return {**status, 'next_tool_to_call': 'prepare_paper_writing_context'}
    ledger = _read(draft / 'evidence_ledger.json')
    body = [{**sections[item['section_id']], 'title': item['title']}
            for item in outline['section_outline']
            if item['section_id'] not in {'abstract', 'references', 'supplementary', 'impact_statement', 'ai_use_statement'}]
    tables = {}
    for table in outline.get('table_specs', []):
        source = table['data_source'].removeprefix('worker_report.')
        if source not in {'metrics', 'baselines'} or not isinstance(worker_report.get(source), dict):
            raise ValueError('Submission tables currently require worker_report.metrics or worker_report.baselines')
        tables[table['table_id']] = {'caption': table['title'], 'rows': [
            {'label': key, 'source': source, 'key': key} for key in worker_report[source]]}
    output = publication / 'submission' / target.profile_key / json_digest([manuscript_hash, ledger_hash])
    exported = export_venue_package(
        target=target, title=outline['title'], abstract=sections['abstract']['prose_html'], sections=body,
        bibliography=[item['source'] for item in ledger['citations'].values()],
        appendix_sections=[sections['supplementary']] if 'supplementary' in sections else [],
        impact_statement=sections.get('impact_statement', {}).get('prose_html'),
        ai_use_statement=sections.get('ai_use_statement', {}).get('prose_html'),
        figure_files={key: publication / 'figures' / f'{key}.png' for key in _read(draft / 'figures.json')},
        worker_report=worker_report, table_specs=tables, output_dir=output,
    )
    if _hash(publication / 'paper.html') != manuscript_hash or _hash(draft / 'evidence_ledger.json') != ledger_hash:
        raise ValueError('Manuscript changed during submission preparation')
    if {str(path.relative_to(publication)): _hash(path) for path in draft.rglob('*.json')} != draft_hashes:
        raise ValueError('Manuscript source changed during submission preparation')
    archive = output / 'submission.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(output.rglob('*')):
            if path.relative_to(output).parts[0] in {'_official_template', '_template_cache'}:
                continue
            if path.is_file() and path != archive and path.suffix in {'.tex', '.bib', '.bst', '.sty', '.cls', '.pdf', '.png', '.jpg'}:
                handle.write(path, str(path.relative_to(output)))
    files = {str(path.relative_to(publication)): _hash(path) for path in output.rglob('*') if path.is_file()}
    receipt = {'kind': 'reviewed_submission_package', 'target': target_record,
               'manuscript_sha256': manuscript_hash, 'ledger_sha256': ledger_hash,
               'review_directory': str(review_root.relative_to(publication)),
               'readiness_sha256': json_digest(reviews['readiness']), 'files': {**files, **draft_hashes},
               'pdf_path': str((output / 'paper.pdf').relative_to(publication)),
               'archive_path': str(archive.relative_to(publication)),
               'venue_export': exported,
               'scope': 'Compiled anonymous venue package with evidence-bound independent review; no conference acceptance or external submission is implied.'}
    _write(publication / 'submission_receipt.json', receipt)
    return {'status': 'completed', 'receipt': receipt}
