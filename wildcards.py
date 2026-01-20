#!/usr/bin/env python3
"""
Wildcard / prompt expander with:
  - __card__ expansion from text files under ./wildcards/
  - {a|b|c} choice groups (supports nesting)
  - {N$$sep$$a|b|c} / {N-M$$sep$$a|b|c} multi-pick groups
  - Per-option weights via trailing $N (e.g. "blue$3")
  - Wildcard history recall via __name[0]__ / __name[1]__ (most recent / previous)

Design goals vs the original version:
  - Deterministic RNG per call (no global random.seed side effects)
  - Robust parsing for nested braces (no regex-based brace parsing loops)
  - Efficient weighting (no list replication for weights)
  - Optional recursive wildcard directory loading and stable key naming
  - Better errors (file + line numbers), cycle detection for recursive wildcards

Notes on syntax:
  - __pattern__ uses fnmatch-style patterns (e.g. __colors__ or __animals/*__)
  - __name[0]__ recalls the most recent expanded result for 'name' (0-based index; [1] is the previous, etc.)
    Example: "__gname__ ... __gname[0]__" repeats the same generated name later in the prompt.
  - {a|b|c} chooses one option (weights supported: {a$1|b$3})
  - {2$$, $$a|b|c} chooses 2 distinct options and joins with ", "
  - {2-4$$ / $$a|b|c} chooses a random number between 2 and 4 (inclusive)

Escapes:
  - If allow_backslash_escapes is enabled (default), you may write \{ \} \| to keep literal characters
"""

from __future__ import annotations

import argparse
import fnmatch
import logging
import random
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import chardet  # type: ignore
except Exception:  # pragma: no cover
    chardet = None  # type: ignore

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------
# BracketValidator
# ------------------------------------------------------------------------------
class BracketValidator:
    """
    Validates balanced brackets for {}, [], (), <>, and symmetric '__' markers.

    This is used primarily to catch malformed wildcard files early. You can optionally
    also validate the input prompt before expansion.
    """

    PAIRS = {"{": "}", "[": "]", "(": ")", "<": ">"}
    CLOSE_TO_OPEN = {v: k for k, v in PAIRS.items()}
    SYM = "__"

    @staticmethod
    def validate(text: str) -> bool:
        stack: List[str] = []
        i = 0

        while i < len(text):
            # Symmetric multi-char token: '__'
            if text.startswith(BracketValidator.SYM, i):
                if stack and stack[-1] == BracketValidator.SYM:
                    stack.pop()
                else:
                    stack.append(BracketValidator.SYM)
                i += 2
                continue

            ch = text[i]
            if ch in BracketValidator.PAIRS:
                stack.append(ch)
            elif ch in BracketValidator.CLOSE_TO_OPEN:
                if not stack:
                    return False
                top = stack.pop()
                if top == BracketValidator.SYM:
                    return False
                if BracketValidator.PAIRS.get(top) != ch:
                    return False
            i += 1

        return not stack

    @staticmethod
    def validate_or_raise(text: str, *, context: str = "") -> None:
        if not BracketValidator.validate(text):
            msg = "Invalid bracket structure"
            if context:
                msg += f" ({context})"
            msg += f": {text!r}"
            raise ValueError(msg)


# ------------------------------------------------------------------------------
# WeightedList
# ------------------------------------------------------------------------------
@dataclass
class WeightedList:
    items: List[str]
    weights: List[int]

    def __post_init__(self) -> None:
        if len(self.items) != len(self.weights):
            raise ValueError("WeightedList items/weights length mismatch")
        for w in self.weights:
            if not isinstance(w, int) or w <= 0:
                raise ValueError(f"Invalid weight: {w}")

    def choose_one(self, rng: random.Random) -> str:
        return rng.choices(self.items, weights=self.weights, k=1)[0]


