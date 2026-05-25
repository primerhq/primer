"""Routing-surface regression tests — now empty post-2026-05-25 prune.

The matrix console runs on a hash router (ui/foundation/router.js).
Hash changes flow through ``hashchange`` events, so browser back/forward
work natively.

U0019 (browser_back_returns_to_agents_list_no_errors) was pruned in
favour of U0105 (operator_troubleshooting_journey), which walks a
strict superset: 7-page traversal that includes a `page.go_back()`
call from /agents back to /sessions and asserts the previous page
re-renders cleanly. U0019's per-page-pair primitive is fully covered
by U0105's interaction-driven flow without the manual seed-and-drill
boilerplate. File kept (not deleted) so the test-id history grep
"U0019" lands here with a clear pointer.
"""

from __future__ import annotations
