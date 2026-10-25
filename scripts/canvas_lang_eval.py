"""Canvas Lang evaluation: measure validity and repair rate on 20 scripted canvases.

The pivot-05 P5 report needs numbers for Canvas Lang: the share of first applies that a frontier model gets valid on the
first try, the share of invalid ones the model repairs within the turn, the pass rate of the per-case structure checks, and the
token cost, all per band (simple / medium / complex). The live run is lead-run: it creates a temporary agent on a given profile
that holds the canvas tools, runs each of the 20 scripted cases in its own session (the update prompt as a second message),
reads the session's message log and the canvas, and writes the report.

The pure core (the part tests/tooling/test_canvas_lang_eval.py pins) is load_cases, score_case, summarize and
render_markdown. The live run (main) drives the REST API; it is not covered by the unit tests.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# The three difficulty bands, in order.
BANDS = ("simple", "medium", "complex")

# The prompt the temporary agent runs on: build what the user asks, fix what the apply reports, and finish.
AGENT_PROMPT = (
    "Build exactly what the user asks on the canvas with canvas_apply, "
    "fix any errors it reports, then reply DONE."
)

# A canvas_apply tool_result is a failure when its output is the tool's error envelope (a JSON body with a type that is an
# error) or when the record's error flag is set.
_FAIL_TYPES = frozenset({"validation-error", "conflict", "unsupported-surface", "not-found", "forbidden"})


@dataclass
class EvalCase:
    """One scripted canvas: what to build, and the structural checks the built canvas must meet."""

    id: str
    band: str
    prompt: str
    expect: dict = field(default_factory=dict)
    update_prompt: str | None = None


@dataclass
class CaseScore:
    """The scored outcome of one case: the applies' validity and repair, the checks, and the token cost."""

    case_id: str
    band: str
    first_apply_valid: bool
    apply_calls: int
    failed_apply_calls: int
    repaired: bool
    checks_passed: bool
    input_tokens: int
    output_tokens: int


def load_cases(path) -> list[EvalCase]:
    """Load the 20 scripted cases from a JSON file, refusing any file that is not exactly 20 unique ids across the three bands."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("the cases file must be a JSON list")
    ids = [c.get("id") for c in data]
    if len(ids) != 20:
        raise ValueError(f"the cases file must hold exactly 20 cases, got {len(ids)}")
    if len(set(ids)) != 20:
        raise ValueError("the cases file must hold 20 unique ids")
    cases: list[EvalCase] = []
    for c in data:
        band = c.get("band")
        if band not in BANDS:
            raise ValueError(f"unknown band {band!r} for case {c.get('id')!r}")
        if not c.get("prompt"):
            raise ValueError(f"case {c.get('id')!r} has no prompt")
        cases.append(
            EvalCase(
                id=c["id"],
                band=band,
                prompt=c["prompt"],
                expect=c.get("expect") or {},
                update_prompt=c.get("update_prompt"),
            )
        )
    return cases


def _apply_failed(result_payload: dict) -> bool:
    """Whether a canvas_apply tool_result is a failure: the tool's error envelope in the output, or the record's error flag."""
    output = result_payload.get("output")
    body = None
    if isinstance(output, str):
        try:
            body = json.loads(output)
        except (json.JSONDecodeError, TypeError, ValueError):
            body = None
    elif isinstance(output, dict):
        body = output
    if isinstance(body, dict) and body.get("type") in _FAIL_TYPES:
        return True
    return bool(result_payload.get("error"))


def _checks_passed(expect: dict, canvas: dict) -> bool:
    """Whether the built canvas meets the case's structural checks: the required types are present and the element cap is held."""
    types: dict[str, int] = {}
    for el in (canvas.get("elements") or {}).values():
        t = (el or {}).get("type")
        if t:
            types[t] = types.get(t, 0) + 1
    for t, n in (expect.get("types_at_least") or {}).items():
        if types.get(t, 0) < int(n):
            return False
    max_elements = expect.get("max_elements")
    if max_elements is not None:
        count = canvas.get("element_count")
        if count is None:
            count = len(canvas.get("elements") or {})
        if int(count) > int(max_elements):
            return False
    return True


