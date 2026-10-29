"""Pre-flight analysis (AD-10): a pipeline phase between PRD and architecture.

It receives the approved PRD and produces a report of
  * contradictions between requirements (FR-11), found by an agent;
  * common omissions (FR-12), found deterministically by keyword;
  * brainstorm themes no requirement reflects (FR-13), found by an agent.

The report is checkpointed. Its last line, `Decision: ...`, is how the human resolves it:
`revise-prd` reopens the PRD in the editor and re-runs the analysis, `proceed` accepts it.
The default is `revise-prd` while there are blocking findings (any contradiction, or a
missing critical topic), so doing nothing never silently waves a problem through.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from foundry.agent import AgentRequest, AgentRunner
from foundry.context import (
    Artifact,
    ArtifactType,
    CodeFailure,
    EnvFailure,
    RunContext,
)
from foundry.phases import DefaultHooks
from foundry.planning import build_prompt

DECISION_PROCEED = "proceed"
DECISION_REVISE = "revise-prd"
_DECISION_RE = re.compile(r"^\s*decision:\s*(proceed|revise-prd)\s*$", re.IGNORECASE | re.MULTILINE)
_META_RE = re.compile(r"<!--\s*foundry:preflight\s*(\{.*?\})\s*-->", re.DOTALL)


# ---------------------------------------------------------------------------- omissions


@dataclass(frozen=True)
class OmissionCheck:
    key: str
    label: str
    critical: bool
    pattern: re.Pattern[str]
    hint: str


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# A topic counts as addressed if the PRD mentions it at all, including to rule it out
# ("no authentication in v1"). Authentication, error handling and persistence are critical:
# an implementation cannot be planned without deciding them. Logging is advisory.
OMISSION_CHECKS: tuple[OmissionCheck, ...] = (
    OmissionCheck(
        "auth", "Authentication / authorization", True,
        _rx(r"\bauth|\blog[- ]?in\b|\bsign[- ]?(?:in|up)\b|\bcredential|\bpermission|\boauth|\bapi[- ]key|\bpassword"),
        "State who may use the system and how they prove it, or say explicitly that it is out of scope.",
    ),
    OmissionCheck(
        "error_handling", "Error handling", True,
        _rx(r"\berrors?\b|\bfail(?:s|ed|ing|ure|ures)?\b|\bexception|\bretr(?:y|ies|ied|ying)\b|\brecover|\bgraceful"),
        "Describe what happens when things go wrong: invalid input, dependency failures, retries.",
    ),
    OmissionCheck(
        "persistence", "Data persistence", True,
        _rx(r"\bpersist|\bdatabase|\bstorage\b|\bstor(?:e|es|ed|ing)\b|\bsqlite|\bpostgres|\bfile ?system|\bdisk\b|\bsav(?:e|es|ed|ing)\b"),
        "Say where data lives and how long it must survive restarts.",
    ),
    OmissionCheck(
        "logging", "Logging / observability", False,
        # bare "log" must not be the start of "log in" (authentication)
        _rx(r"\blog\b(?![- ]?in\b)|\blogs\b|\blogged\b|\blogging\b|\bobservab|\bmonitor|\bmetrics\b|\baudit"),
        "Decide what is logged and where, so failures can be diagnosed.",
    ),
)


@dataclass(frozen=True)
class Omission:
    key: str
    label: str
    critical: bool
    hint: str


def find_omissions(prd_text: str) -> list[Omission]:
    return [
        Omission(check.key, check.label, check.critical, check.hint)
        for check in OMISSION_CHECKS
        if not check.pattern.search(prd_text)
    ]


# ------------------------------------------------------------------- agent findings


@dataclass(frozen=True)
class Contradiction:
    requirements: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class TraceabilityGap:
    theme: str
    description: str


def _extract_json(text: str) -> Any:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise CodeFailure("pre-flight agent did not return a JSON object")
        candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise CodeFailure(f"pre-flight agent returned invalid JSON: {exc}") from exc


def _items(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CodeFailure(f"pre-flight agent returned a malformed {key!r} list")
    return value


def _text(item: dict[str, Any], key: str, where: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CodeFailure(f"pre-flight agent returned an entry in {where!r} without {key!r}")
    return value.strip()


def parse_agent_findings(text: str) -> tuple[list[Contradiction], list[TraceabilityGap]]:
    data = _extract_json(text)
    if not isinstance(data, dict) or not ({"contradictions", "traceability_gaps"} & data.keys()):
        raise CodeFailure("pre-flight agent response has neither 'contradictions' nor 'traceability_gaps'")

    contradictions = []
    for item in _items(data, "contradictions"):
        reqs = item.get("requirements", [])
        if not isinstance(reqs, list) or not all(isinstance(r, str) for r in reqs):
            raise CodeFailure("pre-flight agent returned malformed 'requirements' in a contradiction")
        contradictions.append(Contradiction(tuple(reqs), _text(item, "description", "contradictions")))
    gaps = [
        TraceabilityGap(_text(item, "theme", "traceability_gaps"), _text(item, "description", "traceability_gaps"))
        for item in _items(data, "traceability_gaps")
    ]
    return contradictions, gaps


# ------------------------------------------------------------------------ the report


@dataclass(frozen=True)
class Findings:
    contradictions: list[Contradiction]
    omissions: list[Omission]
    gaps: list[TraceabilityGap]

    @property
    def blocking(self) -> int:
        return len(self.contradictions) + sum(1 for o in self.omissions if o.critical)

    @property
    def default_decision(self) -> str:
        return DECISION_REVISE if self.blocking else DECISION_PROCEED


@dataclass(frozen=True)
class PreflightSummary:
    contradictions: int
    omissions: int
    gaps: int
    blocking: int
    decision: str


def render_report(findings: Findings, prd: Artifact) -> str:
    meta = json.dumps(
        {
            "contradictions": len(findings.contradictions),
            "omissions": len(findings.omissions),
            "traceability_gaps": len(findings.gaps),
            "blocking": findings.blocking,
            "prd_hash": prd.sha256_hash,
        }
    )
    lines = [
        "# Pre-flight Report",
        "",
        f"<!-- foundry:preflight {meta} -->",
        "",
        "## Summary",
        "",
        f"- Contradictions: {len(findings.contradictions)}",
        f"- Omissions: {len(findings.omissions)} "
        f"({sum(1 for o in findings.omissions if o.critical)} critical)",
        f"- Traceability gaps: {len(findings.gaps)}",
        f"- Blocking findings: {findings.blocking}",
        "",
        "## Contradictions",
        "",
    ]
    lines += [
        f"{n}. **{' vs '.join(c.requirements) or 'Requirements'}**: {c.description}"
        for n, c in enumerate(findings.contradictions, 1)
    ] or ["None found."]
    lines += ["", "## Omissions", ""]
    lines += [
        f"- **{o.label}** ({'critical' if o.critical else 'advisory'}): not mentioned in the PRD. {o.hint}"
        for o in findings.omissions
    ] or ["None found."]
    lines += ["", "## Traceability gaps", ""]
    lines += [f"- **{g.theme}**: {g.description}" for g in findings.gaps] or ["None found."]
    lines += [
        "",
        "## Resolution",
        "",
        "Edit the last line. `revise-prd` opens the PRD in your editor and then re-runs this "
        "analysis; `proceed` accepts the PRD as it is.",
        "",
        f"Decision: {findings.default_decision}",
        "",
    ]
    return "\n".join(lines)


def summarize_report(report: str) -> PreflightSummary:
    """Read the counts and the human's decision back out of the (possibly edited) report."""
    meta: dict[str, Any] = {}
    match = _META_RE.search(report)
    if match:
        try:
            parsed = json.loads(match.group(1))
            meta = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            meta = {}

    def count(key: str) -> int:
        value = meta.get(key, 0)
        return value if isinstance(value, int) and value >= 0 else 0

    blocking = count("blocking")
    decisions = _DECISION_RE.findall(report)
    decision = decisions[-1].lower() if decisions else (DECISION_REVISE if blocking else DECISION_PROCEED)
    return PreflightSummary(
        contradictions=count("contradictions"),
        omissions=count("omissions"),
        gaps=count("traceability_gaps"),
        blocking=blocking,
        decision=decision,
    )


