"""Pure-stdlib OpenAI Responses API deep research engine for tool-research."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path

from ar3.fsio import atomic_write_text

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_RESEARCH_MODEL = "gpt-5.6-sol"
DEFAULT_PLANNER_MODEL = "gpt-5.6-luna"
DEFAULT_POLL_INTERVAL_S = 30.0
DEFAULT_JOB_TIMEOUT_S = 20.0 * 60.0
FANOUT_MIN = 1
FANOUT_MAX = 8
FANOUT_AUTO = 0
CANDIDATE_FLOOR = 12
EST_PLANNER_INPUT_PER_M = 1.0
EST_PLANNER_OUTPUT_PER_M = 6.0
UNVERIFIED_EST_RESEARCH_JOB_USD = 0.50
BASE_MAX_TOOL_CALLS = 50
QPD_WARN_THRESHOLD = 0.4


class ResearchError(RuntimeError):
    """Raised when a research API call, decomposition, or quorum check fails."""


def resolve_base_url(api_url: str | None = None) -> str:
    chosen = (api_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).strip()
    return chosen.rstrip("/") or DEFAULT_OPENAI_BASE_URL


def resolve_poll_interval(override: float | None = None) -> float:
    if override is not None and override > 0:
        return float(override)
    env_raw = (os.environ.get("AR3_RESEARCH_POLL_INTERVAL") or "").strip()
    if env_raw:
        try:
            val = float(env_raw)
            if math.isfinite(val) and val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_POLL_INTERVAL_S


def prompt_hash(prompt: str, model: str = DEFAULT_RESEARCH_MODEL) -> str:
    clean = "".join(prompt.split())
    raw = f"{clean}\0{model}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def job_key(question: str, fanout: int, model: str) -> str:
    raw = f"{question}\0{fanout}\0{model}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def research_dir(workdir: Path) -> Path:
    d = Path(workdir).resolve() / ".research"
    d.mkdir(parents=True, exist_ok=True)
    return d


def max_tool_calls(n: int) -> int:
    if n <= 1:
        return BASE_MAX_TOOL_CALLS
    return max(10, BASE_MAX_TOOL_CALLS // n)


def clamp_fanout(n: int) -> tuple[int, bool]:
    clamped = max(FANOUT_MIN, min(FANOUT_MAX, n))
    return clamped, clamped != n


def merge_quorum(n: int) -> int:
    return math.ceil(0.67 * n)


def _http_json(
    url: str,
    *,
    api_key: str,
    payload: dict | None = None,
    timeout: float = 120.0,
) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        return {"error": f"HTTP {exc.code}: {detail or exc.reason}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"error": str(reason)}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return {"error": f"invalid JSON from API: {exc}"}
    return parsed if isinstance(parsed, dict) else {"error": "API response was not a JSON object"}


def submit_research_job(
    api_key: str,
    prompt: str,
    *,
    model: str = DEFAULT_RESEARCH_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
    tool_budget: int | None = None,
) -> dict:
    payload: dict = {
        "model": model,
        "input": prompt,
        "tools": [{"type": "web_search_preview"}],
        "background": True,
    }
    if tool_budget is not None:
        payload["max_tool_calls"] = tool_budget
    return _http_json(f"{base_url}/responses", api_key=api_key, payload=payload)


def check_job_status(
    api_key: str,
    response_id: str,
    *,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
) -> dict:
    safe_id = urllib.parse.quote(response_id, safe="")
    return _http_json(f"{base_url}/responses/{safe_id}", api_key=api_key, payload=None)


def call_text_model(
    api_key: str,
    model: str,
    prompt: str,
    *,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
) -> dict:
    payload = {"model": model, "input": prompt}
    return _http_json(f"{base_url}/responses", api_key=api_key, payload=payload, timeout=300.0)


def extract_output_text(response_data: dict) -> str:
    parts: list[str] = []
    for item in response_data.get("output") or ():
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for block in item.get("content") or ():
                if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                    text = block.get("text")
                    if text:
                        parts.append(str(text))
        elif item.get("type") == "output_text" and item.get("text"):
            parts.append(str(item["text"]))
    if parts:
        return "\n".join(parts)
    for key in ("output_text", "text"):
        val = response_data.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def usage_tokens(response_data: dict) -> tuple[int, int]:
    usage = response_data.get("usage") or {}
    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return inp, out


def est_planner_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * EST_PLANNER_INPUT_PER_M / 1_000_000
        + output_tokens * EST_PLANNER_OUTPUT_PER_M / 1_000_000
    )


def extract_urls(text: str) -> list[str]:
    found = re.findall(r"https?://[^\s\]\)>'\"<>]+", text or "")
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        cleaned = u.rstrip(".,;:)")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def normalize_url(url: str) -> str:
    """Conservative URL normalizer for citation corroboration unions.

    Lowercases scheme and host, removes leading `www.`, drops trailing path
    slashes and fragments, and preserves path + sorted query parameters so
    distinct document revisions, filings, and primary-vs-summary URLs stay
    distinct.
    """
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return url.strip().lower()
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = urllib.parse.urlencode(
        sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    )
    return urllib.parse.urlunparse((scheme, netloc, path, "", query, ""))


def urls_from_response(data: dict, body: str) -> list[str]:
    urls = extract_urls(body)
    for item in data.get("output") or ():
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or ():
            if not isinstance(block, dict):
                continue
            for ann in block.get("annotations") or ():
                if isinstance(ann, dict) and ann.get("url"):
                    u = str(ann["url"]).strip()
                    if u and u not in urls:
                        urls.append(u)
    return urls


def _parse_json_obj(text: str) -> object | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    brace = re.search(r"\{.*\}", stripped, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


def mmr_select(
    candidates: list[dict],
    n: int,
    *,
    lambda_: float = 0.0,
) -> list[dict]:
    """Select `n` candidates via MMR on token Jaccard of `seed_query` (`lambda_=0` = pure diversity)."""
    if n <= 0 or not candidates:
        return []
    n = min(n, len(candidates))
    tokenized = [
        _tokenize(str(c.get("seed_query") or c.get("angle") or ""))
        for c in candidates
    ]
    selected = [0]
    remaining = list(range(1, len(candidates)))
    while len(selected) < n and remaining:
        best_i = remaining[0]
        best_score = float("-inf")
        for idx in remaining:
            toks = tokenized[idx]
            max_sim = max(_jaccard(toks, tokenized[j]) for j in selected)
            score = lambda_ * 1.0 - (1.0 - lambda_) * max_sim
            if score > best_score:
                best_score = score
                best_i = idx
        remaining.remove(best_i)
        selected.append(best_i)
    return [candidates[i] for i in selected]


def select_with_adversarial(
    candidates: list[dict],
    adversarial: dict,
    n: int,
) -> list[dict]:
    """Select `N - 1` topical angles by MMR and append the reserved adversarial slot."""
    if n <= 0:
        return []
    if n == 1:
        return [adversarial]
    adv_angle = str(adversarial.get("angle") or "").strip().lower()
    pool = [
        c
        for c in candidates
        if str(c.get("angle") or "").strip().lower() != adv_angle
    ]
    topical = mmr_select(pool if pool else candidates, n - 1, lambda_=0.0)
    return topical + [adversarial]


def _build_sub_prompt(
    goal: str,
    brief: str,
    selected: dict,
    all_selected: list[dict],
) -> str:
    angle = str(selected.get("angle") or "").strip()
    seed = str(selected.get("seed_query") or angle).strip()
    siblings = [
        str(o.get("angle") or "").strip()
        for o in all_selected
        if o is not selected and str(o.get("angle") or "").strip()
    ]
    if siblings:
        sibling_block = (
            "Sibling assignments (other sub-jobs own these scopes):\n"
            + "\n".join(f"- {o}" for o in siblings)
            + "\nDo not enter a sibling's scope except to surface a contradiction with it."
        )
    else:
        sibling_block = "Stay tightly scoped to this angle only."
    return (
        f"## Global objective\n{goal}\n\n"
        f"## Shared brief\n{brief}\n\n"
        f"## Your objective\n"
        f"Research this angle only: {angle}\n"
        f"Seed query / search focus: {seed}\n\n"
        f"## Output format\n"
        "Markdown brief with: short summary, numbered findings with inline "
        "source URLs (include observation dates when the source states them), "
        "and a Sources list. Every claim needs a URL.\n\n"
        f"## Source guidance\n"
        "Prefer primary sources, peer-reviewed work, official docs, and "
        "high-signal reporting. Note date and provenance when relevant.\n\n"
        f"## Boundaries\n"
        f"{sibling_block}\n"
    )


def _clean_angle(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    angle = item.get("angle")
    seed = item.get("seed_query") or angle
    if isinstance(angle, str) and angle.strip() and isinstance(seed, str) and seed.strip():
        return {"angle": angle.strip(), "seed_query": seed.strip()}
    return None


def brief_and_candidates(
    api_key: str,
    prompt: str,
    *,
    planner_model: str = DEFAULT_PLANNER_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
) -> tuple[str, list[dict], dict, int, dict]:
    """Stage 0 planner: brief, >=12 candidate angles, reserved adversarial slot, and recommended N."""
    decompose_prompt = (
        "You are planning a multi-angle deep-research run.\n"
        "Given the research question below, return ONLY a JSON object with:\n"
        '  "brief": a planning brief of at most 200 words,\n'
        '  "recommended_fanout": an integer in [1, 8] for how many parallel '
        "angles this question warrants (use 1 for simple factual questions "
        "that need no fanout),\n"
        f'  "candidates": an array of at least {CANDIDATE_FLOOR} objects, '
        'each with "angle" (short label) and "seed_query" (search-oriented '
        "phrase). Candidates must cover DISTINCT dimensions — not paraphrases "
        "of the same anchor,\n"
        '  "adversarial": one object with "angle" and "seed_query" that '
        "actively seeks contradictions, counter-evidence, or reasons the "
        "main thesis may be wrong. This is reserved separately from "
        "candidates and must not be omitted.\n\n"
        f"Question:\n{prompt}"
    )
    resp = call_text_model(api_key, planner_model, decompose_prompt, base_url=base_url)
    if resp.get("error"):
        raise ResearchError(f"Stage 0 decompose failed: {resp['error']}")
    text = extract_output_text(resp)
    parsed = _parse_json_obj(text)
    if not isinstance(parsed, dict):
        raise ResearchError(
            f"Stage 0 decompose did not return a JSON object (got: {text[:200]!r})"
        )
    brief = parsed.get("brief")
    candidates = parsed.get("candidates")
    adv_raw = parsed.get("adversarial")
    rec_raw = parsed.get("recommended_fanout")
    if not isinstance(brief, str) or not brief.strip():
        raise ResearchError("Stage 0 brief must be a non-empty string")
    if not isinstance(candidates, list):
        raise ResearchError("Stage 0 candidates must be a list")
    adversarial = _clean_angle(adv_raw)
    if adversarial is None:
        raise ResearchError("Stage 0 adversarial must be an angle object")
    cleaned = [item for c in candidates if (item := _clean_angle(c)) is not None]
    if len(cleaned) < CANDIDATE_FLOOR:
        raise ResearchError(
            f"Stage 0 returned {len(cleaned)} usable candidates; need >= {CANDIDATE_FLOOR}"
        )
    if isinstance(rec_raw, bool):
        raise ResearchError("Stage 0 recommended_fanout must be an integer")
    try:
        rec_int = int(rec_raw)
    except (TypeError, ValueError):
        raise ResearchError("Stage 0 recommended_fanout must be an integer") from None
    rec_clamped, _ = clamp_fanout(rec_int)
    return brief.strip(), cleaned, adversarial, rec_clamped, resp


def citation_union(
    reports: Sequence[tuple[int, str, str, list[str]] | tuple[str, str, list[str]]],
) -> list[dict]:
    counts: dict[str, dict] = {}
    for pos, item in enumerate(reports, start=1):
        if len(item) == 4:
            idx, _q, _body, urls = item
        else:
            _q, _body, urls = item
            idx = pos
        for u in urls:
            norm = normalize_url(u)
            entry = counts.setdefault(
                norm,
                {"url": u.strip(), "norm": norm, "count": 0, "reports": []},
            )
            entry["count"] += 1
            label = f"report-{int(idx):02d}.md"
            if label not in entry["reports"]:
                entry["reports"].append(label)
    return sorted(counts.values(), key=lambda e: (-e["count"], e["norm"]))


def compose_reports(
    api_key: str,
    prompt: str,
    reports: list[tuple[int, str, str, list[str]]],
    missing: list[dict],
    *,
    research_model: str = DEFAULT_RESEARCH_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
) -> tuple[str, dict]:
    """Pass 1: compose prose and surface dated contradictions (no citations yet)."""
    blocks: list[str] = []
    for idx, question, body, urls in reports:
        url_lines = "\n".join(f"- {u}" for u in urls) or "- (none extracted)"
        blocks.append(
            f"### Sub-report {idx}\n"
            f"File: report-{idx:02d}.md\n"
            f"Question: {question}\n\n"
            f"{body}\n\n"
            f"Cited URLs:\n{url_lines}\n"
        )
    missing_block = ""
    if missing:
        lines = [
            f"- Sub-question {m['index']} ({m['question']}): {m['reason']}"
            for m in missing
        ]
        missing_block = (
            "\n## Incomplete coverage (MUST include in output)\n"
            "The following sub-questions did not produce a usable report. "
            "State them plainly in a ## Missing sub-questions section — "
            "do not invent findings for them:\n"
            + "\n".join(lines)
            + "\n"
        )
    compose_prompt = (
        "Compose ONE markdown research brief from the completed sub-reports.\n"
        "This is the COMPOSITION pass only — do NOT attach citation markers "
        "yet; a later pass will re-attach them.\n"
        "Requirements:\n"
        "1. Cluster related claims across sub-reports into coherent prose.\n"
        "2. Include ## Contradictions listing every point where sub-reports "
        "disagree. SURFACING is mandatory — do NOT pick a winner, reconcile, "
        "or silently drop either side. Phrase each side with its observation "
        "date: 'A reports X as of <date>' / 'B reports Y as of <date>' so a "
        "staleness gap is visible as one. If a side gives no date, write "
        "'as of (undated)'.\n"
        "3. If any sub-questions are missing, include ## Missing sub-questions "
        "naming each and the reason (timed out / failed).\n"
        "4. Start with # Merged research and a short ## Summary.\n"
        "5. Output markdown only. Do not invent sources or resolve conflicts.\n\n"
        f"Original question:\n{prompt}\n"
        f"{missing_block}\n"
        + "\n".join(blocks)
    )
    resp = call_text_model(api_key, research_model, compose_prompt, base_url=base_url)
    if resp.get("error"):
        raise ResearchError(f"compose pass failed: {resp['error']}")
    text = extract_output_text(resp).strip()
    if not text:
        raise ResearchError("compose pass returned empty text")
    return text, resp


def attach_citations(
    api_key: str,
    composed: str,
    reports: list[tuple[int, str, str, list[str]]],
    *,
    research_model: str = DEFAULT_RESEARCH_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
) -> tuple[str, dict]:
    """Pass 2: re-attach per-claim `[report-NN.md | URL]` citations conservatively."""
    rows = citation_union(reports)
    cite_lines = "\n".join(
        f"- {c['url']} (norm={c['norm']}; corroboration={c['count']}; {', '.join(c['reports'])})"
        for c in rows
    ) or "- (none)"
    report_blobs = [
        f"report-{idx:02d}.md | {question}\n"
        f"URLs: {', '.join(urls) if urls else '(none)'}\n"
        f"Excerpt:\n{body[:2000]}\n"
        for idx, question, body, urls in reports
    ]
    attach_prompt = (
        "Citation re-attachment pass. The composed brief below has NO "
        "citation markers yet. Attach per-claim source attribution of the "
        "form [report-NN.md | URL], using the citation union and sub-reports.\n"
        "Rules:\n"
        "1. Claim-level citation unions over normalized URLs — but do NOT "
        "collapse distinct sources: different document revisions, separate "
        "filings, a primary source and a summary of it, or similar-titled "
        "papers must stay separate. When in doubt, keep them separate.\n"
        "2. Include ## Citations with the URL union below, preserving "
        "corroboration counts.\n"
        "3. Preserve ## Contradictions exactly (including per-side "
        "'as of <date>' phrasing); only add citation markers, never smooth "
        "disagreement away.\n"
        "4. Output the full markdown brief with citations attached. "
        "Do not invent sources.\n\n"
        f"Composed brief:\n{composed}\n\n"
        f"Citation union:\n{cite_lines}\n\n"
        "Sub-reports:\n" + "\n".join(report_blobs)
    )
    resp = call_text_model(api_key, research_model, attach_prompt, base_url=base_url)
    if resp.get("error"):
        raise ResearchError(f"citation attach pass failed: {resp['error']}")
    text = extract_output_text(resp).strip()
    if not text:
        raise ResearchError("citation attach pass returned empty text")
    return text, resp


def citation_check(
    api_key: str,
    merged: str,
    reports: list[tuple[int, str, str, list[str]]],
    *,
    planner_model: str = DEFAULT_PLANNER_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
) -> tuple[str | None, dict]:
    """Pass 3: verify per-claim citations on `planner_model`."""
    report_blobs = [
        f"report-{idx:02d}.md | {question}\n"
        f"URLs: {', '.join(urls) if urls else '(none)'}\n"
        f"Excerpt:\n{body[:1500]}\n"
        for idx, question, body, urls in reports
    ]
    check_prompt = (
        "Citation check pass. For each claim in the merged brief, verify:\n"
        "1. The cited URL resolves (or is present in the sub-report sources).\n"
        "2. It is the correct document for that claim (not a neighbour's).\n"
        "3. The document actually supports the adjacent claim.\n"
        "4. Numerics and dates in the claim match the source.\n"
        "5. No citations were inherited from a neighbouring claim.\n"
        "6. Conflicts / contradictions are still preserved, not smoothed away.\n"
        "Return markdown:\n"
        "## Citation check\n"
        "- OK / WEAK / MISSING lines per claim (brief).\n"
        "Do not rewrite the brief. Do not resolve contradictions.\n\n"
        f"Merged brief:\n{merged}\n\n"
        "Sub-reports:\n" + "\n".join(report_blobs)
    )
    resp = call_text_model(api_key, planner_model, check_prompt, base_url=base_url)
    if resp.get("error"):
        print(f"tool-research: citation check skipped: {resp['error']}", file=sys.stderr)
        return None, resp
    text = extract_output_text(resp).strip()
    if not text:
        print("tool-research: citation check skipped: empty output", file=sys.stderr)
        return None, resp
    return text, resp


def _write_json(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _write_sub_report(
    path: Path,
    index: int,
    question: str,
    body: str,
    urls: list[str],
) -> None:
    lines = [
        f"# Sub-report {index}",
        "",
        "## Question",
        "",
        question,
        "",
        "## Findings",
        "",
        body.strip() or "(no text extracted from response)",
        "",
        "## Sources",
        "",
    ]
    if urls:
        lines.extend(f"- {u}" for u in urls)
    else:
        lines.append("- (none extracted)")
    lines.append("")
    atomic_write_text(path, "\n".join(lines))


def render_single_report(status_data: dict) -> str:
    body = extract_output_text(status_data).strip()
    urls = urls_from_response(status_data, body)
    if not body:
        body = "(no markdown text extracted from response)"
    unlisted = [u for u in urls if u not in body]
    if unlisted:
        source_lines = "\n".join(f"- {u}" for u in unlisted)
        body = f"{body}\n\n## Sources\n\n{source_lines}"
    return body + "\n"


def _count_web_queries(data: dict) -> int:
    count = 0
    for item in data.get("output") or ():
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if "web_search" in item_type:
            count += 1
            continue
        if item_type == "message":
            for block in item.get("content") or ():
                if isinstance(block, dict) and "web_search" in str(block.get("type") or ""):
                    count += 1
    return count


def _domain_of(url: str) -> str:
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def _mean_qpd(completed_data: list[dict], url_lists: list[list[str]]) -> float:
    total_q = sum(_count_web_queries(d) for d in completed_data)
    total_d = sum(len({normalize_url(u) for u in urls}) for urls in url_lists)
    return (total_q / total_d) if total_d else 0.0


def _citation_overlap_ratio(url_lists: list[list[str]]) -> float:
    sets = [{normalize_url(u) for u in urls} for urls in url_lists]
    if len(sets) < 2:
        return 0.0
    total = 0.0
    pairs = 0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            total += _jaccard(sets[i], sets[j])
            pairs += 1
    return (total / pairs) if pairs else 0.0


def _unique_domains(url_lists: list[list[str]]) -> int:
    return len(
        {
            d
            for urls in url_lists
            for u in urls
            if (d := _domain_of(u))
        }
    )


def _count_contradictions(merged: str) -> int:
    if "## Contradictions" not in merged:
        return 0
    section = merged.split("## Contradictions", 1)[1]
    next_h = re.search(r"\n##\s+", section)
    if next_h:
        section = section[: next_h.start()]
    count = 0
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("-"):
            continue
        body = s.lstrip("-").strip().lower()
        if not body or body.startswith("none"):
            continue
        count += 1
    return count


def format_metrics(metrics: dict) -> str:
    lines = [
        "--- fanout metrics ---",
        f"mean QPD: {metrics['mean_qpd']:.3f}",
        f"citation overlap ratio: {metrics['citation_overlap_ratio']:.3f}",
        f"unique authoritative domains: {metrics['unique_domains']}",
        f"contradiction count: {metrics['contradiction_count']}",
        f"total cost: {metrics['total_cost_label']}",
    ]
    if metrics.get("qpd_warning"):
        lines.append(
            f"WARNING: mean QPD {metrics['mean_qpd']:.3f} < {QPD_WARN_THRESHOLD} (retrieval-efficiency gate)"
        )
    lines.append("---")
    return "\n".join(lines) + "\n"


_CHECKPOINT_RE = re.compile(
    r"^(?:single-[0-9a-f]{24}|fanout-[0-9a-f]{24}-(?:auto|n[1-8]))$"
)


def _valid_single_submit(
    submit_path: Path, prompt: str, phash: str, model: str
) -> dict | None:
    if not submit_path.is_file():
        return None
    try:
        sub_data = json.loads(submit_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(sub_data, dict):
        return None
    if "prompt" in sub_data and sub_data.get("prompt") != prompt:
        return None
    if "prompt_hash" in sub_data and sub_data.get("prompt_hash") != phash:
        return None
    if "model" in sub_data and sub_data.get("model") != model:
        return None
    rid = str(sub_data.get("id") or "").strip()
    return sub_data if rid else None


def _load_valid_manifest(
    manifest_path: Path,
    *,
    prompt: str,
    research_model: str,
    expected_n: int | None = None,
) -> dict | None:
    if not manifest_path.is_file():
        return None
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    if loaded.get("prompt") != prompt or loaded.get("model") != research_model:
        return None
    raw_n = loaded.get("n")
    if isinstance(raw_n, bool) or not isinstance(raw_n, int):
        return None
    if expected_n is not None and raw_n != expected_n:
        return None
    if loaded.get("state") == "single_delegated":
        return loaded if raw_n == 1 else None
    if raw_n < 2:
        return None
    selected = loaded.get("selected")
    sub_prompts = loaded.get("sub_prompts")
    if not isinstance(loaded.get("brief"), str):
        return None
    if not isinstance(selected, list) or len(selected) != raw_n:
        return None
    if not isinstance(sub_prompts, list) or len(sub_prompts) != raw_n:
        return None
    return loaded


def _job_identity_matches(
    record: object,
    *,
    key: str,
    question: str,
    sub_hash: str,
    model: str,
) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("key") != key or record.get("question") != question:
        return False
    rec_hash = record.get("sub_prompt_hash")
    if rec_hash is not None and rec_hash != sub_hash:
        return False
    rec_model = record.get("model")
    if rec_model is not None and rec_model != model:
        return False
    return True


def _purge_fanout_artifacts(run_dir: Path) -> None:
    for pattern in ("job-*.json", "report-*.md", "merged.md"):
        for stale in run_dir.glob(pattern):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass


def is_checkpoint_id(token: str) -> bool:
    return bool(_CHECKPOINT_RE.match((token or "").strip()))


def list_checkpoints(workdir: Path) -> list[dict]:
    """Return saved single-shot and fanout checkpoints in `<workdir>/.research/`."""
    rroot = Path(workdir).resolve() / ".research"
    if not rroot.is_dir():
        return []
    entries: list[dict] = []
    for child in rroot.iterdir():
        if not child.is_dir() or not is_checkpoint_id(child.name):
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            mtime = 0.0
        if child.name.startswith("single-"):
            sub_path = child / "submit.json"
            res_path = child / "result.json"
            rep_path = child / "report.md"
            sub_data: dict = {}
            res_data: dict = {}
            if sub_path.is_file():
                try:
                    loaded = json.loads(sub_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        sub_data = loaded
                except (OSError, ValueError):
                    pass
            if res_path.is_file():
                try:
                    loaded = json.loads(res_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        res_data = loaded
                except (OSError, ValueError):
                    pass
            if not sub_data and not res_data:
                continue
            status = str(res_data.get("status") or "")
            if status == "completed" and rep_path.is_file():
                state = "completed"
            elif status:
                state = status
            elif sub_data.get("id"):
                state = "submitted"
            else:
                state = "incomplete"
            entries.append(
                {
                    "id": child.name,
                    "mode": "single",
                    "fanout": None,
                    "n": 1,
                    "state": state,
                    "prompt": str(sub_data.get("prompt") or ""),
                    "model": str(sub_data.get("model") or DEFAULT_RESEARCH_MODEL),
                    "updated_at": mtime,
                }
            )
            continue

        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("prompt"), str):
            continue
        is_auto = child.name.endswith("-auto")
        n_val = int(manifest.get("n") or 0)
        fanout_spec = FANOUT_AUTO if is_auto else n_val
        raw_state = str(manifest.get("state") or "")
        merged_exists = (child / "merged.md").is_file()
        if raw_state == "merged" and merged_exists:
            state = "partial" if manifest.get("missing") else "completed"
        elif raw_state == "single_delegated":
            state = "single_delegated"
        else:
            state = "in_progress"
        entries.append(
            {
                "id": child.name,
                "mode": "fanout",
                "fanout": fanout_spec,
                "n": n_val,
                "state": state,
                "prompt": str(manifest.get("prompt") or ""),
                "model": str(manifest.get("model") or DEFAULT_RESEARCH_MODEL),
                "planner_model": str(
                    manifest.get("planner_model") or DEFAULT_PLANNER_MODEL
                ),
                "updated_at": mtime,
            }
        )
    entries.sort(key=lambda item: (-float(item["updated_at"]), str(item["id"])))
    return entries


def resolve_checkpoint(workdir: Path, checkpoint_id: str) -> dict:
    """Resolve and validate a checkpoint directory by ID under `<workdir>/.research/`."""
    cid = (checkpoint_id or "").strip()
    if not is_checkpoint_id(cid):
        raise ResearchError(f"invalid checkpoint id {checkpoint_id!r}")
    rroot = (Path(workdir).resolve() / ".research").resolve()
    target = (rroot / cid).resolve()
    try:
        target.relative_to(rroot)
    except ValueError as exc:
        raise ResearchError(f"invalid checkpoint id {checkpoint_id!r}") from exc
    if not target.is_dir():
        raise ResearchError(f"checkpoint {cid!r} not found")
    for item in list_checkpoints(workdir):
        if item["id"] == cid:
            if not item.get("prompt"):
                raise ResearchError(
                    f"checkpoint {cid!r} does not store a resumable prompt; pass the question text"
                )
            return item
    raise ResearchError(f"checkpoint {cid!r} is incomplete or unreadable")


def find_prompt_checkpoint(
    workdir: Path,
    prompt: str,
    research_model: str = DEFAULT_RESEARCH_MODEL,
    fanout: int | None = None,
) -> int | None:
    """Locate the fanout setting for an existing checkpoint matching `prompt`."""
    rroot = Path(workdir).resolve() / ".research"
    if not rroot.is_dir():
        raise ResearchError("no saved checkpoint found to resume")
    phash = prompt_hash(prompt, research_model)
    if fanout is not None:
        if fanout == 1:
            single_dir = rroot / f"single-{phash}"
            if _valid_single_submit(single_dir / "submit.json", prompt, phash, research_model) or (
                (single_dir / "result.json").is_file() and (single_dir / "report.md").is_file()
            ):
                return 1
            raise ResearchError("no saved single-shot checkpoint found to resume")
        auto = fanout == FANOUT_AUTO
        clamped = FANOUT_AUTO if auto else clamp_fanout(fanout)[0]
        slug = f"fanout-{phash}-auto" if auto else f"fanout-{phash}-n{clamped}"
        manifest = _load_valid_manifest(
            rroot / slug / "manifest.json",
            prompt=prompt,
            research_model=research_model,
            expected_n=None if auto else clamped,
        )
        if manifest is None:
            raise ResearchError("no saved fanout checkpoint found to resume")
        return clamped

    single_dir = rroot / f"single-{phash}"
    has_single = bool(
        _valid_single_submit(single_dir / "submit.json", prompt, phash, research_model)
        or ((single_dir / "result.json").is_file() and (single_dir / "report.md").is_file())
    )
    fanout_matches: list[int] = []
    auto_manifest = _load_valid_manifest(
        rroot / f"fanout-{phash}-auto" / "manifest.json",
        prompt=prompt,
        research_model=research_model,
        expected_n=None,
    )
    if auto_manifest is not None:
        fanout_matches.append(FANOUT_AUTO)
    for k in range(2, FANOUT_MAX + 1):
        m = _load_valid_manifest(
            rroot / f"fanout-{phash}-n{k}" / "manifest.json",
            prompt=prompt,
            research_model=research_model,
            expected_n=k,
        )
        if m is not None:
            fanout_matches.append(k)

    if has_single and not fanout_matches:
        return None
    if not has_single and len(fanout_matches) == 1:
        return fanout_matches[0]
    if has_single and fanout_matches:
        return None
    if len(fanout_matches) > 1:
        raise ResearchError(
            "multiple fanout checkpoints match this prompt; specify --fanout [N] or --resume=<checkpoint-id>"
        )
    raise ResearchError("no saved checkpoint found to resume")


def run_single_shot(
    prompt: str,
    *,
    api_key: str,
    workdir: Path,
    research_model: str = DEFAULT_RESEARCH_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
    job_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
    poll_interval_s: float | None = None,
    no_cache: bool = False,
    resume: bool = False,
) -> dict:
    """Run or resume a single-shot deep-research job and persist `report.md`."""
    if resume and no_cache:
        raise ResearchError("--resume cannot be combined with --no-cache")
    poll_s = resolve_poll_interval(poll_interval_s)
    phash = prompt_hash(prompt, research_model)
    rroot = Path(workdir).resolve() / ".research"
    run_dir = rroot / f"single-{phash}"
    submit_path = run_dir / "submit.json"
    result_path = run_dir / "result.json"
    report_path = run_dir / "report.md"

    sub_meta = (
        None
        if no_cache
        else _valid_single_submit(submit_path, prompt, phash, research_model)
    )
    saved_result: dict | None = None
    if not no_cache and result_path.is_file():
        if not submit_path.is_file() or sub_meta is not None:
            try:
                loaded = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    saved_result = loaded
            except (OSError, ValueError):
                saved_result = None

    if saved_result is not None and saved_result.get("status") == "completed" and report_path.is_file():
        try:
            md = report_path.read_text(encoding="utf-8")
            return {
                "mode": "single",
                "markdown": md,
                "report_file": report_path,
                "sub_report_files": [],
                "run_dir": run_dir,
                "partial": False,
                "raw_json": saved_result,
            }
        except OSError:
            pass

    if resume and sub_meta is None and saved_result is None:
        raise ResearchError("no saved single-shot checkpoint found to resume")

    run_dir.mkdir(parents=True, exist_ok=True)
    if no_cache:
        for stale in (submit_path, result_path, report_path):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass

    response_id = str(sub_meta.get("id") or "") if sub_meta is not None else ""
    if (
        resume
        and saved_result is not None
        and saved_result.get("status") in ("failed", "cancelled", "incomplete")
    ):
        response_id = ""
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not response_id:
        submitted = submit_research_job(
            api_key,
            prompt,
            model=research_model,
            base_url=base_url,
        )
        if submitted.get("error"):
            raise ResearchError(f"research submit failed: {submitted['error']}")
        response_id = str(submitted.get("id") or "")
        if not response_id:
            raise ResearchError("research submit returned no response id")
        _write_json(
            submit_path,
            {
                "id": response_id,
                "prompt": prompt,
                "prompt_hash": phash,
                "model": research_model,
                "response": submitted,
            },
        )

    started = time.monotonic()
    while True:
        status_data = check_job_status(api_key, response_id, base_url=base_url)
        status = status_data.get("status")
        if status == "completed":
            _write_json(result_path, status_data)
            md = render_single_report(status_data)
            atomic_write_text(report_path, md)
            return {
                "mode": "single",
                "markdown": md,
                "report_file": report_path,
                "sub_report_files": [],
                "run_dir": run_dir,
                "partial": False,
                "raw_json": status_data,
            }
        if status in ("failed", "cancelled", "incomplete"):
            _write_json(result_path, status_data)
            err_detail = status_data.get("error") or status
            raise ResearchError(f"research job {status}: {err_detail}")
        if status_data.get("error") and "status" not in status_data:
            raise ResearchError(f"status check failed: {status_data['error']}")
        elapsed = time.monotonic() - started
        if elapsed >= job_timeout_s:
            raise ResearchError(f"research job timed out after {int(elapsed)}s")
        print(
            f"tool-research: status={status or 'unknown'} — polling again in {poll_s:g}s",
            file=sys.stderr,
        )
        time.sleep(min(poll_s, max(0.01, job_timeout_s - elapsed)))


def run_plan_only(
    prompt: str,
    fanout: int,
    *,
    api_key: str,
    planner_model: str = DEFAULT_PLANNER_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
) -> dict:
    """Run Stage 0 planning + MMR selection without submitting research jobs."""
    auto = fanout == FANOUT_AUTO
    n_source = "recommendation" if auto else "flag"
    n = fanout
    if not auto:
        n, was_clamped = clamp_fanout(n)
        if was_clamped:
            print(
                f"tool-research: clamped --fanout to {n} (allowed {FANOUT_MIN}-{FANOUT_MAX})",
                file=sys.stderr,
            )

    brief, candidates, adversarial, recommended, decomp_resp = brief_and_candidates(
        api_key,
        prompt,
        planner_model=planner_model,
        base_url=base_url,
    )
    inp, out = usage_tokens(decomp_resp)
    planner_cost = est_planner_usd(inp, out)
    if auto:
        n = recommended

    if n <= 1:
        lines = [
            "# Research plan (N=1, single-shot)",
            "",
            "## Brief",
            "",
            brief,
            "",
            "## Selected sub-questions",
            "",
            "(none — N=1 uses the single-shot path)",
            "",
            "## Adversarial (reserved, unused at N=1)",
            "",
            f"- {adversarial['angle']}",
            f"  seed_query: {adversarial['seed_query']}",
            "",
        ]
        selected: list[dict] = []
    else:
        selected = select_with_adversarial(candidates, adversarial, n)
        lines = [
            f"# Research plan (N={n}, source={n_source})",
            "",
            "## Brief",
            "",
            brief,
            "",
            "## Selected sub-questions",
            "",
        ]
        for idx, s in enumerate(selected, start=1):
            tag = " [adversarial]" if s is selected[-1] else ""
            lines.append(f"{idx}. {s['angle']}{tag}")
            lines.append(f"   seed_query: {s['seed_query']}")
        lines.append("")

    md = "\n".join(lines)
    return {
        "mode": "plan",
        "markdown": md,
        "report_file": None,
        "sub_report_files": [],
        "run_dir": None,
        "partial": False,
        "raw_json": {
            "n": n,
            "n_source": n_source,
            "recommended_fanout": recommended,
            "brief": brief,
            "selected": selected,
            "adversarial": adversarial,
            "planner_cost_usd": planner_cost,
        },
    }


def _poll_fanout_jobs(
    api_key: str,
    jobs: dict[int, dict],
    run_dir: Path,
    manifest: dict,
    manifest_path: Path,
    *,
    base_url: str,
    timeout_s: float,
    poll_s: float,
    quorum: int,
) -> tuple[dict[int, dict], dict[int, str]]:
    """Poll background jobs and persist each completion immediately to disk."""
    n = len(jobs)
    completed: dict[int, dict] = {}
    terminal: dict[int, str] = {}
    started_at = {i: time.monotonic() for i in jobs}
    pending = set(jobs)

    for idx in list(pending):
        job_meta = next((j for j in manifest["jobs"] if j["index"] == idx), None)
        if not job_meta:
            continue
        result_path = run_dir / f"job-{idx}-result.json"
        if job_meta.get("state") == "completed" and result_path.is_file():
            completed[idx] = json.loads(result_path.read_text(encoding="utf-8"))
            terminal[idx] = "completed"
            pending.discard(idx)
            print(f"[job-{idx}] already completed (resumed from disk)", file=sys.stderr)

    while pending:
        for idx in list(pending):
            rid = jobs[idx]["id"]
            status_data = check_job_status(api_key, rid, base_url=base_url)
            status = status_data.get("status", "unknown")
            if status == "completed":
                completed[idx] = status_data
                terminal[idx] = "completed"
                pending.discard(idx)
                result_path = run_dir / f"job-{idx}-result.json"
                _write_json(result_path, status_data)
                for j in manifest["jobs"]:
                    if j["index"] == idx:
                        j["state"] = "completed"
                        j["result_file"] = result_path.name
                _write_json(manifest_path, manifest)
                print(f"[job-{idx}] completed (persisted)", file=sys.stderr)
            elif status in ("failed", "cancelled", "incomplete"):
                terminal[idx] = str(status)
                pending.discard(idx)
                fail_path = run_dir / f"job-{idx}-result.json"
                _write_json(fail_path, status_data)
                for j in manifest["jobs"]:
                    if j["index"] == idx:
                        j["state"] = str(status)
                _write_json(manifest_path, manifest)
                print(f"[job-{idx}] {status}", file=sys.stderr)
            elif status_data.get("error") and "status" not in status_data:
                terminal[idx] = "error"
                pending.discard(idx)
                for j in manifest["jobs"]:
                    if j["index"] == idx:
                        j["state"] = "error"
                        j["error"] = str(status_data.get("error"))
                _write_json(manifest_path, manifest)
                print(f"[job-{idx}] error: {status_data.get('error')}", file=sys.stderr)
            else:
                elapsed = time.monotonic() - started_at[idx]
                if elapsed >= timeout_s:
                    terminal[idx] = "timed out"
                    pending.discard(idx)
                    for j in manifest["jobs"]:
                        if j["index"] == idx:
                            j["state"] = "timed_out"
                    _write_json(manifest_path, manifest)
                    print(f"[job-{idx}] timed out after {int(elapsed)}s", file=sys.stderr)
                else:
                    print(
                        f"[job-{idx}] status={status} — polling again in {poll_s:g}s",
                        file=sys.stderr,
                    )

        if not pending:
            break

        if len(completed) >= quorum and all(
            (time.monotonic() - started_at[i]) >= timeout_s for i in pending
        ):
            for idx in list(pending):
                terminal[idx] = "timed out"
                pending.discard(idx)
                for j in manifest["jobs"]:
                    if j["index"] == idx:
                        j["state"] = "timed_out"
            _write_json(manifest_path, manifest)
            break

        if len(completed) >= quorum:
            waits = [
                max(0.0, timeout_s - (time.monotonic() - started_at[i]))
                for i in pending
            ]
            sleep_for = min(poll_s, min(waits)) if waits else 0.0
        else:
            sleep_for = poll_s
        if sleep_for > 0:
            time.sleep(sleep_for)

    return completed, terminal


def run_fanout(
    prompt: str,
    fanout: int,
    *,
    api_key: str,
    workdir: Path,
    research_model: str = DEFAULT_RESEARCH_MODEL,
    planner_model: str = DEFAULT_PLANNER_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
    job_timeout_s: float = DEFAULT_JOB_TIMEOUT_S,
    poll_interval_s: float | None = None,
    no_cache: bool = False,
    resume: bool = False,
) -> dict:
    """Run or resume a multi-angle fanout research pipeline with 3-pass synthesis."""
    if resume and no_cache:
        raise ResearchError("--resume cannot be combined with --no-cache")
    poll_s = resolve_poll_interval(poll_interval_s)
    auto = fanout == FANOUT_AUTO
    n_source = "recommendation" if auto else "flag"
    n = fanout
    if not auto:
        n, was_clamped = clamp_fanout(n)
        if was_clamped:
            print(
                f"tool-research: clamped --fanout to {n} (allowed {FANOUT_MIN}-{FANOUT_MAX})",
                file=sys.stderr,
            )
        if n == 1:
            return run_single_shot(
                prompt,
                api_key=api_key,
                workdir=workdir,
                research_model=research_model,
                base_url=base_url,
                job_timeout_s=job_timeout_s,
                poll_interval_s=poll_s,
                no_cache=no_cache,
                resume=resume,
            )

    base_hash = prompt_hash(prompt, research_model)
    slug = f"fanout-{base_hash}-auto" if auto else f"fanout-{base_hash}-n{n}"
    candidate_dir = Path(workdir).resolve() / ".research" / slug
    candidate_manifest = candidate_dir / "manifest.json"
    planner_cost = 0.0

    manifest: dict | None = None
    run_dir: Path | None = None
    manifest_path: Path | None = None

    if not no_cache:
        loaded = _load_valid_manifest(
            candidate_manifest,
            prompt=prompt,
            research_model=research_model,
            expected_n=None if auto else n,
        )
        if loaded is not None:
            if loaded.get("state") == "single_delegated":
                return run_single_shot(
                    prompt,
                    api_key=api_key,
                    workdir=workdir,
                    research_model=research_model,
                    base_url=base_url,
                    job_timeout_s=job_timeout_s,
                    poll_interval_s=poll_s,
                    no_cache=False,
                    resume=resume,
                )
            merged_file = candidate_dir / "merged.md"
            has_missing = bool(loaded.get("missing"))
            if (
                loaded.get("state") == "merged"
                and merged_file.is_file()
                and (not resume or not has_missing)
            ):
                subs = sorted(candidate_dir.glob("report-*.md"))
                return {
                    "mode": "fanout",
                    "markdown": merged_file.read_text(encoding="utf-8"),
                    "report_file": merged_file,
                    "sub_report_files": subs,
                    "run_dir": candidate_dir,
                    "partial": has_missing,
                    "raw_json": loaded,
                }
            manifest = loaded
            run_dir = candidate_dir
            manifest_path = candidate_manifest

    if resume and manifest is None:
        raise ResearchError("no saved fanout checkpoint found to resume")

    if manifest is not None and run_dir is not None and manifest_path is not None:
        n = int(manifest["n"])
        n_source = str(manifest.get("n_source") or n_source)
        brief = str(manifest["brief"])
        selected = list(manifest["selected"])
        sub_prompts = list(manifest["sub_prompts"])
        print(
            f"tool-research: resuming fanout run at {run_dir} ({n} sub-questions)",
            file=sys.stderr,
        )
    else:
        brief, candidates, adversarial, recommended, decomp_resp = brief_and_candidates(
            api_key,
            prompt,
            planner_model=planner_model,
            base_url=base_url,
        )
        inp, out = usage_tokens(decomp_resp)
        planner_cost += est_planner_usd(inp, out)

        if auto:
            n_source = "recommendation"
            if recommended <= 1:
                auto_dir = research_dir(workdir) / slug
                auto_dir.mkdir(parents=True, exist_ok=True)
                _purge_fanout_artifacts(auto_dir)
                _write_json(
                    auto_dir / "manifest.json",
                    {
                        "prompt": prompt,
                        "n": 1,
                        "n_source": n_source,
                        "recommended_fanout": recommended,
                        "model": research_model,
                        "planner_model": planner_model,
                        "brief": brief,
                        "selected": [],
                        "adversarial": adversarial,
                        "sub_prompts": [],
                        "state": "single_delegated",
                    },
                )
                return run_single_shot(
                    prompt,
                    api_key=api_key,
                    workdir=workdir,
                    research_model=research_model,
                    base_url=base_url,
                    job_timeout_s=job_timeout_s,
                    poll_interval_s=poll_s,
                    no_cache=no_cache,
                    resume=False,
                )
            n, _ = clamp_fanout(recommended)

        run_dir = research_dir(workdir) / (
            f"fanout-{base_hash}-auto" if auto else f"fanout-{base_hash}-n{n}"
        )
        manifest_path = run_dir / "manifest.json"
        run_dir.mkdir(parents=True, exist_ok=True)
        _purge_fanout_artifacts(run_dir)

        tool_budget = max_tool_calls(n)
        selected = select_with_adversarial(candidates, adversarial, n)
        if len(selected) < n:
            raise ResearchError(f"selected {len(selected)} angles; need {n}")
        sub_prompts = [_build_sub_prompt(prompt, brief, s, selected) for s in selected]
        manifest = {
            "prompt": prompt,
            "n": n,
            "n_source": n_source,
            "recommended_fanout": recommended,
            "model": research_model,
            "planner_model": planner_model,
            "brief": brief,
            "selected": selected,
            "adversarial": adversarial,
            "sub_prompts": sub_prompts,
            "max_tool_calls": tool_budget,
            "state": "running",
            "jobs": [],
        }
        _write_json(manifest_path, manifest)

    assert run_dir is not None and manifest_path is not None
    tool_budget = int(manifest.get("max_tool_calls") or max_tool_calls(n))
    job_records: dict[int, dict] = {}
    existing_jobs = {
        int(j["index"]): j
        for j in (manifest.get("jobs") or ())
        if isinstance(j, dict) and isinstance(j.get("index"), int)
    }

    for idx in range(1, n + 1):
        question = str(selected[idx - 1]["angle"])
        sub_prompt = str(sub_prompts[idx - 1])
        sub_hash = prompt_hash(sub_prompt, research_model)
        key = job_key(question, n, research_model)
        cache_file = run_dir / f"job-{idx}.json"
        result_file = run_dir / f"job-{idx}-result.json"
        prior = existing_jobs.get(idx)
        prior_matches = _job_identity_matches(
            prior,
            key=key,
            question=question,
            sub_hash=sub_hash,
            model=research_model,
        )

        if (
            not no_cache
            and prior_matches
            and prior is not None
            and prior.get("state") == "completed"
            and result_file.is_file()
        ):
            cache_ok = True
            if cache_file.is_file():
                try:
                    c_obj = json.loads(cache_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    c_obj = None
                cache_ok = _job_identity_matches(
                    c_obj,
                    key=key,
                    question=question,
                    sub_hash=sub_hash,
                    model=research_model,
                )
            if cache_ok:
                prior["sub_prompt_hash"] = sub_hash
                prior["model"] = research_model
                job_records[idx] = {
                    "id": prior.get("id") or "cached",
                    "key": key,
                    "state": "completed",
                }
                continue

        prior_state = str(prior.get("state") or "") if prior_matches and prior else ""
        dead_prior = prior_state in ("failed", "cancelled", "incomplete", "submit_failed")
        if dead_prior or not prior_matches:
            if dead_prior:
                for stale in (cache_file, result_file):
                    try:
                        stale.unlink(missing_ok=True)
                    except OSError:
                        pass

        if not no_cache and not dead_prior and cache_file.is_file():
            try:
                cached_sub = json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached_sub = None
            if _job_identity_matches(
                cached_sub,
                key=key,
                question=question,
                sub_hash=sub_hash,
                model=research_model,
            ):
                rid = str(cached_sub.get("id") or "")
                if rid:
                    job_records[idx] = {"id": rid, "key": key, "state": "submitted"}
                    existing_jobs[idx] = {
                        "index": idx,
                        "key": key,
                        "id": rid,
                        "question": question,
                        "sub_prompt_hash": sub_hash,
                        "model": research_model,
                        "state": "submitted",
                    }
                    continue
            for stale in (cache_file, result_file):
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    pass

        sub_data = submit_research_job(
            api_key,
            sub_prompt,
            model=research_model,
            base_url=base_url,
            tool_budget=tool_budget,
        )
        if sub_data.get("error"):
            existing_jobs[idx] = {
                "index": idx,
                "key": key,
                "id": "",
                "question": question,
                "sub_prompt_hash": sub_hash,
                "model": research_model,
                "state": "submit_failed",
                "error": str(sub_data.get("error")),
            }
            continue
        rid = str(sub_data.get("id") or "")
        if not rid:
            existing_jobs[idx] = {
                "index": idx,
                "key": key,
                "id": "",
                "question": question,
                "sub_prompt_hash": sub_hash,
                "model": research_model,
                "state": "submit_failed",
                "error": "missing response id",
            }
            continue
        _write_json(
            cache_file,
            {
                "id": rid,
                "index": idx,
                "key": key,
                "question": question,
                "sub_prompt_hash": sub_hash,
                "model": research_model,
                "response": sub_data,
            },
        )
        job_records[idx] = {"id": rid, "key": key, "state": "submitted"}
        existing_jobs[idx] = {
            "index": idx,
            "key": key,
            "id": rid,
            "question": question,
            "sub_prompt_hash": sub_hash,
            "model": research_model,
            "state": "submitted",
        }

    manifest["jobs"] = [existing_jobs[i] for i in range(1, n + 1) if i in existing_jobs]
    _write_json(manifest_path, manifest)

    live = {i: rec for i, rec in job_records.items() if rec.get("id")}
    if not live:
        raise ResearchError("all fanout research job submissions failed")

    quorum = merge_quorum(n)
    completed, terminal = _poll_fanout_jobs(
        api_key,
        live,
        run_dir,
        manifest,
        manifest_path,
        base_url=base_url,
        timeout_s=job_timeout_s,
        poll_s=poll_s,
        quorum=quorum,
    )

    if len(completed) < quorum:
        raise ResearchError(
            f"only {len(completed)}/{n} sub-jobs completed (need >= {quorum} for partial merge)"
        )

    reports: list[tuple[int, str, str, list[str]]] = []
    completed_data: list[dict] = []
    url_lists: list[list[str]] = []
    missing: list[dict] = []
    sub_report_files: list[Path] = []

    for idx in range(1, n + 1):
        question = str(selected[idx - 1]["angle"])
        sub_path = run_dir / f"report-{idx:02d}.md"
        data = completed.get(idx)
        if data is None:
            try:
                sub_path.unlink(missing_ok=True)
            except OSError:
                pass
            reason = terminal.get(idx) or "failed"
            missing.append({"index": idx, "question": question, "reason": reason})
            continue
        body = extract_output_text(data)
        urls = urls_from_response(data, body)
        _write_sub_report(sub_path, idx, question, body, urls)
        sub_report_files.append(sub_path)
        reports.append((idx, question, body, urls))
        completed_data.append(data)
        url_lists.append(urls)

    composed, compose_resp = compose_reports(
        api_key,
        prompt,
        reports,
        missing,
        research_model=research_model,
        base_url=base_url,
    )
    compose_usage = usage_tokens(compose_resp)

    merged, attach_resp = attach_citations(
        api_key,
        composed,
        reports,
        research_model=research_model,
        base_url=base_url,
    )
    attach_usage = usage_tokens(attach_resp)

    check_text, check_resp = citation_check(
        api_key,
        merged,
        reports,
        planner_model=planner_model,
        base_url=base_url,
    )
    if check_text is not None:
        check_usage = usage_tokens(check_resp)
        planner_cost += est_planner_usd(*check_usage)
        merged = merged.rstrip() + "\n\n" + check_text + "\n"

    if missing and "## Missing sub-questions" not in merged:
        missing_lines = ["## Missing sub-questions", ""]
        for m in missing:
            missing_lines.append(
                f"- Sub-question {m['index']} ({m['question']}): {m['reason']}"
            )
        merged = merged.rstrip() + "\n\n" + "\n".join(missing_lines) + "\n"

    if not merged.endswith("\n"):
        merged += "\n"

    merged_path = run_dir / "merged.md"
    atomic_write_text(merged_path, merged)

    submitted_count = sum(
        1
        for j in manifest["jobs"]
        if j.get("state") in ("submitted", "completed") and j.get("id")
    )
    mean_qpd = _mean_qpd(completed_data, url_lists)
    overlap = _citation_overlap_ratio(url_lists)
    domains = _unique_domains(url_lists)
    contra = _count_contradictions(merged)
    research_est = submitted_count * UNVERIFIED_EST_RESEARCH_JOB_USD
    sol_tokens = compose_usage[0] + compose_usage[1] + attach_usage[0] + attach_usage[1]
    total_est = research_est + planner_cost
    cost_label = (
        f"${total_est:.4f} "
        f"(research ${research_est:.2f} unverified estimate; "
        f"planner ${planner_cost:.4f} from reported usage @ "
        f"${EST_PLANNER_INPUT_PER_M:.0f}/${EST_PLANNER_OUTPUT_PER_M:.0f} per 1M; "
        f"sol compose+attach {sol_tokens} tokens reported, $/M unverified)"
    )
    metrics = {
        "mean_qpd": mean_qpd,
        "citation_overlap_ratio": overlap,
        "unique_domains": domains,
        "contradiction_count": contra,
        "total_cost_usd": total_est,
        "total_cost_label": cost_label,
        "qpd_warning": mean_qpd < QPD_WARN_THRESHOLD,
        "research_cost_unverified_usd": research_est,
        "planner_cost_usd": planner_cost,
        "sol_compose_attach_tokens": sol_tokens,
    }
    sys.stderr.write(format_metrics(metrics))

    manifest["state"] = "merged"
    manifest["completed"] = sorted(completed)
    manifest["missing"] = missing
    manifest["metrics"] = metrics
    _write_json(manifest_path, manifest)

    return {
        "mode": "fanout",
        "markdown": merged,
        "report_file": merged_path,
        "sub_report_files": sub_report_files,
        "run_dir": run_dir,
        "partial": bool(missing),
        "raw_json": manifest,
    }