def score_case(case: EvalCase, records: list[dict], canvas: dict) -> CaseScore:
    """Score one case from its session's message log and its final canvas.

    A failed apply followed by a successful one is a repair; the first apply's validity is whether it was not a failure; the
    tokens sum the llm_call records; the checks read the final canvas against the case's expect.
    """
    call_ids: list[str] = []
    seen: set[str] = set()
    results: dict[str, dict] = {}
    in_tokens = 0
    out_tokens = 0
    for rec in records:
        kind = rec.get("kind")
        payload = rec.get("payload") or {}
        if kind == "tool_call":
            name = str(payload.get("name", ""))
            if "canvas_apply" in name:
                cid = payload.get("id")
                if cid is not None and cid not in seen:
                    seen.add(cid)
                    call_ids.append(cid)
        elif kind == "tool_result":
            cid = payload.get("call_id")
            if cid in seen:
                results[cid] = payload
        elif kind == "llm_call":
            in_tokens += int(payload.get("input_tokens") or 0)
            out_tokens += int(payload.get("output_tokens") or 0)

    failed = [cid in results and _apply_failed(results[cid]) for cid in call_ids]
    failed_apply_calls = sum(1 for f in failed if f)
    # A repair is a successful apply that follows at least one failed apply.
    seen_failure = False
    repaired = False
    for f in failed:
        if f:
            seen_failure = True
        elif seen_failure:
            repaired = True
            break
    first_apply_valid = bool(call_ids) and not failed[0]
    return CaseScore(
        case_id=case.id,
        band=case.band,
        first_apply_valid=first_apply_valid,
        apply_calls=len(call_ids),
        failed_apply_calls=failed_apply_calls,
        repaired=repaired,
        checks_passed=_checks_passed(case.expect, canvas),
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )


def summarize(scores: list[CaseScore]) -> dict:
    """The report's rates (validity, repair, pass) and the per-band breakdown."""

    def _rate(flags: list[bool]) -> float:
        return round(sum(1 for f in flags if f) / len(flags), 4) if flags else 0.0

    out: dict = {
        "cases": len(scores),
        "validity_rate": _rate([s.first_apply_valid for s in scores]),
        "repair_rate": _rate([s.repaired for s in scores]),
        "pass_rate": _rate([s.checks_passed for s in scores]),
        "input_tokens": sum(s.input_tokens for s in scores),
        "output_tokens": sum(s.output_tokens for s in scores),
        "per_band": {},
    }
    for band in BANDS:
        band_scores = [s for s in scores if s.band == band]
        out["per_band"][band] = {
            "cases": len(band_scores),
            "validity_rate": _rate([s.first_apply_valid for s in band_scores]),
            "pass_rate": _rate([s.checks_passed for s in band_scores]),
        }
    return out


def render_markdown(summary: dict, scores: list[CaseScore]) -> str:
    """The Markdown report: the headline rates, the per-band table, and a row per case."""
    lines = [
        "# Canvas Lang evaluation",
        "",
        f"- Cases: {summary['cases']}",
        f"- Validity rate: {summary['validity_rate']:.0%}",
        f"- Repair rate: {summary['repair_rate']:.0%}",
        f"- Pass rate: {summary['pass_rate']:.0%}",
        f"- Tokens: {summary['input_tokens']} in, {summary['output_tokens']} out",
        "",
        "## By band",
        "",
        "| Band | Cases | Validity | Pass |",
        "|---|---|---|---|",
    ]
    for band in BANDS:
        b = summary["per_band"][band]
        lines.append(f"| {band} | {b['cases']} | {b['validity_rate']:.0%} | {b['pass_rate']:.0%} |")
    lines += ["", "## Per case", "", "| Case | Band | Applies | Failed | Repaired | Checks | In | Out |", "|---|---|---|---|---|---|---|---|"]
    for s in scores:
        lines.append(
            f"| {s.case_id} | {s.band} | {s.apply_calls} | {s.failed_apply_calls} "
            f"| {'yes' if s.repaired else 'no'} | {'yes' if s.checks_passed else 'no'} | {s.input_tokens} | {s.output_tokens} |"
        )
    return "\n".join(lines) + "\n"