# -------------------------------------------------------------------------- the phase

_ANALYSIS_TASK = """\
Analyse the PRD below against the brainstorm it came from.

1. **Contradictions**: two or more requirements that cannot all be satisfied at once
   (conflicting behaviour, numbers, or constraints). Report real conflicts only, not style.
   Cite requirement IDs (FR-1, NFR-2, ...) where the PRD has them.
2. **Traceability gaps**: themes, ideas or decisions in the brainstorm that no requirement in
   the PRD reflects and that the PRD does not explicitly descope.

Respond with ONLY a JSON object in this exact shape (empty lists if nothing is found):

```json
{"contradictions": [{"requirements": ["FR-1", "FR-4"], "description": "..."}],
 "traceability_gaps": [{"theme": "...", "description": "..."}]}
```"""


class PreflightPhase(DefaultHooks):
    """FR-11, FR-12, FR-13 as a pipeline phase (AD-10)."""

    name = "preflight"
    checkpoint = True
    revise_target = "prd"

    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    async def run(self, input: Artifact, ctx: RunContext) -> Artifact:
        brainstorm = ctx.upstream.get("brainstorm")
        if brainstorm is None:
            raise EnvFailure("pre-flight needs the approved brainstorm to check traceability")

        prompt = build_prompt(
            "pre-flight analysis of the PRD",
            None,
            deliverable="Your final message must be only the JSON object described below.",
            instructions=_ANALYSIS_TASK,
            prd=input.content,
            approved_brainstorm=brainstorm.content,
        )
        result = await self.runner.run(AgentRequest(name=self.name, prompt=prompt), ctx)
        contradictions, gaps = parse_agent_findings(result.text)
        findings = Findings(contradictions, find_omissions(input.content), gaps)
        return Artifact(render_report(findings, input), ArtifactType.PREFLIGHT)

    # ---- hooks

    def requests_revision(self, approved: Artifact) -> bool:
        return summarize_report(approved.content).decision == DECISION_REVISE

    def checkpoint_fields(self, approved: Artifact) -> dict[str, Any]:
        summary = summarize_report(approved.content)
        return {
            "contradiction_count": summary.contradictions,
            "omission_count": summary.omissions,
            "traceability_gap_count": summary.gaps,
            "blocking_findings": summary.blocking,
            "resolution": summary.decision,
            "proceeded_with_blocking_findings": summary.decision == DECISION_PROCEED
            and summary.blocking > 0,
        }