# ------------------------------------------------------------------------------
# WildcardLibrary
# ------------------------------------------------------------------------------
class WildcardLibrary:
    """
    Loads wildcard text files from a directory.

    - Each .txt file becomes a key.
    - Keys are loaded as:
        (1) relative path without suffix (e.g. 'animals/birds'), always
        (2) stem-only key (e.g. 'birds') only if unambiguous across the directory tree
    - Each non-empty, non-comment line is an entry.
    - Trailing '$N' sets that line's weight.
    """

    _WEIGHT_RE = re.compile(r"^(.*?)(?:\$(\d+))?$")

    def __init__(self, wildcard_dir: Path, *, recursive: bool = True) -> None:
        self.wildcard_dir = Path(wildcard_dir)
        self.recursive = recursive

        self._loaded = False
        self._lock = threading.RLock()
        self._cards: Dict[str, WeightedList] = {}

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def cards(self) -> Dict[str, WeightedList]:
        return self._cards

    def load(self, *, force: bool = False) -> None:
        with self._lock:
            if self._loaded and not force:
                return

            self._cards = {}
            if not self.wildcard_dir.exists():
                logger.warning("Wildcard directory does not exist: %s", self.wildcard_dir)
                self._loaded = True
                return

            paths = (
                sorted(self.wildcard_dir.rglob("*.txt"))
                if self.recursive
                else sorted(self.wildcard_dir.glob("*.txt"))
            )

            # Track stems to detect ambiguity.
            stem_to_rel: Dict[str, List[str]] = {}

            for path in paths:
                rel_key = path.relative_to(self.wildcard_dir).with_suffix("").as_posix()
                stem_key = path.stem

                items: List[str] = []
                weights: List[int] = []

                raw = path.read_bytes()
                text: str
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    enc = None
                    if chardet is not None:
                        try:
                            enc = (chardet.detect(raw) or {}).get("encoding")
                        except Exception:
                            enc = None
                    text = raw.decode(enc or "utf-8", errors="replace")

                for line_no, line in enumerate(text.splitlines(), start=1):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    m = self._WEIGHT_RE.match(line)
                    if not m:
                        continue
                    body = m.group(1)
                    weight = int(m.group(2)) if m.group(2) else 1
                    if weight <= 0:
                        continue

                    try:
                        BracketValidator.validate_or_raise(body, context=f"{path.name}:L{line_no}")
                    except ValueError as e:
                        # Skip only the bad line; keep loading the rest of the file.
                        logger.error("%s", e)
                        continue

                    items.append(body)
                    weights.append(weight)

                if items:
                    self._cards[rel_key] = WeightedList(items, weights)
                    stem_to_rel.setdefault(stem_key, []).append(rel_key)

            # Add stem-only keys if unambiguous (avoids breaking existing __colors__ usage).
            for stem, rel_keys in stem_to_rel.items():
                if stem in self._cards:
                    continue
                if len(rel_keys) == 1:
                    self._cards[stem] = self._cards[rel_keys[0]]
                else:
                    logger.warning(
                        "Wildcard stem key '%s' is ambiguous across %d files; use full relative keys: %s",
                        stem, len(rel_keys), ", ".join(rel_keys),
                    )

            self._loaded = True
            logger.info("Loaded %d wildcard card(s) from %s", len(self._cards), self.wildcard_dir)

    def match_keys(self, pattern: str) -> List[str]:
        return fnmatch.filter(list(self._cards.keys()), pattern)

    def pick_line(self, pattern: str, rng: random.Random, *, strict: bool = False) -> Tuple[str, str]:
        """
        Returns (resolved_key, chosen_line). 'pattern' may include fnmatch wildcards.
        """
        with self._lock:
            if not self._loaded:
                self.load()

            matches = self.match_keys(pattern)
            if not matches:
                if strict:
                    raise KeyError(f"No wildcard card matches pattern {pattern!r}")
                return pattern, pattern  # mirrors original behavior (drop underscores)
            key = rng.choice(matches)
            return key, self._cards[key].choose_one(rng)


# ------------------------------------------------------------------------------
# PromptExpander
# ------------------------------------------------------------------------------
@dataclass(frozen=True)
class ExpanderConfig:
    default_separator: str = ", "
    max_depth: int = 50
    strict_cards: bool = False
    validate_input_brackets: bool = True
    normalize_commas: bool = True
    allow_backslash_escapes: bool = True
    keep_history: int = 100


