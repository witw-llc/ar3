"""a8s wake handler and reply composer for tool-research."""
from __future__ import annotations

import json
import math
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import _runtime as rt

from .engine import (
    DEFAULT_JOB_TIMEOUT_S,
    DEFAULT_PLANNER_MODEL,
    DEFAULT_RESEARCH_MODEL,
    FANOUT_AUTO,
    ResearchError,
    find_prompt_checkpoint,
    is_checkpoint_id,
    resolve_base_url,
    resolve_checkpoint,
    run_fanout,
    run_plan_only,
    run_single_shot,
)
from .outbox import Outbox
from .secretstore import SecretStoreError, resolve_api_key, scrub_text

README = Path(__file__).resolve().parent / "README.md"
DEFAULT_INLINE_MAX = 16384
MAX_CONTEXT_ATTACHMENT_BYTES = 65536
_INT_RE = re.compile(r"^-?\d+$")


class ResearchMessageError(ValueError):
    """Raised when message flags, attachments, or prompt arguments are invalid."""


def _exit_code(sent: int, *, ok: bool) -> int:
    if not rt.print_mode():
        return 0
    if sent != 0 or not ok:
        return 1
    return 0


def _parse_positive_float(raw: str | None, default: float, name: str) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(str(raw).strip())
    except ValueError:
        raise ResearchMessageError(f"invalid {name}: {raw!r}") from None
    if not math.isfinite(val) or val <= 0:
        raise ResearchMessageError(f"{name} must be a positive number")
    return val


def _parse_positive_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        val = int(str(raw).strip())
    except ValueError:
        raise ResearchMessageError(f"invalid {name}: {raw!r}") from None
    if val <= 0:
        raise ResearchMessageError(f"{name} must be a positive integer")
    return val


def _parse_fanout_spec(raw: str | None) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    val = str(raw).strip().lower()
    if val == "auto":
        return FANOUT_AUTO
    try:
        num = int(val)
    except ValueError:
        raise ResearchMessageError(
            f"invalid fanout {raw!r} (expected integer 1..8 or 'auto')"
        ) from None
    if num < 1:
        raise ResearchMessageError("fanout must be >= 1 (or omit N for Stage 0 auto)")
    return num


def parse_research_message(body: str, *, default_fanout: int | None = None) -> dict:
    """Parse leading flags (`--fanout [N]`, `--plan-only`, `--no-cache`, `--resume`, `--json`) and prompt."""
    stripped = (body or "").strip()
    if not stripped:
        raise ResearchMessageError("provide a research question")

    lines = stripped.splitlines()
    first_line = lines[0].strip()
    rest_lines = "\n".join(lines[1:])

    if first_line.startswith("-"):
        try:
            tokens = shlex.split(first_line)
        except ValueError as exc:
            raise ResearchMessageError(f"cannot parse flags: {exc}") from None
    else:
        return {
            "prompt": stripped,
            "fanout": default_fanout,
            "fanout_explicit": False,
            "plan_only": False,
            "no_cache": False,
            "as_json": False,
            "resume": False,
            "checkpoint_id": None,
        }

    fanout = default_fanout
    fanout_explicit = False
    plan_only = False
    no_cache = False
    as_json = False
    resume = False
    checkpoint_id: str | None = None
    idx = 0
    prompt_tokens: list[str] = []

    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "--":
            prompt_tokens.extend(tokens[idx + 1 :])
            break
        if not tok.startswith("-"):
            prompt_tokens.extend(tokens[idx:])
            break
        if tok == "--plan-only":
            plan_only = True
            idx += 1
            continue
        if tok == "--no-cache":
            no_cache = True
            idx += 1
            continue
        if tok == "--json":
            as_json = True
            idx += 1
            continue
        if tok == "--resume":
            resume = True
            idx += 1
            continue
        if tok.startswith("--resume="):
            cid = tok.split("=", 1)[1].strip()
            if not cid:
                raise ResearchMessageError("missing checkpoint id after --resume=")
            resume = True
            checkpoint_id = cid
            idx += 1
            continue
        if tok.startswith("--fanout="):
            val = tok.split("=", 1)[1]
            parsed = _parse_fanout_spec(val)
            if parsed is None:
                raise ResearchMessageError("missing value after --fanout=")
            fanout = parsed
            fanout_explicit = True
            idx += 1
            continue
        if tok == "--fanout":
            if idx + 1 < len(tokens):
                nxt = tokens[idx + 1]
                if nxt.lower() == "auto":
                    fanout = FANOUT_AUTO
                    fanout_explicit = True
                    idx += 2
                    continue
                if _INT_RE.match(nxt):
                    fanout = _parse_fanout_spec(nxt)
                    fanout_explicit = True
                    idx += 2
                    continue
            fanout = FANOUT_AUTO
            fanout_explicit = True
            idx += 1
            continue
        raise ResearchMessageError(f"unknown option {tok!r}")

    first_prompt = " ".join(prompt_tokens).strip()
    if first_prompt and rest_lines.strip():
        prompt = f"{first_prompt}\n{rest_lines}"
    elif first_prompt:
        prompt = first_prompt
    else:
        prompt = rest_lines.strip()

    if resume and no_cache:
        raise ResearchMessageError("--resume cannot be combined with --no-cache")
    if resume and plan_only:
        raise ResearchMessageError("--resume cannot be combined with --plan-only")
    if checkpoint_id and prompt:
        raise ResearchMessageError(
            "pass either --resume=<checkpoint-id> or a research question, not both"
        )
    if not prompt and not checkpoint_id:
        raise ResearchMessageError("provide a research question after options")
    if plan_only and (fanout is None or fanout == 1):
        raise ResearchMessageError("--plan-only requires --fanout (bare or N >= 2)")

    return {
        "prompt": prompt,
        "fanout": fanout,
        "fanout_explicit": fanout_explicit,
        "plan_only": plan_only,
        "no_cache": no_cache,
        "as_json": as_json,
        "resume": resume,
        "checkpoint_id": checkpoint_id,
    }


