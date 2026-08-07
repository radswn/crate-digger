import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping


PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)


@dataclass(frozen=True)
class NormalizedLabel:
    raw_name: str
    normalized_name: str
    display_name: str
    alias_applied: bool


def normalize_label_name(
    raw_name: str,
    aliases: Mapping[str, str] | None = None,
) -> NormalizedLabel:
    """Normalize conservatively and apply only explicitly configured aliases."""

    raw = " ".join(raw_name.split()).strip()
    if not raw:
        raise ValueError("Label name cannot be empty")
    normalized_aliases = {
        _basic_normalize(source): " ".join(destination.split()).strip()
        for source, destination in (aliases or {}).items()
        if source.strip() and destination.strip()
    }
    basic = _basic_normalize(raw)
    canonical = normalized_aliases.get(basic)
    if canonical is None:
        return NormalizedLabel(
            raw_name=raw,
            normalized_name=basic,
            display_name=raw,
            alias_applied=False,
        )
    return NormalizedLabel(
        raw_name=raw,
        normalized_name=_basic_normalize(canonical),
        display_name=canonical,
        alias_applied=True,
    )


def normalize_entity_name(value: str) -> str:
    return _basic_normalize(value)


def _basic_normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = PUNCTUATION_RE.sub(" ", value)
    return " ".join(value.split())
