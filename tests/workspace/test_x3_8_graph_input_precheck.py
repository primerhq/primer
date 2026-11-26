"""X3.8: the graph input pre-check (session create + invoke_graph, the one
``validate_graph_input``) answers a stored ``input_schema`` that is not a valid
JSON schema with a clear 422, not a 500; its instance bound is the app's
request-body cap (a body the body-cap middleware admits is not refused at the
historical 1 MiB default); and it validates on its own small thread pool, not
the interactive user-regex pool that tap LIKE reads share.
"""

import time

import pytest

from primer.model.except_ import ValidationError
from primer.workspace.session_factory import validate_graph_input


@pytest.mark.asyncio
async def test_a_stored_input_schema_that_is_not_a_valid_json_schema_is_refused_with_a_clear_422() -> None:
    # ``required`` must be a list: jsonschema's own check_schema raises
    # SchemaError for this, which the pre-check used to let escape as a 500.
    with pytest.raises(ValidationError) as excinfo:
        await validate_graph_input({"name": "x"}, {"required": "name"})
    assert "not a valid JSON schema" in str(excinfo.value)


@pytest.mark.asyncio
async def test_graph_input_at_the_body_cap_is_admitted_and_past_it_is_refused() -> None:
    schema = {"type": "object", "properties": {"s": {"type": "string"}}}
    # 1.5 MiB: past the historical 1 MiB default the pre-check used to refuse
    # here, within the 32 MiB body cap the middleware admits.
    await validate_graph_input({"s": "a" * (1500 * 1024)}, schema)
    # Past the cap: refused, with the cap named.
    with pytest.raises(ValidationError) as excinfo:
        await validate_graph_input({"s": "a" * (33 * 1024 * 1024)}, schema)
    assert "32 MiB" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_precheck_validates_on_its_own_pool_not_the_tap_shared_one() -> None:
    from primer.common.user_regex import _INTERACTIVE

    # Saturate the interactive pool (4 workers) with 2 s sleeps: a pre-check
    # that still ran on it would queue behind them for 2 s or more; its own
    # pool does not.
    sleepers = [_INTERACTIVE.submit(time.sleep, 2.0) for _ in range(4)]
    try:
        started = time.monotonic()
        await validate_graph_input({"s": "x"}, {"type": "object"})
        elapsed = time.monotonic() - started
    finally:
        for s in sleepers:
            s.result(timeout=10)
    assert elapsed < 1.0, f"the pre-check waited {elapsed:.2f} s on the shared pool"