# ----- The live run (lead-run before release; not covered by the unit tests) -----


def _wait_turn_done(client: httpx.Client, session_id: str, *, timeout_s: float) -> None:
    """Poll the session's message log until the turn is terminal (a done record) or the session parks."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"/v1/sessions/{session_id}/messages", params={"limit": 500})
        r.raise_for_status()
        items = r.json().get("items", [])
        if any(rec.get("kind") == "done" for rec in items):
            return
        s = client.get(f"/v1/sessions/{session_id}")
        if s.status_code == 200 and s.json().get("parked_status"):
            return
        time.sleep(0.5)
    raise TimeoutError(f"session {session_id} did not finish within {timeout_s:.0f}s")


def _read_messages(client: httpx.Client, session_id: str) -> list[dict]:
    r = client.get(f"/v1/sessions/{session_id}/messages", params={"limit": 500})
    r.raise_for_status()
    return r.json().get("items", [])


def _read_canvas(client: httpx.Client, workspace_id: str, session_id: str) -> dict:
    r = client.get(f"/v1/workspaces/{workspace_id}/sessions/{session_id}/canvas")
    r.raise_for_status()
    return r.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Canvas Lang evaluation.")
    parser.add_argument("--base-url", required=True, help="the API base URL")
    parser.add_argument("--token", required=True, help="a bearer token")
    parser.add_argument("--profile", required=True, help="the model profile id the temporary agent uses")
    parser.add_argument("--workspace", default=None, help="a workspace id (default: the first workspace)")
    parser.add_argument("--cases", default=None, help="a cases file (default: the one next to this script)")
    parser.add_argument("--out", default="canvas-lang-report.md", help="the report path")
    parser.add_argument("--keep", action="store_true", help="keep the temporary agent and sessions")
    parser.add_argument("--timeout", type=float, default=300.0, help="per-turn timeout in seconds")
    args = parser.parse_args(argv)

    cases_path = Path(args.cases) if args.cases else Path(__file__).resolve().parent / "canvas_lang_eval_cases.json"
    cases = load_cases(cases_path)

    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=60.0) as client:
        if args.workspace:
            workspace_id = args.workspace
        else:
            r = client.get("/v1/workspaces")
            r.raise_for_status()
            items = r.json().get("items", r.json() if isinstance(r.json(), list) else [])
            if not items:
                raise SystemExit("no workspace found; pass --workspace")
            workspace_id = items[0].get("id") if isinstance(items[0], dict) else items[0]

        agent_id = f"canvas-lang-eval-{int(time.time())}"
        r = client.post(
            "/v1/agents",
            json={
                "id": agent_id,
                "description": "Temporary Canvas Lang evaluation agent.",
                "model": {"profile_id": args.profile},
                "tools": ["canvas__canvas_apply", "canvas__canvas_get"],
                "prompt": AGENT_PROMPT,
            },
        )
        r.raise_for_status()

        scores: list[CaseScore] = []
        session_ids: list[str] = []
        try:
            for case in cases:
                r = client.post(
                    f"/v1/workspaces/{workspace_id}/sessions",
                    json={"binding": {"kind": "agent", "agent_id": agent_id}},
                )
                r.raise_for_status()
                session_id = r.json()["id"]
                session_ids.append(session_id)

                r = client.post(f"/v1/sessions/{session_id}/messages", json={"content": case.prompt})
                r.raise_for_status()
                _wait_turn_done(client, session_id, timeout_s=args.timeout)

                if case.update_prompt:
                    r = client.post(f"/v1/sessions/{session_id}/messages", json={"content": case.update_prompt})
                    r.raise_for_status()
                    _wait_turn_done(client, session_id, timeout_s=args.timeout)

                records = _read_messages(client, session_id)
                canvas = _read_canvas(client, workspace_id, session_id)
                scores.append(score_case(case, records, canvas))
                print(f"{case.id} ({case.band}): scored")
        finally:
            if not args.keep:
                for sid in session_ids:
                    client.delete(f"/v1/sessions/{sid}")
                client.delete(f"/v1/agents/{agent_id}")

        report = render_markdown(summarize(scores), scores)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"report written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
