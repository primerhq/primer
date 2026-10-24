"""Journeys: an edit of a Channel, Workspace or Semantic Search Storage provider keeps every field it did not change and keeps the stored secret (audit G2-26).

These three classes register through forms of their own hosted in the Register overlay (plan 02 Tasks 17 to 19), and their edits are where real rows were
broken: a channel provider edit that left its token blank answered 422 on every platform (audit G2-01: the form sends the served mask back, as its placeholder
promises, and the token rules refused the mask's shape before the stored token was restored), an emptied optional workspace field could not be cleared (G2-06),
and the SSP edit tested its draft with the mask (G2-07). No journey edited one of these providers, which is how G2-01 shipped.

Each journey seeds a row with every field set over REST, opens it the way a person does (Providers at the type, the card's Open, the detail page's Edit), changes
what a person would change, saves, and reads the row back: the changed fields changed, every other field is as it was, and the secret is still the served mask.
A GET never shows a stored secret, so "the stored secret was kept" is said in the strongest form the server allows:

* the PUT the console sent carries the served mask for the secret (which ``preserve_masked_secrets`` restores; an empty or missing secret would replace it);
* for a channel provider, a second PUT of the served row answers 200: after the restore, ``channel_token_problem`` applies the platform's token rule to the
  value that will be stored, so a stored mask (or anything else that is not a token) would answer 422.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from primer.common.url_userinfo import MASK
from tests._support.smk import smk
from tests.ui_e2e._studio_helpers import open_provider_catalog

pytestmark = [smk("SMK-UI-06", status="partial"), pytest.mark.ui_e2e]


def _open_detail(page: Page, console_url: str, cls: str, row_id: str, section: str) -> None:
    """Providers at the type, the row's card found through the filter (a busy install has more than one page of cards), its Open, and the detail page."""
    open_provider_catalog(page, console_url, cls=cls)
    expect(page.get_by_test_id(f"provider-body-{cls}")).to_be_visible(timeout=20_000)
    page.get_by_test_id("pc-filter").fill(row_id)
    expect(page.get_by_test_id(f"pk-card:{cls}:{row_id}")).to_be_visible(timeout=20_000)
    page.get_by_test_id(f"pk-card-open:{cls}:{row_id}").click()
    expect(page.get_by_test_id(section)).to_be_visible(timeout=15_000)


def _edit_overlay(page: Page, edit_testid: str, row_id: str) -> Locator:
    page.get_by_test_id(edit_testid).click()
    overlay = page.get_by_test_id("pc-register")
    expect(overlay).to_be_visible(timeout=15_000)
    expect(overlay).to_contain_text(f"Edit {row_id}")
    return overlay


def _save(page: Page, overlay: Locator, plural: str, row_id: str):
    """Press Save and return the server's answer to the PUT it sent."""
    with page.expect_response(lambda resp: resp.request.method == "PUT" and resp.url.split("?")[0].endswith(f"/v1/{plural}/{row_id}")) as answered:
        overlay.get_by_role("button", name="Save", exact=True).click()
    return answered.value


def _with(row: dict, path: tuple[str, ...], value) -> dict:
    """A copy of ``row`` with the value at ``path`` replaced."""
    out = dict(row)
    if len(path) == 1:
        out[path[0]] = value
    else:
        out[path[0]] = _with(row[path[0]], path[1:], value)
    return out


# ---- Channels ----------------------------------------------------------------------------------------------------------------------------------------------

# Per platform: the config the row is seeded with, the field the edit changes and its new value, and how a person changes it in the form.
CHANNEL_EDITS = {
    "telegram": (
        {"bot_token": "123456:" + "T" * 36, "poll_timeout_seconds": 25},
        "poll_timeout_seconds", 40,
        lambda overlay: overlay.get_by_label("Poll timeout (s)", exact=True).fill("40"),
    ),
    "discord": (
        {"bot_token": "D" * 60, "enable_dms": True},
        "enable_dms", False,
        lambda overlay: overlay.get_by_role("checkbox", name="Request the dm_messages intent", exact=True).uncheck(),
    ),
}


