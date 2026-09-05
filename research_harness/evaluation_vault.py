"""Storage excluded from development agents and experiment subprocesses."""
from pathlib import Path
import hashlib
import json


def evaluation_vault_root() -> Path:
    return Path.home() / '.local/share/research-harness/evaluation-vault'


def ensure_evaluation_vault() -> Path:
    root = evaluation_vault_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root.resolve()


def reject_private_evaluation_input(path: Path) -> None:
    source = path.expanduser().resolve()
    vault = evaluation_vault_root().resolve()
    if source.is_relative_to(vault) or vault.is_relative_to(source):
        raise ValueError('Private evaluation storage cannot be a development input.')


def sealed_bank_metadata(thread: Path) -> dict | None:
    path = thread / 'production/evaluation_bank.json'
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    private = evaluation_vault_root() / thread.name / 'bank.json'
    digest = hashlib.sha256(private.read_bytes()).hexdigest()
    if (record['thread_id'] != thread.name or record['private_sha256'] != digest
            or record['bank_id'] != 'bank_' + digest):
        raise ValueError('Sealed evaluation bank does not match its public commitment')
    bundle = Path(record['game_bundle']).resolve()
    manifest_bytes = (bundle / 'input_manifest.json').read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != record['input_manifest_sha256']:
        raise ValueError('Sealed bank game manifest changed')
    for name, expected in json.loads(manifest_bytes)['files'].items():
        source = (bundle / name).resolve()
        source.relative_to(bundle)
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError('Sealed bank game source changed')
    provenance_path = thread / 'production/evaluation_sampling_provenance/receipt.json'
    provenance = json.loads(provenance_path.read_text()) if provenance_path.exists() else None
    if provenance:
        if (provenance['bank_id'] != record['bank_id']
                or hashlib.sha256(Path(provenance['sampler_source']).read_bytes()).hexdigest() != provenance['sampler_sha256']):
            raise ValueError('Evaluation sampling provenance does not match its source or bank')
    return {**record, 'integrity_verified': True, 'sampling_provenance': provenance}
