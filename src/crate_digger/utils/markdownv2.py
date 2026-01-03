from crate_digger.constants import MARKDOWN_V2_ESCAPE_CHARS


def bold(text: str) -> str:
    return f"*{text}*"


def underline(text: str) -> str:
    return f"__{text}__"


def escape_markdown_v2(text: str) -> str:
    return ''.join(f"\\{c}" if c in MARKDOWN_V2_ESCAPE_CHARS else c for c in text)