@pytest.mark.parametrize("platform", sorted(CHANNEL_EDITS))
def test_a_channel_provider_edit_keeps_the_stored_token(page: Page, base_url: str, console_url: str, unique_suffix: str, platform: str) -> None:
    seed_config, field, value, change = CHANNEL_EDITS[platform]
    cid = f"reg-ch-{platform}-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/channel_providers", json={"id": cid, "provider": platform, "config": seed_config})
        assert r.status_code == 201, r.text
        before = c.get(f"/v1/channel_providers/{cid}").json()
    try:
        assert before["config"]["bot_token"] == MASK, before
        _open_detail(page, console_url, "channel", cid, "ch-section-config")
        overlay = _edit_overlay(page, "ch-detail-edit", cid)
        token = overlay.get_by_label("Bot token", exact=True)
        expect(token).to_have_value("")   # the mask is never put in the box
        expect(token).to_have_attribute("placeholder", "Leave blank to keep the stored secret")
        change(overlay)

        answer = _save(page, overlay, "channel_providers", cid)
        assert answer.status == 200, f"the edit was refused (audit G2-01 answered 422 here): {answer.status} {answer.text()}"
        sent = answer.request.post_data_json
        assert sent["config"]["bot_token"] == MASK, f"the console did not send the served mask back for the blank token: {sent['config']}"
        assert sent["config"][field] == value, sent["config"]
        expect(overlay).not_to_be_visible(timeout=15_000)
        expect(page.get_by_test_id("ch-section-config")).to_contain_text(str(value).lower() if isinstance(value, bool) else str(value))

        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            after = c.get(f"/v1/channel_providers/{cid}").json()
            assert after == _with(before, ("config", field), value), f"the edit changed a field it did not show:\nbefore {before}\nafter  {after}"
            # the stored token is a real token: the served row PUT back is restored and then judged by the platform's token rule
            again = c.put(f"/v1/channel_providers/{cid}", json={"id": cid, "provider": platform, "config": after["config"]})
            assert again.status_code == 200, f"the stored token is no longer a {platform} token: {again.status_code} {again.text}"
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/channel_providers/{cid}")


# ---- Workspace providers -------------------------------------------------------------------------------------------------------------------------------------

