import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from commit0.harness.constants_java import (
    JAVA_SOURCE_EXT,
    JAVA_STUB_MARKER,
    JAVA_SKIP_FILENAMES,
)
from agent.thinking_capture import SummarizerCost

logger = logging.getLogger(__name__)

# Re-export shared utilities from agent_utils so Java callers don't
# import directly from the Python-specific module.
from agent.agent_utils import get_specification, SPEC_INFO_HEADER  # noqa: F401
from commit0.harness.utils import relativize


_BUILD_DIR_NAMES = frozenset({"target", "build", ".gradle"})


def _in_build_dir(rel_parts: tuple) -> bool:
    for part in rel_parts:
        if part in _BUILD_DIR_NAMES:
            return True
    if len(rel_parts) >= 2:
        for i in range(len(rel_parts) - 1):
            if rel_parts[i] == ".mvn" and rel_parts[i + 1] == "wrapper":
                return True
    return False


def collect_java_files(repo_path: str) -> List[str]:
    p = Path(repo_path)
    java_files = []
    for f in p.rglob(f"*{JAVA_SOURCE_EXT}"):
        rel = f.relative_to(p)
        if _in_build_dir(rel.parts):
            continue
        rel_str = str(rel)
        if "/src/test/" in rel_str or "\\src\\test\\" in rel_str:
            continue
        if f.name in JAVA_SKIP_FILENAMES:
            continue
        java_files.append(str(f))
    return java_files


def is_java_stubbed(file_path: str) -> bool:
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return JAVA_STUB_MARKER in content


def count_java_stubs(repo_path: str) -> dict:
    files = collect_java_files(repo_path)
    total_stubs = 0
    total_files = len(files)
    stubbed_files = 0
    for f in files:
        try:
            content = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stubs = content.count(JAVA_STUB_MARKER)
        total_stubs += stubs
        if stubs > 0:
            stubbed_files += 1
    return {
        "total_files": total_files,
        "stubbed_files": stubbed_files,
        "total_stubs": total_stubs,
    }


# ---------------------------------------------------------------------------
# Java-specific summarization prompts (hardcoded "Java library")
# ---------------------------------------------------------------------------

_JAVA_SUMMARIZER_SYSTEM_PROMPT = (
    "You are a technical documentation summarizer for an AI coding "
    "agent that must implement a Java library from its specification. "
    "Your summary will be the ONLY reference the agent receives.\n\n"
    "PRESERVE (mandatory, never drop):\n"
    "- Every public API signature: function/class/method names, "
    "parameter names, types, default values, return types.\n"
    "- Behavioral contracts: what each function/method does, "
    "preconditions, postconditions, invariants.\n"
    "- Error handling: exceptions raised, error conditions, "
    "what happens on invalid input, fallback behaviors.\n"
    "- Code examples and usage patterns that show HOW to call the API "
    "(keep them verbatim or minimally shortened).\n"
    "- Module/package structure, class hierarchy, inheritance, "
    "dependencies between components.\n"
    "- Constants, enums, config values, magic numbers with meaning.\n"
    "- Edge cases, boundary conditions, thread-safety notes, "
    "platform-specific behavior.\n\n"
    "OMIT (drop first when budget is tight):\n"
    "- Introductions, installation instructions, changelog, "
    "marketing text, contributor guidelines.\n"
    "- Verbose prose that restates what the API signature already shows.\n"
    "- Redundant examples (keep one per pattern, drop duplicates).\n\n"
    "PRIORITY (when budget forces cuts, drop in this order):\n"
    "1. Drop internal/private helpers before public API.\n"
    "2. Drop verbose descriptions before signatures.\n"
    "3. Drop duplicate examples before unique ones.\n"
    "4. Never drop: public API signatures, error conditions, "
    "code examples showing non-obvious usage.\n\n"
    "FORMAT: Be maximally dense. Use terse notation over full sentences. "
    "Group by module/class. Use code blocks for signatures."
)


