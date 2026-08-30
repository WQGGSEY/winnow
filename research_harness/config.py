from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when repository configuration violates harness policy."""


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:index]
    return line


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
    ):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        lines.append(line)
    return lines


def parse_simple_yaml(text: str) -> Any:
    """Parse the small YAML subset used by this scaffold.

    Supported features are nested maps, lists, inline scalar lists, quoted
    strings, booleans, nulls, ints, and floats. This intentionally avoids a
    runtime dependency while keeping config files readable.
    """

    lines = _logical_lines(text)

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return {}, index
        stripped = lines[index].strip()
        if _indent(lines[index]) < indent:
            return {}, index
        if stripped.startswith("- "):
            return parse_list(index, indent)
        return parse_map(index, indent)

    def parse_list(index: int, indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(lines):
            line = lines[index]
            if _indent(line) != indent or not line.strip().startswith("- "):
                break
            content = line.strip()[2:].strip()
            index += 1

            if content == "":
                value, index = parse_block(index, indent + 2)
                items.append(value)
                continue

            if ":" in content:
                key, raw_value = content.split(":", 1)
                item: dict[str, Any] = {}
                if raw_value.strip():
                    item[key.strip()] = _parse_scalar(raw_value.strip())
                else:
                    nested, index = parse_block(index, indent + 2)
                    item[key.strip()] = nested

                if index < len(lines) and _indent(lines[index]) > indent:
                    extra, index = parse_block(index, indent + 2)
                    if isinstance(extra, dict):
                        item.update(extra)
                items.append(item)
            else:
                items.append(_parse_scalar(content))
                if index < len(lines) and _indent(lines[index]) > indent:
                    _, index = parse_block(index, indent + 2)
        return items, index

    def parse_map(index: int, indent: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while index < len(lines):
            line = lines[index]
            current_indent = _indent(line)
            if current_indent < indent:
                break
            if current_indent > indent:
                break
            stripped = line.strip()
            if stripped.startswith("- "):
                break
            if ":" not in stripped:
                raise ConfigError(f"invalid YAML line: {line}")
            key, raw_value = stripped.split(":", 1)
            index += 1
            if raw_value.strip():
                mapping[key.strip()] = _parse_scalar(raw_value.strip())
            else:
                value, index = parse_block(index, indent + 2)
                mapping[key.strip()] = value
        return mapping, index

    parsed, final_index = parse_block(0, _indent(lines[0]) if lines else 0)
    if final_index != len(lines):
        raise ConfigError("YAML parser did not consume the full document")
    return parsed


def load_yaml(path: Path) -> Any:
    return parse_simple_yaml(path.read_text(encoding="utf-8"))


def split_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            raw_frontmatter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :]).lstrip("\n")
            parsed = parse_simple_yaml(raw_frontmatter) if raw_frontmatter.strip() else {}
            if not isinstance(parsed, dict):
                raise ConfigError(f"frontmatter must be a map: {path}")
            return parsed, body
    raise ConfigError(f"frontmatter was opened but not closed: {path}")


def load_settings(repo_root: Path) -> dict[str, Any]:
    settings = load_json(repo_root / "settings.json")
    if "framework_invariants" in settings:
        raise ConfigError("settings.json cannot override framework invariants")
    runtime = settings.get("runtime", {})
    auth = runtime.get("auth_policy", {})
    if auth.get("mode") == "subscription_oauth" and not auth.get(
        "require_no_anthropic_api_key"
    ):
        raise ConfigError("subscription_oauth requires require_no_anthropic_api_key=true")
    return settings


def load_harness_config(repo_root: Path) -> dict[str, Any]:
    config = load_yaml(repo_root / "configs" / "harness.yaml")
    if not isinstance(config, dict):
        raise ConfigError("harness config must be a map")
    if "harness_semantics" not in config:
        raise ConfigError("harness config requires harness_semantics")
    return config


def load_research_profile(repo_root: Path) -> dict[str, Any]:
    frontmatter, body = split_frontmatter(repo_root / "research_profile.md")
    if frontmatter.get("settings_override"):
        raise ConfigError("research_profile.md cannot override core settings/invariants")
    frontmatter["body"] = body
    return frontmatter


def load_lessons(repo_root: Path) -> dict[str, Any]:
    lessons = load_yaml(repo_root / "lessons.yaml")
    if not isinstance(lessons, dict):
        raise ConfigError("lessons.yaml must be a map")
    for lesson in lessons.get("active_lessons", []):
        text = lesson.get("text", "")
        if "\n" in text:
            raise ConfigError(f"lesson must be one line: {lesson.get('id')}")
    return lessons


def resolve_agent_model(settings: Mapping[str, Any], role: str) -> str:
    """Pick the Codex model name for an active agent role.

    Resolution order:
      1. settings.runtime.agent_models.<role>
      2. settings.runtime.agent_models.default
      3. settings.runtime.worker_backends.codex_live.model
      4. literal "gpt-5.6-sol" as a last-resort default.

    Accepts any ``Mapping`` so that ADR-0005 ``ResolvedSettings`` (which is
    a Mapping but not a dict subclass) flows through the same path as a
    raw ``settings`` dict from ``load_settings()``.
    """

    runtime = settings.get("runtime", {}) if isinstance(settings, Mapping) else {}
    agent_models = runtime.get("agent_models", {}) or {}
    if isinstance(agent_models, Mapping):
        explicit = agent_models.get(role)
        if isinstance(explicit, str) and explicit.strip():
            return explicit
        default = agent_models.get("default")
        if isinstance(default, str) and default.strip():
            return default
    live = (
        runtime.get("worker_backends", {}).get("codex_live", {})
        if isinstance(runtime.get("worker_backends"), Mapping)
        else {}
    )
    fallback = live.get("model") if isinstance(live, Mapping) else None
    if isinstance(fallback, str) and fallback.strip():
        return fallback
    return "gpt-5.6-sol"


def resolve_legacy_claude_agent_model(settings: Mapping[str, Any], role: str) -> str:
    """Pick a Claude model only for the dormant market, refiner, and lesson code."""

    runtime = settings.get("runtime", {}) if isinstance(settings, Mapping) else {}
    models = runtime.get("legacy_claude_agent_models", {}) or {}
    if isinstance(models, Mapping):
        explicit = models.get(role)
        if isinstance(explicit, str) and explicit.strip():
            return explicit
        default = models.get("default")
        if isinstance(default, str) and default.strip():
            return default
    return "claude-sonnet-4-6"


def resolve_agent_budget(settings: Mapping[str, Any], role: str) -> str:
    """Pick a Claude USD cap only for dormant legacy agent code.

    Resolution order:
      1. settings.runtime.legacy_claude_agent_budgets.<role>
      2. settings.runtime.legacy_claude_agent_budgets.default
      3. literal "0.25" as a last-resort default.

    Returns the value as a string because that's what the Claude CLI's
    ``--max-budget-usd`` flag expects. Values in settings may be either
    strings ("0.50") or numbers (0.5); both are coerced to str here so
    callers don't need to care.
    """

    runtime = settings.get("runtime", {}) if isinstance(settings, Mapping) else {}
    agent_budgets = runtime.get("legacy_claude_agent_budgets", {}) or {}
    if isinstance(agent_budgets, Mapping):
        explicit = agent_budgets.get(role)
        if explicit is not None and str(explicit).strip():
            return str(explicit)
        default = agent_budgets.get("default")
        if default is not None and str(default).strip():
            return str(default)
    return "0.25"


class _NotSet:  # sentinel: "value resolution couldn't extract a usable value"
    ...


_NOT_SET = _NotSet()


def resolve_agent_max_rounds(
    settings: Mapping[str, Any], role: str, *, fallback: int = 8
) -> int | None:
    """Pick the max_rounds cap for an agent role.

    Returns either a positive integer (the cap) or ``None`` (unlimited —
    the agent owns the DONE decision and the harness does no
    force-extract).

    Resolution order:
      1. settings.runtime.agent_max_rounds.<role>
      2. settings.runtime.agent_max_rounds.default
      3. the caller-supplied ``fallback`` (per-agent hard-coded default,
         e.g. 8 for grilling, 12 for refine)

    Recognised value shapes for the entries:
      - positive integer N → N
      - the string "unlimited" / "none" / "infinity" / "inf" (case-
        insensitive) → None
      - JSON null → None
      - anything else → raise ConfigError
    """

    def _coerce(value: Any, *, source: str) -> int | None | _NotSet:
        if value is None:
            return None  # explicit "unlimited"
        if isinstance(value, bool):
            # bool is a subclass of int — reject before the int branch
            raise ConfigError(
                f"{source}: max_rounds must be a positive integer or 'unlimited', got bool"
            )
        if isinstance(value, int):
            if value < 1:
                raise ConfigError(f"{source}: max_rounds must be >= 1, got {value}")
            return value
        if isinstance(value, str):
            stripped = value.strip().lower()
            if stripped in {"unlimited", "none", "infinity", "inf"}:
                return None
            try:
                n = int(stripped)
            except ValueError:
                raise ConfigError(
                    f"{source}: max_rounds string must be 'unlimited' or a "
                    f"positive integer, got {value!r}"
                ) from None
            if n < 1:
                raise ConfigError(f"{source}: max_rounds must be >= 1, got {n}")
            return n
        return _NOT_SET  # unknown shape — skip and try fallback

    runtime = settings.get("runtime", {}) if isinstance(settings, Mapping) else {}
    section = runtime.get("agent_max_rounds", {}) or {}
    if isinstance(section, Mapping):
        if role in section:
            result = _coerce(section[role], source=f"agent_max_rounds.{role}")
            if result is not _NOT_SET:
                return result
        if "default" in section:
            result = _coerce(section["default"], source="agent_max_rounds.default")
            if result is not _NOT_SET:
                return result
    return fallback


# ---------------------------------------------------------------------------
# Thread-aware wrappers (ADR 0005, Phase 2)
#
# These import lazily from settings_scoped to keep config.py free of any
# hard dependency on the scoped-settings system — callers that haven't
# migrated still call ``resolve_agent_*(settings, role)`` directly with
# the dict returned by ``load_settings()``.
# ---------------------------------------------------------------------------


def resolve_agent_model_for_thread(
    repo_root: Path, thread_id: str | None, role: str
) -> str:
    """Thread-aware variant of :func:`resolve_agent_model`.

    Constructs a ``ResolvedSettings`` for ``(repo_root, thread_id)`` and
    delegates. Falls back to project + operator scope when ``thread_id`` is
    None. Phase 4 will migrate the existing call sites onto this entrypoint.
    """
    from research_harness.settings_scoped import resolve_for_thread

    return resolve_agent_model(resolve_for_thread(repo_root, thread_id), role)


def resolve_agent_budget_for_thread(
    repo_root: Path, thread_id: str | None, role: str
) -> str:
    """Thread-aware variant of :func:`resolve_agent_budget`."""
    from research_harness.settings_scoped import resolve_for_thread

    return resolve_agent_budget(resolve_for_thread(repo_root, thread_id), role)


def resolve_agent_max_rounds_for_thread(
    repo_root: Path,
    thread_id: str | None,
    role: str,
    *,
    fallback: int = 8,
) -> int | None:
    """Thread-aware variant of :func:`resolve_agent_max_rounds`."""
    from research_harness.settings_scoped import resolve_for_thread

    return resolve_agent_max_rounds(
        resolve_for_thread(repo_root, thread_id), role, fallback=fallback
    )


PRACTITIONER_VOICE_MARKER = "# Practitioner Lens override"


def load_critic_profile(path: Path) -> dict[str, Any]:
    frontmatter, body = split_frontmatter(path)
    if frontmatter.get("override_policy") not in {None, "taste_only"}:
        raise ConfigError(f"critic override_policy must be taste_only: {path}")
    if not frontmatter.get("critic_profile_id"):
        raise ConfigError(f"critic requires critic_profile_id: {path}")
    # Practitioner-voice contract: a critic profile is only loadable if it
    # either declares persona_voice: practitioner in frontmatter or includes
    # an explicit "# Practitioner Lens override" section in its body that
    # acknowledges the so_what / next_actions contract from
    # critics/PRACTITIONER_PERSONA.md. This blocks silently re-introducing
    # purely-academic reviewers that would bypass the operator-decision gate.
    persona_voice = (frontmatter.get("persona_voice") or "").strip().lower()
    has_override_section = PRACTITIONER_VOICE_MARKER in body
    if persona_voice != "practitioner" and not has_override_section:
        raise ConfigError(
            "critic must inherit practitioner voice: set 'persona_voice: practitioner' "
            f"in frontmatter or include a '{PRACTITIONER_VOICE_MARKER}' section in the "
            f"body explaining how the so_what / next_actions contract maps to this "
            f"critic's specialty. Offender: {path}"
        )
    frontmatter["body"] = body
    frontmatter["path"] = str(path)
    return frontmatter