def test_a_workspace_provider_edit_keeps_every_field_and_the_stored_token(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    """A Kubernetes provider reached through a service-account token and a Gateway API HTTPRoute (the reachability the old form rewrote to ingress): the edit
    changes the external port and empties the listener, which the server must store as none (audit G2-06), and keeps everything else."""
    wp = f"reg-wp-{unique_suffix}"
    seed = {
        "id": wp,
        "provider": "kubernetes",
        "config": {
            "kind": "kubernetes",
            "variant": "system",
            "connection": {
                "kind": "service_account_token",
                "apiserver_url": "https://kube-journey.invalid:6443",
                "ca_data": "-----BEGIN CERTIFICATE-----\nam91cm5leQ==\n-----END CERTIFICATE-----",
                "token": f"sa-token-{unique_suffix}",
                "namespace": "journey-sa",
            },
            "namespace": f"journey-{unique_suffix}",
            "reachability": {
                "kind": "gateway_httproute",
                "gateway": {"name": "journey-gw", "namespace": "gw-system", "section_name": "https"},
                "routing": {"kind": "hostname", "hostname_template": "{workspace_id}.journey.invalid"},
                "scheme": "wss",
                "external_port": 443,
                "backend_port": 6000,
            },
            "image_pull_secrets": ["journey-regcred"],
        },
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/workspace_providers", json=seed)
        assert r.status_code == 201, r.text
        before = c.get(f"/v1/workspace_providers/{wp}").json()
    try:
        assert before["config"]["connection"]["token"] == MASK, before["config"]["connection"]
        _open_detail(page, console_url, "workspace", wp, "wp-section-config")
        overlay = _edit_overlay(page, "wp-detail-edit", wp)
        expect(overlay.get_by_label(re.compile(r"^token\b"))).to_have_value(MASK)   # the form holds the served mask, never the token
        port = overlay.get_by_label(re.compile(r"^External port\b"))
        expect(port).to_have_value("443")
        port.fill("8443")
        listener = overlay.get_by_label(re.compile(r"^Listener\b"))
        expect(listener).to_have_value("https")
        listener.fill("")

        answer = _save(page, overlay, "workspace_providers", wp)
        assert answer.status == 200, f"{answer.status} {answer.text()}"
        sent = answer.request.post_data_json["config"]
        assert sent["connection"]["token"] == MASK, f"the console did not send the served mask back for the token: {sent['connection']}"
        assert sent["reachability"]["gateway"]["section_name"] is None, "an emptied listener must be sent as none, not left out (left out, the stored one is kept)"
        expect(overlay).not_to_be_visible(timeout=15_000)

        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            after = c.get(f"/v1/workspace_providers/{wp}").json()
        expected = _with(_with(before, ("config", "reachability", "external_port"), 8443), ("config", "reachability", "gateway", "section_name"), None)
        assert after == expected, f"the edit changed a field it did not show:\nbefore {before}\nafter  {after}"
        assert after["config"]["connection"]["token"] == MASK   # still a stored token: an emptied one would be served empty
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/workspace_providers/{wp}")


# ---- Semantic Search Storage -------------------------------------------------------------------------------------------------------------------------------

def test_a_semantic_search_store_edit_keeps_every_field_and_the_stored_password(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    """A pgvector store with every tuning field set, three of which the form never shows (distance metric, ef_search, reindex cron: the old form dropped them
    and forced ef_search back). The edit changes M; the password is left blank. No connection is made: the host does not resolve, and neither the save nor the
    edit form tests it (audit G2-07)."""
    sid = f"reg-ssp-pg-{unique_suffix}"
    seed = {
        "id": sid,
        "provider": "pgvector",
        "config": {
            "hostname": "pg-journey.invalid",
            "port": 6543,
            "username": "journey",
            "password": f"pw-{unique_suffix}",
            "database": "vectors",
            "db_schema": "journey",
            "distance_metric": "l2",
            "hnsw_m": 24,
            "hnsw_ef_construction": 80,
            "hnsw_ef_search": 150,
            "reindex_cron": "0 3 * * *",
            "use_halfvec": True,
        },
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/ssp", json=seed)
        assert r.status_code == 201, r.text
        before = c.get(f"/v1/ssp/{sid}").json()
    try:
        assert before["config"]["password"] == MASK, before["config"]
        _open_detail(page, console_url, "ssp", sid, "ssp-section-config")
        overlay = _edit_overlay(page, "ssp-detail-edit", sid)
        expect(overlay.get_by_test_id("ssp-test-connect-draft")).to_have_count(0)
        expect(overlay.get_by_label(re.compile(r"^password secret"))).to_have_value("")
        m = overlay.get_by_label("M graph degree", exact=True)
        expect(m).to_have_value("24")
        m.fill("32")

        answer = _save(page, overlay, "ssp", sid)
        assert answer.status == 200, f"{answer.status} {answer.text()}"
        sent = answer.request.post_data_json["config"]
        assert sent["password"] == MASK, f"the console did not send the served mask back for the blank password: {sent}"
        expect(overlay).not_to_be_visible(timeout=15_000)

        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            after = c.get(f"/v1/ssp/{sid}").json()
        assert after == _with(before, ("config", "hnsw_m"), 32), f"the edit changed a field it did not show:\nbefore {before}\nafter  {after}"
        assert after["config"]["password"] == MASK
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/ssp/{sid}")