_JAVA_CONSOLIDATION_SYSTEM_PROMPT = (
    "You are combining multiple section summaries of a Java library "
    "specification into one cohesive summary. The sections may overlap.\n\n"
    "Rules:\n"
    "- Remove duplicate API signatures, keeping the most complete version.\n"
    "- Preserve ALL unique: public API signatures, error conditions, "
    "code examples, behavioral contracts, edge cases.\n"
    "- Merge related sections logically (group by module/class).\n"
    "- Use terse notation. No preamble or meta-commentary."
)


# ---------------------------------------------------------------------------
# Java-specific summarization functions
# ---------------------------------------------------------------------------

def _count_tokens(text: str, model: str) -> int:
    """Count tokens using litellm's tokenizer for the given model."""
    try:
        import litellm

        return litellm.token_counter(model=model, text=text)
    except Exception:
        logger.warning(
            "litellm tokenizer unavailable for model '%s', "
            "falling back to len//4 approximation",
            model,
        )
        return len(text) // 4


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    """Split text into chunks of approximately chunk_size characters.

    Tries to break at newline boundaries to avoid splitting mid-sentence.
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(text[start:])
            break
        search_start = end - chunk_size // 5
        newline_pos = text.rfind("\n", search_start, end)
        if newline_pos > start:
            end = newline_pos + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def _summarize_single_java(
    text: str,
    model: str,
    max_tokens: int,
    token_budget: int,
    litellm_module: object,
    system_prompt: Optional[str] = None,
    timeout: float = 120,
) -> tuple[Optional[str], SummarizerCost]:
    """Call LLM to summarize a single piece of text for Java. Returns (summary, cost_info)."""
    prompt = system_prompt or _JAVA_SUMMARIZER_SYSTEM_PROMPT
    response = litellm_module.completion(  # type: ignore[union-attr]
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    prompt
                    + "\n- Your summary MUST be under "
                    + str(token_budget)
                    + " tokens."
                ),
            },
            {
                "role": "user",
                "content": "Summarize this specification:\n\n" + text,
            },
        ],
        max_tokens=max_tokens,
        timeout=timeout,
        num_retries=3,
        retry_strategy="exponential_backoff_retry",
    )

    cost = SummarizerCost()
    usage = getattr(response, "usage", None)
    if usage:
        cost.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        cost.completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    try:
        import litellm

        cost.cost = litellm.completion_cost(completion_response=response)
    except Exception:
        pass

    content = response.choices[0].message.content  # type: ignore[union-attr]
    if content:
        return content.strip(), cost
    return None, cost


def summarize_specification_java(
    spec_text: str,
    model: str,
    max_tokens: int = 4000,
    max_char_length: int = 10000,
    timeout: float = 120,
    cache_path: Optional[Path] = None,
) -> tuple[str, list[SummarizerCost]]:
    """Summarize specification text using an LLM for Java libraries.

    Returns (summary_text, list_of_costs) where costs tracks every LLM call made.
    For specs that fit within a single LLM context window, summarizes in one pass.
    For larger specs, splits into chunks, summarizes each in parallel, then consolidates.
    Falls back to truncation if any LLM call fails.
    """
    all_costs: list[SummarizerCost] = []

    max_token_length = _count_tokens(spec_text[:max_char_length], model)
    if max_token_length < 1:
        max_token_length = max_char_length // 4

    cache_key = hashlib.sha256(
        (spec_text + model + str(max_char_length)).encode()
    ).hexdigest()

    if cache_path is not None:
        try:
            if cache_path.exists():
                cached = json.loads(cache_path.read_text())
                if cached.get("hash") == cache_key:
                    logger.info("Spec summary cache hit (%s)", relativize(cache_path))
                    return cached["summary"], all_costs
        except Exception:
            logger.debug("Cache read failed, proceeding with summarization")

    import litellm

    original_len = len(spec_text)
    original_tokens = _count_tokens(spec_text, model)

    def _write_cache(summary: str) -> None:
        if cache_path is None:
            return
        try:
            cache_path.write_text(
                json.dumps(
                    {
                        "hash": cache_key,
                        "model": model,
                        "max_char_length": max_char_length,
                        "summary": summary,
                    }
                )
            )
        except Exception:
            logger.debug("Cache write failed for %s", cache_path)

    # ~100K tokens per chunk, leaving room for system prompt + output
    chunk_max_tokens = 100_000
    chunk_max_chars = chunk_max_tokens * 4

    try:
        input_tokens = original_tokens
        if input_tokens <= chunk_max_tokens:
            summary, cost = _summarize_single_java(
                text=spec_text,
                model=model,
                max_tokens=max_tokens,
                token_budget=max_token_length,
                litellm_module=litellm,
                timeout=timeout,
            )
            all_costs.append(cost)
            if summary:
                logger.info(
                    "Spec summarized (single-pass): %d chars (%d tokens) -> %d chars (model=%s)",
                    original_len,
                    original_tokens,
                    len(summary),
                    model,
                )
                _write_cache(summary)
                return summary, all_costs
            logger.warning("Empty summary from %s, falling back to truncation", model)
            return spec_text[:max_char_length], all_costs

        chunks = _chunk_text(spec_text, chunk_max_chars)
        logger.info(
            "Spec too large for single pass (%d tokens), splitting into %d chunks",
            original_tokens,
            len(chunks),
        )

        per_chunk_token_budget = max_token_length // len(chunks)
        chunk_summaries: list[str] = []

        max_workers = min(len(chunks), 4)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(
                    _summarize_single_java,
                    chunk,
                    model,
                    max_tokens,
                    per_chunk_token_budget,
                    litellm,
                    None,
                    timeout,
                ): i
                for i, chunk in enumerate(chunks)
            }
            results: dict[int, Optional[tuple[Optional[str], SummarizerCost]]] = {}
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception:
                    logger.warning(
                        "Chunk %d/%d raised exception, skipping", idx + 1, len(chunks)
                    )
                    results[idx] = None

        for i in range(len(chunks)):
            r = results.get(i)
            if r is not None:
                text_result, chunk_cost = r
                all_costs.append(chunk_cost)
                if text_result:
                    chunk_summaries.append(text_result)
                else:
                    logger.warning(
                        "Chunk %d/%d returned empty, skipping", i + 1, len(chunks)
                    )
            else:
                logger.warning(
                    "Chunk %d/%d returned empty, skipping", i + 1, len(chunks)
                )

        if not chunk_summaries:
            logger.warning("All chunk summaries empty, falling back to truncation")
            return spec_text[:max_char_length], all_costs

        merged = "\n\n".join(chunk_summaries)
        merged_tokens = _count_tokens(merged, model)
        logger.info(
            "Consolidating %d chunk summaries (%d tokens total) into final summary",
            len(chunk_summaries),
            merged_tokens,
        )

        if merged_tokens <= max_token_length:
            logger.info(
                "Spec summarized (chunked, no consolidation needed): %d tokens -> %d tokens",
                original_tokens,
                merged_tokens,
            )
            _write_cache(merged)
            return merged, all_costs

        final, consolidation_cost = _summarize_single_java(
            text=merged,
            model=model,
            max_tokens=max_tokens,
            token_budget=max_token_length,
            litellm_module=litellm,
            system_prompt=_JAVA_CONSOLIDATION_SYSTEM_PROMPT,
            timeout=timeout,
        )
        all_costs.append(consolidation_cost)
        if final:
            logger.info(
                "Spec summarized (chunked+consolidated): %d chars -> %d chars (model=%s)",
                original_len,
                len(final),
                model,
            )
            _write_cache(final)
            return final, all_costs

        logger.warning("Consolidation returned empty, using merged chunk summaries")
        return merged, all_costs

    except Exception as e:
        logger.warning(
            "Spec summarization failed (%s), falling back to truncation: %s", model, e
        )
        return spec_text[:max_char_length], all_costs