class PromptExpander:
    """
    Expands a prompt using a WildcardLibrary and a per-run RNG.

    Parsing strategy:
      - Single-pass scan over the string
      - When encountering '__', expand a card token '__pattern__'
      - When encountering '{', parse the matching '}' and evaluate that brace expression
      - Recursively expand results (with depth/cycle guards)
    """

    _WEIGHT_RE = re.compile(r"^(.*?)(?:\$(\d+))?$")
    _HISTORY_REF_RE = re.compile(r"^(?P<key>.+?)\[(?P<index>\d+)\]$")

    def __init__(
        self,
        library: WildcardLibrary,
        rng: random.Random,
        config: Optional[ExpanderConfig] = None,
        *,
        history: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        self.lib = library
        self.rng = rng
        self.cfg = config or ExpanderConfig()
        self.history = history if history is not None else {}

    def expand(self, text: str) -> str:
        if text is None or not isinstance(text, str):
            raise TypeError("text must be a string")
        if self.cfg.validate_input_brackets:
            BracketValidator.validate_or_raise(text, context="input")

        expanded = self._expand_text(text, depth=0, card_stack=())

        if self.cfg.normalize_commas:
            expanded = self._normalize_commas(expanded, original=text)

        # Final check: if stray '__' remains, treat as error (matches original intent).
        if "__" in expanded:
            raise ValueError(f"Expansion might be incomplete: leftover '__' in {expanded!r}")

        return expanded

    def _normalize_commas(self, expanded: str, *, original: str) -> str:
        prefix = "," if original.startswith(",") else ""
        suffix = "," if original.endswith(",") else ""
        parts = [p.strip() for p in expanded.split(",") if p.strip()]
        return prefix + ", ".join(parts) + suffix

    def _expand_text(self, text: str, *, depth: int, card_stack: Tuple[str, ...]) -> str:
        if depth > self.cfg.max_depth:
            raise RecursionError(
                f"Maximum expansion depth exceeded ({self.cfg.max_depth}). "
                "Possible recursive wildcard reference."
            )

        out: List[str] = []
        i = 0

        while i < len(text):
            # Backslash escapes (optional)
            if self.cfg.allow_backslash_escapes and text[i] == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue

            # Card: __pattern__
            if text.startswith("__", i):
                end = text.find("__", i + 2)
                if end == -1:
                    out.append(text[i:])  # leave as-is; outer caller will error on leftover '__'
                    break

                pattern = text[i + 2 : end]
                if pattern == "":
                    out.append("__")
                    i += 2
                    continue

                replacement = self._expand_card(pattern, depth=depth, card_stack=card_stack)
                out.append(replacement)
                i = end + 2
                continue

            # Brace: { ... }
            if text[i] == "{":
                content, end = self._extract_brace_content(text, i)
                evaluated = self._eval_brace(content, depth=depth, card_stack=card_stack)
                out.append(evaluated)
                i = end + 1
                continue

            out.append(text[i])
            i += 1

        return "".join(out)

    def _extract_brace_content(self, text: str, start: int) -> Tuple[str, int]:
        """Return (content_inside_braces, end_index_of_closing_brace)."""
        assert text[start] == "{"
        depth = 0
        i = start

        while i < len(text):
            if self.cfg.allow_backslash_escapes and text[i] == "\\" and i + 1 < len(text):
                i += 2
                continue

            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[start + 1 : i], i
            i += 1

        raise ValueError(f"Unmatched '{{' at position {start}")

    def _eval_brace(self, content: str, *, depth: int, card_stack: Tuple[str, ...]) -> str:
        count_spec, joiner, options_str = self._parse_brace_header(content)

        options_raw = self._split_top_level(options_str, sep_char="|")
        options: List[Tuple[str, int]] = []

        for opt in options_raw:
            opt = opt.strip()
            if not opt:
                continue
            body, w = self._parse_weight_suffix(opt)
            options.append((body, w))

        if not options:
            return ""

        if count_spec is None:
            chosen = self._choose_weighted_one(options)
            return self._expand_text(chosen, depth=depth + 1, card_stack=card_stack)

        lo, hi = count_spec
        c = len(options)
        lo = max(0, min(lo, c))
        hi = max(0, min(hi, c))
        if lo > hi:
            lo, hi = hi, lo

        k = self.rng.randint(lo, hi)
        chosen_list = self._choose_weighted_k(options, k)
        expanded_parts = [self._expand_text(x, depth=depth + 1, card_stack=card_stack) for x in chosen_list]
        return joiner.join(expanded_parts)

    def _parse_brace_header(self, content: str) -> Tuple[Optional[Tuple[int, int]], str, str]:
        """
        Parse a brace expression header.

        Supported:
          - {a|b|c}
          - {2$$a|b|c}
          - {2$$; $$a|b|c}
          - {2-4$$a|b|c}
          - {-3$$a|b|c}   -> 0..3
          - {2-$$a|b|c}   -> 2..N (clamped later)
          - {$$; $$a|b|c} -> no count, custom separator (still chooses 1)
        """
        default_sep = self.cfg.default_separator
        if "$$" not in content:
            return None, default_sep, content

        first = content.find("$$")
        spec = content[:first]
        rest = content[first + 2 :]

        count_spec: Optional[Tuple[int, int]] = None

        if spec == "":
            count_spec = None
        elif re.fullmatch(r"\d+", spec):
            n = int(spec)
            count_spec = (n, n)
        elif re.fullmatch(r"\d*-\d*", spec):
            left, right = spec.split("-", 1)
            lo = int(left) if left else 0
            hi = int(right) if right else 10**9
            count_spec = (lo, hi)
        else:
            # Not a header; treat as a normal option list.
            return None, default_sep, content

        joiner = default_sep
        second = rest.find("$$")
        if second != -1:
            joiner = rest[:second]
            options_str = rest[second + 2 :]
        else:
            options_str = rest

        return count_spec, joiner, options_str

    def _split_top_level(self, s: str, *, sep_char: str) -> List[str]:
        parts: List[str] = []
        buf: List[str] = []
        depth = 0
        i = 0

        while i < len(s):
            if self.cfg.allow_backslash_escapes and s[i] == "\\" and i + 1 < len(s):
                buf.append(s[i + 1])
                i += 2
                continue

            ch = s[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
            elif ch == sep_char and depth == 0:
                parts.append("".join(buf))
                buf = []
                i += 1
                continue

            buf.append(ch)
            i += 1

        parts.append("".join(buf))
        return parts

    def _parse_weight_suffix(self, s: str) -> Tuple[str, int]:
        m = self._WEIGHT_RE.match(s)
        if not m:
            return s, 1
        body = m.group(1)
        weight = int(m.group(2)) if m.group(2) else 1
        return body, max(1, weight)

    def _choose_weighted_one(self, options: Sequence[Tuple[str, int]]) -> str:
        texts = [t for t, _ in options]
        weights = [w for _, w in options]
        return self.rng.choices(texts, weights=weights, k=1)[0]

    def _choose_weighted_k(self, options: Sequence[Tuple[str, int]], k: int) -> List[str]:
        if k <= 0:
            return []
        if k >= len(options):
            texts = [t for t, _ in options]
            self.rng.shuffle(texts)
            return texts

        pool = list(options)
        chosen: List[str] = []
        for _ in range(k):
            texts = [t for t, _ in pool]
            weights = [w for _, w in pool]
            idx = self.rng.choices(range(len(pool)), weights=weights, k=1)[0]
            chosen.append(pool.pop(idx)[0])
        return chosen

    def _expand_card(self, pattern: str, *, depth: int, card_stack: Tuple[str, ...]) -> str:
        history_key, history_index = self._parse_history_reference(pattern)
        if history_key is not None:
            return self._get_history_value(history_key, history_index)

        if not self.lib.loaded:
            self.lib.load()

        key, line = self.lib.pick_line(pattern, self.rng, strict=self.cfg.strict_cards)

        # If strict is False and no match existed, lib returns (pattern, pattern)
        if key == pattern and line == pattern and pattern not in self.lib.cards:
            return pattern

        # Cycle detection on resolved keys
        if key in card_stack:
            raise ValueError(f"Detected recursive wildcard cycle: {' -> '.join(card_stack + (key,))}")

        expanded = self._expand_text(line, depth=depth + 1, card_stack=card_stack + (key,))
        self._record_history(key, expanded)
        if pattern != key:
            self._record_history(pattern, expanded)
        return expanded

    def _parse_history_reference(self, pattern: str) -> Tuple[Optional[str], int]:
        m = self._HISTORY_REF_RE.match(pattern)
        if not m:
            return None, 0
        return m.group("key"), int(m.group("index"))

    def _get_history_value(self, key: str, index: int) -> str:
        items = self.history.get(key) or []
        if index < 0:
            raise ValueError(f"History index must be >= 0 (got {index}) for {key!r}")
        if index >= len(items):
            raise ValueError(f"History for {key!r} has only {len(items)} item(s); cannot access index {index}")
        return items[index]

    def _record_history(self, key: str, value: str) -> None:
        keep = int(self.cfg.keep_history)
        if keep <= 0:
            return
        items = self.history.setdefault(key, [])
        items.insert(0, value)
        if len(items) > keep:
            del items[keep:]




# ------------------------------------------------------------------------------
# ComfyUI Node
# ------------------------------------------------------------------------------
class TextWildcards:
    """
    ComfyUI node for expanding wildcards.
    """

    # Library is module-level cached for performance.
    _LIB = WildcardLibrary(Path(__file__).resolve().parent / "wildcards", recursive=True)
    _HISTORY_LOCK = threading.RLock()
    _HISTORY: Dict[str, Dict[str, List[str]]] = {}

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "refresh": ("INT", {"default": 0, "min": 0, "max": 1}),
                "n_keep_history": ("INT", {"default": 100, "min": 0, "max": 100000}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "ascii_text")
    FUNCTION = "encode"
    CATEGORY = "Randomizer"

    def encode(
        self,
        seed: int,
        text: str,
        refresh: int,
        n_keep_history: int,
        unique_id: Optional[str] = None,
    ):
        # Local RNG; does not touch global random state.
        rng = random.Random(int(seed))

        if int(refresh) == 1:
            self._LIB.load(force=True)
        elif not self._LIB.loaded:
            self._LIB.load()

        keep = max(0, int(n_keep_history))
        uid = unique_id or "__default__"

        with self._HISTORY_LOCK:
            history = self._HISTORY.setdefault(uid, {})
            if keep <= 0:
                history.clear()
            else:
                for k, v in list(history.items()):
                    if not v:
                        history.pop(k, None)
                        continue
                    if len(v) > keep:
                        del v[keep:]

            expander = PromptExpander(self._LIB, rng, ExpanderConfig(keep_history=keep), history=history)
            expanded = expander.expand(text)

        ascii_text = expanded.encode("ascii", "replace").decode("ascii")
        return (expanded, ascii_text)


# ------------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# ComfyUI registration
# ----------------------------------------------------------------------------
NODE_CLASS_MAPPINGS = {
    "TextWildcards": TextWildcards,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "TextWildcards": "Text Wildcards",
}

# CLI
# ------------------------------------------------------------------------------
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Expand wildcards in a text prompt.")
    parser.add_argument("--text", type=str, required=True, help="Text with wildcards to expand.")
    parser.add_argument("--seed", type=int, default=0, help="Random seed for reproducible expansion.")
    parser.add_argument("--refresh", action="store_true", help="Force reload wildcard files from disk.")
    parser.add_argument("--wildcards-dir", type=str, default=None, help="Override the wildcards directory.")
    parser.add_argument("--no-normalize-commas", action="store_true", help="Disable comma/whitespace normalization.")
    parser.add_argument("--strict", action="store_true", help="Raise error if a wildcard key is not found.")
    parser.add_argument("--no-validate", action="store_true", help="Disable bracket validation on the input prompt.")
    parser.add_argument("--log-level", type=str, default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR).")

    args = parser.parse_args()

    level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)

    wildcards_dir = (
        Path(args.wildcards_dir)
        if args.wildcards_dir is not None
        else Path(__file__).resolve().parent / "wildcards"
    )

    lib = WildcardLibrary(wildcards_dir, recursive=True)
    lib.load(force=bool(args.refresh))

    cfg = ExpanderConfig(
        strict_cards=bool(args.strict),
        normalize_commas=not bool(args.no_normalize_commas),
        validate_input_brackets=not bool(args.no_validate),
    )

    expander = PromptExpander(lib, random.Random(int(args.seed)), cfg)
    expanded = expander.expand(args.text)

    print(expanded)


if __name__ == "__main__":
    main()
