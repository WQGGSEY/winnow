"""Storage excluded from development agents and experiment subprocesses."""
from pathlib import Path


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
