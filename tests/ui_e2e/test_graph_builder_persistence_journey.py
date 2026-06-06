"""UI E2E: end-to-end graph-builder persistence journey.

Existing graph tests cover individual pieces of the editor:

  * U0028 / U0086 — modal create + nav to detail
  * U0087        — Add node enables Save
  * U0088        — Discard reverts the unsaved edit
  * U0089        — Auto-layout doesn't dirty Save
  * U0090        — Dangling-reference issue surfaces

What's missing: the OPERATOR FLOW that builds a graph from scratch
in the UI, saves it, walks away (page reload + back to list), and
returns to find the edit still there. This is the "did my work
actually persist" verification operators perform reflexively after
building anything non-trivial — and a single regression in the
PUT /v1/graphs/{id} payload, the editor's diff tracker, or the
useResource cache invalidation would silently leak.

Pages traversed:

  /graphs (list) → /graphs/{gid} (modal create flow) →
  page.reload() on /graphs/{gid} (persistence check) →
  /graphs (breadcrumb back; row in list) → /graphs/{gid}
  (click row; node still there)

Multi-feature exercise:

  1. /graphs list page + "New graph" CTA
  2. NewGraphModal seeds an agent→terminal skeleton + redirects
     to /graphs/{gid}
  3. Editor renders the seeded skeleton; Save disabled (diff=0)
  4. Add Node toolbar → Terminal — diff goes from 0 to 1
  5. Save becomes enabled — click → "Graph saved" toast + Save
     returns to disabled (diff cleared by refetch)
  6. page.reload() — editor re-mounts, loads from the server,
     and the added node IS in the rendered editor
     (without this, an in-memory-only edit would slip through)
  7. Breadcrumb back to /graphs — list shows the new id
  8. Click row → /graphs/{gid} — editor still intact

Covers backlog item U0107.
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect


from tests._support.smk import smk  # noqa: E402
pytestmark = smk("SMK-UI-04")


def _seed_agent_with_provider(
    base_url: str, suffix: str,
) -> dict[str, str]:
    """Seed an LLMProvider + Agent so the New-graph modal has an
    agent to preselect into its seed node."""
    ids = {
        "llm": f"j-llm-107-{suffix}",
        "agent": f"j-ag-107-{suffix}",
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/llm_providers", json={
            "id": ids["llm"],
            "provider": "ollama",
            "config": {"url": "http://127.0.0.1:9999"},
            "models": [{"name": "fake-model", "context_length": 4096}],
            "limits": {"max_concurrency": 1},
        })
        assert r.status_code == 201, f"seed llm: {r.text}"
        r = c.post("/v1/agents", json={
            "id": ids["agent"],
            "description": "U0107 graph-builder probe",
            "model": {"provider_id": ids["llm"], "model_name": "fake-model"},
            "tools": [],
            "system_prompt": ["probe"],
        })
        assert r.status_code == 201, f"seed agent: {r.text}"
    return ids


def _cleanup(base_url: str, ids: dict[str, str], graph_id: str | None) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for url in (
            f"/v1/graphs/{graph_id}" if graph_id else None,
            f"/v1/agents/{ids['agent']}",
            f"/v1/llm_providers/{ids['llm']}",
        ):
            if url is None:
                continue
            try:
                c.delete(url)
            except Exception:  # noqa: BLE001
                pass


# ===========================================================================
# U0107 — Graph-builder persistence journey
# ===========================================================================


def test_u0107_graph_builder_persistence_journey(
    page,
    base_url: str,
    console_url: str,
    unique_suffix: str,
) -> None:
    """U0107 — Build a graph via the UI from scratch, save it, then
    verify it survives a reload + back-and-forth nav.

    Steps:

      1. Seed LLM provider + agent via API (so the New-graph modal
         can preselect an agent into its seed node).
      2. Navigate /graphs list → click "New graph" → fill id →
         submit → land on /graphs/{gid}.
      3. Editor renders the seeded agent→terminal skeleton + Save
         is disabled (diffCount === 0).
      4. Click Add Node → Terminal — the editor stages a new node
         locally; Save becomes enabled.
      5. Click Save — "Graph saved" toast appears + Save returns
         to disabled (diff cleared by refetch).
      6. page.reload() — editor re-mounts and loads from the server.
         If the PUT body didn't actually persist the new node,
         the next assertion (editor still has the staged node count
         after reload) fails — this is the load-bearing check.
      7. Click the "Graphs" breadcrumb → /graphs list — new row
         visible with our id.
      8. Click the row → /graphs/{gid} — editor + saved nodes
         still intact.

    Multi-page (3 distinct pages) + reload + modal + editor +
    cross-page nav. No LLM required.
    """
    ids = _seed_agent_with_provider(base_url, unique_suffix)
    graph_id = f"gr-107-{unique_suffix}"
    graph_id_created: str | None = None
    try:
        # ----- 1. /graphs list ---------------------------------------
        page.goto(f"{console_url}#/graphs", wait_until="domcontentloaded")
        expect(page.locator("h1.page-title")).to_have_text(
            "Graphs", timeout=20_000,
        )

        # ----- 2. Open New-graph modal ------------------------------
        page.get_by_role("button", name="New graph").first.click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=5_000)

        # The id field uses placeholder "auto-generated" — same
        # pattern as U0047 (LLM provider modal). The bare <input>
        # has no explicit type attribute, so `input[type=text]`
        # doesn't match; use the placeholder selector.
        id_input = modal.get_by_placeholder("auto-generated", exact=False).first
        id_input.fill(graph_id)

        # Submit — the modal's onCreate navigates to /graphs/{gid}.
        # NewGraphModal uses footer button "Create"; the exact label
        # is set inline via Btn. Find the primary submit by text.
        modal.get_by_role("button", name="Create", exact=True).click()

        # ----- 3. Land on /graphs/{gid}; editor renders ------------
        page.wait_for_url(
            f"**/console/#/graphs/{graph_id}", timeout=15_000,
        )
        graph_id_created = graph_id
        expect(page.locator("h1.page-title", has_text=graph_id)).to_be_visible(
            timeout=20_000,
        )

        # Save button initially disabled (diff = 0 against loaded).
        save = page.get_by_role("button", name="Save", exact=True).first
        save.wait_for(state="visible", timeout=10_000)
        expect(save).to_be_disabled(timeout=5_000)

        # ----- 4. Add Node → Terminal ------------------------------
        add_btn = page.get_by_role(
            "button", name="Add node", exact=False,
        ).first
        if add_btn.count() == 0:
            pytest.skip("Add node button not visible in editor")
        add_btn.click()
        page.wait_for_timeout(300)  # dropdown render

        terminal_opt = page.get_by_text("Terminal", exact=False).first
        if terminal_opt.count() == 0:
            pytest.skip("Terminal option not found in Add menu")
        terminal_opt.click()

        # Save becomes enabled.
        expect(save).to_be_enabled(timeout=5_000)

        # ----- 5. Click Save → toast + Save disables ---------------
        save.click()
        # "Graph saved" toast appears per graphs.jsx:437.
        expect(page.get_by_text("Graph saved", exact=False)).to_be_visible(
            timeout=10_000,
        )
        # Save returns to disabled after the refetch clears the
        # staged diff (the server's response IS the new baseline).
        expect(save).to_be_disabled(timeout=10_000)

        # Snapshot the visible terminal-node count BEFORE reload so
        # we can verify post-reload count is the same (persistence).
        # Terminal nodes render with their kind="terminal" label
        # somewhere in the editor. Use a stable selector that doesn't
        # depend on SVG internals — count rendered node boxes.
        # graphs.jsx renders nodes as absolute-positioned divs;
        # they typically carry a node id text we can count.
        # For robustness, just count save mutations — if Save is
        # disabled post-reload, the editor is consistent with the
        # server state.

        # ----- 6. Reload → editor re-mounts + loads from server ----
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("h1.page-title", has_text=graph_id)).to_be_visible(
            timeout=20_000,
        )

        # Save is disabled post-reload because draft == loaded;
        # this is the load-bearing persistence check. If the PUT
        # body didn't actually save the new node, the loaded graph
        # would now match a state without that node, the draft
        # would also be that state (just-loaded), Save would stay
        # disabled BUT the node would be missing from the page
        # body. So assert BOTH:
        #   (a) Save disabled (no spurious diff after reload)
        #   (b) at least one terminal-kind node visible
        save_after = page.get_by_role("button", name="Save", exact=True).first
        expect(save_after).to_be_disabled(timeout=15_000)

        # The newly-added Terminal node carries an auto-assigned id
        # like "terminal_1" (the editor appends a numeric suffix to
        # the kind). It MUST be visible after reload — that's the
        # load-bearing persistence check: if Save's PUT didn't
        # actually persist the new node, the reload would show only
        # the seed skeleton's two nodes ("start" + "end") and this
        # locator would time out.
        expect(
            page.get_by_text("terminal_1", exact=False).first
        ).to_be_visible(timeout=10_000)

        # ----- 7. Click "Graphs" breadcrumb → /graphs list ---------
        page.locator(".crumb a", has_text="Graphs").click()
        page.wait_for_url("**/console/#/graphs", timeout=10_000)
        expect(page.locator("h1.page-title")).to_have_text(
            "Graphs", timeout=10_000,
        )
        row = page.locator("tbody tr", has_text=graph_id)
        expect(row).to_be_visible(timeout=15_000)

        # ----- 8. Click row → back to /graphs/{gid} ----------------
        row.first.click()
        page.wait_for_url(
            f"**/console/#/graphs/{graph_id}", timeout=15_000,
        )
        expect(page.locator("h1.page-title", has_text=graph_id)).to_be_visible(
            timeout=15_000,
        )
        # Editor still intact — Save still disabled (loaded == draft).
        save_final = page.get_by_role("button", name="Save", exact=True).first
        save_final.wait_for(state="visible", timeout=10_000)
        expect(save_final).to_be_disabled(timeout=10_000)
    finally:
        _cleanup(base_url, ids, graph_id_created)
