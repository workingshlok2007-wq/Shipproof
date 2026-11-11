"""Builds a CodeIndex (paths + declared symbols + HTTP routes) from a GitHub tarball, entirely in memory."""

from __future__ import annotations

import io
import re
import tarfile
from pathlib import PurePosixPath

from shipproof.prompts import MAX_INDEX_ENTRIES, MAX_SYMBOLS_PER_FILE, CodeIndex, CodeIndexEntry

MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILES = 2000
MAX_SYMBOL_LENGTH = 256

SOURCE_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".java", ".kt", ".go", ".rb", ".rs",
    ".cs", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".scala",
})
SKIP_DIRS = frozenset({
    ".git", "node_modules", "vendor", "dist", "build", "out", "target", "venv", ".venv",
    "__pycache__", ".next", ".idea", ".vscode", "coverage",
})

_SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:final\s+)?(?:class|interface|struct|enum|trait)\s+([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", re.MULTILINE),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", re.MULTILINE),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.MULTILINE),
    re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)", re.MULTILINE),
)
_ROUTE_PATTERN = re.compile(r"\b\w+\.(get|post|put|patch|delete|websocket|route)\(\s*[\"'](/[^\"'\s]{0,200})[\"']", re.IGNORECASE)


class CodeIndexError(RuntimeError):
    pass


def extractSymbols(source: str) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for pattern in _SYMBOL_PATTERNS:
        for match in pattern.finditer(source):
            found[match.group(1)[:MAX_SYMBOL_LENGTH]] = None
    for match in _ROUTE_PATTERN.finditer(source):
        found[f"{match.group(1).upper()} {match.group(2)}"[:MAX_SYMBOL_LENGTH]] = None
    return tuple(found)[:MAX_SYMBOLS_PER_FILE]


def _repoRelativePath(memberName: str) -> str | None:
    """GitHub tarballs prefix every path with '<owner>-<repo>-<sha>/'. Rejects traversal and absolute paths."""
    parts = PurePosixPath(memberName).parts
    if len(parts) < 2 or memberName.startswith("/") or any(p in ("..", "") for p in parts):
        return None
    relative = parts[1:]
    if any(p in SKIP_DIRS for p in relative[:-1]):
        return None
    path = "/".join(relative)
    if any(ord(ch) < 0x20 for ch in path) or "\\" in path or len(path) > 512:
        return None
    return path


def buildCodeIndexFromTarball(data: bytes) -> tuple[CodeIndex, int]:
    """Returns (index, skippedCount). Nothing is written to disk. Symlinks, devices and oversize files are skipped."""
    entries: list[CodeIndexEntry] = []
    skipped = 0
    sourceFiles = 0
    totalBytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                path = _repoRelativePath(member.name)
                if path is None:
                    skipped += 1
                    continue
                if len(entries) >= MAX_INDEX_ENTRIES:
                    skipped += 1
                    continue
                symbols: tuple[str, ...] = ()
                if PurePosixPath(path).suffix.lower() in SOURCE_EXTENSIONS:
                    if member.size > MAX_FILE_BYTES or sourceFiles >= MAX_SOURCE_FILES or totalBytes + member.size > MAX_TOTAL_SOURCE_BYTES:
                        skipped += 1
                    else:
                        handle = archive.extractfile(member)
                        if handle is None:
                            skipped += 1
                        else:
                            content = handle.read(MAX_FILE_BYTES + 1)
                            totalBytes += len(content)
                            sourceFiles += 1
                            symbols = extractSymbols(content.decode("utf-8", errors="replace"))
                entries.append(CodeIndexEntry(path=path, symbols=symbols))
    except (tarfile.TarError, EOFError, OSError) as error:
        raise CodeIndexError(f"invalid repository archive: {error}") from error
    return CodeIndex(entries=tuple(entries)), skipped
