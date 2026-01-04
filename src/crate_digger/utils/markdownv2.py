from crate_digger.constants import MARKDOWN_V2_ESCAPE_CHARS


def bold(text: str) -> str:
    """Format text as bold in MarkdownV2.

    Args:
        text: Text to format

    Returns:
        Text wrapped in asterisks
    """
    return f"*{text}*"


def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2 formatting.

    Args:
        text: Text to escape

    Returns:
        Text with special characters escaped
    """
    return "".join(f"\\{c}" if c in MARKDOWN_V2_ESCAPE_CHARS else c for c in text)
