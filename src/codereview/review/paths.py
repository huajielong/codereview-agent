"""Diff path normalization."""


def normalize_path(path: str | None) -> str | None:
    if not path:
        return None
    value = path.strip().strip("`\"'")
    for prefix in ("./", "a/", "b/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value


def resolve_path(file: str | None, valid_lines: dict[str, set[int]]) -> str | None:
    """Exact match first, then normalized fallback."""
    if not file:
        return None
    if file in valid_lines:
        return file
    normalized = normalize_path(file)
    for key in valid_lines:
        if normalize_path(key) == normalized:
            return key
    return None