def _append_attached_context(prompt: str, attachments: list[str]) -> str:
    if not attachments:
        return prompt
    blocks: list[str] = []
    for raw_path in attachments:
        path = Path(raw_path)
        if not path.is_file():
            raise ResearchMessageError(f"attached file not found: {path.name}")
        size = path.stat().st_size
        if size > MAX_CONTEXT_ATTACHMENT_BYTES:
            raise ResearchMessageError(
                f"attached file {path.name} exceeds {MAX_CONTEXT_ATTACHMENT_BYTES} bytes"
            )
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ResearchMessageError(
                f"attached file {path.name} must be UTF-8 text"
            ) from None
        if "\0" in content:
            raise ResearchMessageError(f"attached file {path.name} must be UTF-8 text")
        blocks.append(f"### {path.name}\n\n{content.strip()}")
    return prompt + "\n\n## Attached context\n\n" + "\n\n".join(blocks)


def _utf8_prefix(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def compose_research_reply(
    result: dict,
    *,
    as_json: bool,
    inline_max: int,
    staging_dir: Path,
    sensitive_values: list[str],
) -> tuple[str, tuple[str, ...]]:
    if as_json:
        raw_text = json.dumps(result["raw_json"], indent=2)
        scrubbed = scrub_text(raw_text, sensitive_values)
        if len(scrubbed.encode("utf-8")) <= inline_max:
            return scrubbed, ()
        out_file = staging_dir / "research.json"
        out_file.write_text(scrubbed + "\n", encoding="utf-8")
        head = _utf8_prefix(scrubbed, inline_max).rstrip("\n")
        body = (
            f"(JSON output over the {inline_max}-byte inline limit — full payload attached as research.json)\n\n"
            f"{head}"
        )
        return body, (str(out_file),)

    markdown = scrub_text(str(result["markdown"] or "").strip(), sensitive_values)
    if len(markdown.encode("utf-8")) <= inline_max:
        return markdown, ()

    report_out = staging_dir / "report.md"
    report_out.write_text(markdown + "\n", encoding="utf-8")
    files: list[str] = [str(report_out)]
    for sub in result.get("sub_report_files") or ():
        sub_path = Path(sub)
        if sub_path.is_file():
            staged_sub = staging_dir / sub_path.name
            sub_text = scrub_text(sub_path.read_text(encoding="utf-8"), sensitive_values)
            staged_sub.write_text(sub_text, encoding="utf-8")
            files.append(str(staged_sub))

    head = _utf8_prefix(markdown, inline_max).rstrip("\n")
    attached_names = ", ".join(Path(f).name for f in files)
    body = (
        f"(report over the {inline_max}-byte inline limit — full report attached: {attached_names})\n\n"
        f"{head}"
    )
    return body, tuple(files)


def flush_pending(workdir: Path) -> int:
    """Flush held replies from `<workdir>/.research/pending/`."""
    outbox = Outbox(workdir)
    delivered, remaining = outbox.flush(
        lambda to, body, files: rt.send_reply(to, body, tuple(files))
    )
    if delivered:
        print(f"tool-research: delivered {delivered} held reply/replies", file=sys.stderr)
    if remaining:
        print(f"tool-research: {remaining} reply/replies still held", file=sys.stderr)
    return 0


def handle_wake(
    *,
    seat: str,
    sender: str,
    allow: str,
    message: str,
    workdir: Path | None = None,
    default_model: str | None = None,
    default_planner_model: str | None = None,
    default_fanout_raw: str | None = None,
    job_timeout_raw: str | None = None,
    inline_max_raw: str | None = None,
    api_url: str | None = None,
    attachments: Sequence[str | Path] | None = None,
    force_resume: bool = False,
) -> int:
    """Process one a8s wake or `ar3 tool research` invocation."""
    if not sender:
        print("tool-research: senderless wake refused", file=sys.stderr)
        return 0
    if not rt.allowed(sender, allow or ""):
        print(f"tool-research: {sender!r} is not in ALLOW — refused", file=sys.stderr)
        return 0

    wd = Path(workdir or Path.cwd()).resolve()
    if not rt.print_mode():
        flush_pending(wd)

    try:
        body, verified_attachments = rt.split_verified_attachments(
            message or "", verified=attachments
        )
    except rt.AttachmentProvenanceError as exc:
        sent = rt.send_reply(sender, f"tool-research: {exc}")
        return _exit_code(sent, ok=False)

    if rt.is_help(body):
        return rt.send_reply(
            sender,
            rt.help_reply(
                "tool-research",
                seat,
                README,
                "apps/tools/research/README.md",
            ),
        )

    try:
        api_key, sensitive = resolve_api_key(wd)
    except SecretStoreError as exc:
        sent = rt.send_reply(sender, f"tool-research: {exc}")
        return _exit_code(sent, ok=False)

    research_model = (default_model or DEFAULT_RESEARCH_MODEL).strip() or DEFAULT_RESEARCH_MODEL
    planner_model = (
        (default_planner_model or DEFAULT_PLANNER_MODEL).strip() or DEFAULT_PLANNER_MODEL
    )

    try:
        job_timeout = _parse_positive_float(
            job_timeout_raw, DEFAULT_JOB_TIMEOUT_S, "JOB_TIMEOUT"
        )
        inline_max = _parse_positive_int(
            inline_max_raw, DEFAULT_INLINE_MAX, "INLINE_MAX"
        )
        default_fanout = _parse_fanout_spec(default_fanout_raw)
        parsed = parse_research_message(body, default_fanout=default_fanout)
        resume_mode = bool(parsed["resume"] or force_resume)
        if resume_mode and parsed["no_cache"]:
            raise ResearchMessageError("--resume cannot be combined with --no-cache")
        if resume_mode and parsed["plan_only"]:
            raise ResearchMessageError("--resume cannot be combined with --plan-only")

        cid = parsed["checkpoint_id"]
        if (
            resume_mode
            and cid is None
            and not verified_attachments
            and is_checkpoint_id(parsed["prompt"])
            and (wd / ".research" / parsed["prompt"]).is_dir()
        ):
            cid = parsed["prompt"]

        if cid is not None:
            if verified_attachments:
                raise ResearchMessageError(
                    "cannot attach new files when resuming by checkpoint id"
                )
            ckpt = resolve_checkpoint(wd, cid)
            full_prompt = str(ckpt["prompt"])
            effective_fanout = ckpt["fanout"]
            if not default_model:
                research_model = str(ckpt.get("model") or research_model)
            if not default_planner_model and ckpt.get("planner_model"):
                planner_model = str(ckpt["planner_model"])
        else:
            full_prompt = _append_attached_context(
                parsed["prompt"], verified_attachments
            )
            effective_fanout = parsed["fanout"]
            if resume_mode and not parsed["fanout_explicit"]:
                effective_fanout = find_prompt_checkpoint(
                    wd, full_prompt, research_model, fanout=None
                )
    except (ResearchMessageError, ResearchError) as exc:
        msg = scrub_text(f"tool-research: {exc}", sensitive)
        sent = rt.send_reply(sender, msg)
        return _exit_code(sent, ok=False)

    if not api_key:
        sent = rt.send_reply(
            sender,
            f"tool-research: OPENAI_API_KEY is not set; store it with "
            f"`a8s vars {seat} set secret.OPENAI_API_KEY` (or "
            f"`ar3 tool research --node {seat} secret set OPENAI_API_KEY -`)",
        )
        return _exit_code(sent, ok=False)

    base_url = resolve_base_url(api_url)

    try:
        if parsed["plan_only"]:
            result = run_plan_only(
                full_prompt,
                int(effective_fanout),
                api_key=api_key,
                planner_model=planner_model,
                base_url=base_url,
            )
        elif effective_fanout is None or effective_fanout == 1:
            result = run_single_shot(
                full_prompt,
                api_key=api_key,
                workdir=wd,
                research_model=research_model,
                base_url=base_url,
                job_timeout_s=job_timeout,
                no_cache=bool(parsed["no_cache"]),
                resume=resume_mode,
            )
        else:
            result = run_fanout(
                full_prompt,
                int(effective_fanout),
                api_key=api_key,
                workdir=wd,
                research_model=research_model,
                planner_model=planner_model,
                base_url=base_url,
                job_timeout_s=job_timeout,
                no_cache=bool(parsed["no_cache"]),
                resume=resume_mode,
            )
    except (ResearchError, OSError) as exc:
        msg = scrub_text(f"tool-research: {exc}", sensitive)
        sent = rt.send_reply(sender, msg)
        return _exit_code(sent, ok=False)

    with tempfile.TemporaryDirectory(prefix="tool-research-reply-") as staging:
        reply_body, reply_files = compose_research_reply(
            result,
            as_json=bool(parsed["as_json"]),
            inline_max=inline_max,
            staging_dir=Path(staging),
            sensitive_values=sensitive,
        )
        sent = rt.send_reply(sender, reply_body, reply_files)
        if sent != 0 and not rt.print_mode():
            held = Outbox(wd).keep(sender, reply_body, reply_files)
            print(
                f"tool-research: tell failed; held reply at {held}",
                file=sys.stderr,
            )
            return 0
        return _exit_code(sent, ok=True)
