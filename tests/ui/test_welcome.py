"""Execute the shared desktop/mobile welcome and composer, without a browser server."""

import json

import pytest

from tests.ui._mini_react import ROOT, mini_react_context, transpile


@pytest.fixture
def welcome():
    context = mini_react_context(transpile(ROOT / "ui/components/console/nv-session-doc.jsx"), r"""
var CON = { wid: "w", role: "user", username: "A", viewer: { id: "a" }, defaultWorkspaceId: "w", toast: function () {},
  welcomeState: { variant: "user", show: true }, welcomeChanged: function (id, state) { CHANGES.push([id, state]); CON.welcomeState = state; } };
function NV_useConsole() { return CON; }
var CHANGES = [], REQUESTS = [], SENT = [], FOCUSED = 0, ACCEPTED = {current:null};
function setInterval() { return 0; } function clearInterval() {}
window.primerApi = { apiFetch: function (method, path, body) { return new Promise(function (resolve, reject) { REQUESTS.push({method:method,path:path,body:body,resolve:resolve,reject:reject}); }); } };
var COMPOSER = { current: { send: function (text) { SENT.push(text); return Promise.resolve(true); }, focus: function () { FOCUSED++; } } };
function mountWelcome() { MR.mount(NV_SessionWelcome, {sid:"s",composer:COMPOSER,accepted:ACCEPTED}); }
function button(text) { return MR.findProp("children", text); }
var SEND_REQUESTS = [], STARTED = 0, SID = 's';
function Chat() { return React.createElement(React.Fragment,null,
  React.createElement(NV_SessionWelcome,{key:CON.viewer.id+':'+CON.role+':'+SID,sid:SID,composer:COMPOSER,accepted:ACCEPTED}),
  React.createElement(NV_Composer,{sid:SID,welcomeComposer:COMPOSER,welcomeAccepted:ACCEPTED,
    onSend:function(text){return new Promise(function(resolve,reject){SEND_REQUESTS.push({text:text,resolve:resolve,reject:reject});});},
    onSendStarted:function(){STARTED++;}})); }
""")
    context.eval((ROOT / "ui/foundation/use-mutation.js").read_text())
    try:
        yield context
    finally:
        context.close()


def value(context, expression):
    return json.loads(context.eval("JSON.stringify(" + expression + ")"))


def resolve(context, index, role="user", show=False):
    context.eval(f"REQUESTS[{index}].resolve({json.dumps({'variant': role, 'show': show})});")
    context.eval("MR.settle();")


@pytest.mark.parametrize("ack_pending", [False, True])
def test_first_manual_send_hides_on_acceptance_without_waiting_for_a_turn(welcome, ack_pending):
    welcome.eval("MR.mount(Chat);")
    if not ack_pending:
        resolve(welcome, 0)
    welcome.eval("MR.find('nv-composer-input').props.onChange({target:{value:'Hello'}}); MR.settle(); MR.click('nv-send');")
    assert value(welcome, "!!MR.find('nv-welcome')") is True
    welcome.eval("SEND_REQUESTS[0].resolve({accepted:true});")
    welcome.eval("MR.settle();")
    assert value(welcome, "STARTED") == 1
    assert value(welcome, "!!MR.find('nv-welcome')") is False
    if ack_pending:
        resolve(welcome, 0)
    welcome.eval("MR.mount(function(){return null;}); MR.mount(Chat);")
    assert value(welcome, "!!MR.find('nv-welcome')") is False
    assert value(welcome, "REQUESTS.length") == 1


def test_failed_manual_send_preserves_welcome_and_draft(welcome):
    welcome.eval("MR.mount(Chat);")
    resolve(welcome, 0)
    welcome.eval("MR.find('nv-composer-input').props.onChange({target:{value:'Keep this'}}); MR.settle(); MR.click('nv-send'); SEND_REQUESTS[0].reject({detail:'Offline'});")
    welcome.eval("MR.settle();")
    assert value(welcome, "!!MR.find('nv-welcome')") is True
    assert value(welcome, "MR.find('nv-composer-input').props.value") == "Keep this"


def test_delayed_chip_cannot_dismiss_a_deliberately_reopened_presentation(welcome):
    welcome.eval("MR.mount(Chat);")
    resolve(welcome, 0)
    welcome.eval("button('Start a new project').props.onClick(); MR.settle(); NV_WELCOME_INSTANCES.s(); MR.settle();")
    resolve(welcome, 1, show=True)
    resolve(welcome, 2)
    welcome.eval("SEND_REQUESTS[0].resolve({accepted:true});")
    welcome.eval("MR.settle();")
    assert value(welcome, "!!MR.find('nv-welcome')") is True


def test_manual_send_during_reopen_is_not_undone_by_its_late_response(welcome):
    welcome.eval("MR.mount(Chat);")
    resolve(welcome, 0)
    welcome.eval("button('Dismiss welcome').props.onClick(); MR.settle(); NV_WELCOME_INSTANCES.s(); MR.settle();")
    welcome.eval("MR.find('nv-composer-input').props.onChange({target:{value:'Hello'}}); MR.settle(); MR.click('nv-send'); SEND_REQUESTS[0].resolve({});")
    welcome.eval("MR.settle();")
    resolve(welcome, 1, show=True)
    assert value(welcome, "!!MR.find('nv-welcome')") is False
    resolve(welcome, 2)
    assert value(welcome, "CON.welcomeState.show") is False


def test_accepted_chat_retries_failed_ack_and_exposes_compact_durable_retry(welcome):
    welcome.eval("MR.mount(Chat); REQUESTS[0].reject({detail:'Storage unavailable'});")
    welcome.eval("MR.settle(); MR.find('nv-composer-input').props.onChange({target:{value:'Hello'}}); MR.settle(); MR.click('nv-send'); SEND_REQUESTS[0].resolve({});")
    welcome.eval("MR.settle();")
    assert value(welcome, "!!MR.find('nv-welcome')") is False
    assert value(welcome, "REQUESTS[1].path") == "/auth/welcome/acknowledge"
    welcome.eval("REQUESTS[1].reject({detail:'Still unavailable'});")
    welcome.eval("MR.settle();")
    assert "Still unavailable" in value(welcome, "MR.texts().join(' ')")
    welcome.eval("button('Retry saving').props.onClick(); MR.settle();")
    resolve(welcome, 2)
    welcome.eval("MR.mount(function(){return null;}); MR.mount(Chat);")
    assert value(welcome, "!!MR.find('nv-welcome')") is False


@pytest.mark.parametrize("scope", ["person", "role", "session"])
def test_old_send_cannot_dismiss_another_welcome_scope(welcome, scope):
    welcome.eval("MR.mount(Chat);")
    resolve(welcome, 0)
    welcome.eval("MR.find('nv-composer-input').props.onChange({target:{value:'Hello'}}); MR.settle(); MR.click('nv-send');")
    welcome.eval({"person": "CON.viewer={id:'b'}", "role": "CON.role='admin'", "session": "SID='other'"}[scope] + "; CON.welcomeState={variant:CON.role,show:true}; MR.rerender();")
    welcome.eval("SEND_REQUESTS[0].resolve({});")
    welcome.eval("MR.settle();")
    assert value(welcome, "!!MR.find('nv-welcome')") is True


def test_ack_is_durable_but_current_mount_stays_visible_and_revisit_hides(welcome):
    welcome.eval("mountWelcome();")
    assert value(welcome, "REQUESTS.map(function(r){return [r.path,r.body];})") == [["/auth/welcome/acknowledge", {"role": "user"}]]
    resolve(welcome, 0)
    assert value(welcome, "!!MR.find('nv-welcome')") is True
    assert value(welcome, "CHANGES") == [["a", {"variant": "user", "show": False}]]
    welcome.eval("button('Dismiss welcome').props.onClick(); MR.settle();")
    assert value(welcome, "FOCUSED") == 1
    assert value(welcome, "!!MR.find('nv-welcome')") is False
    welcome.eval("MR.mount(function(){return null;}); mountWelcome();")
    assert value(welcome, "!!MR.find('nv-welcome')") is False
    welcome.eval("MR.click('nv-welcome-reopen');")
    assert value(welcome, "REQUESTS[1].path") == "/auth/welcome/reopen"
    resolve(welcome, 1, show=True)
    assert value(welcome, "!!MR.find('nv-welcome')") is True
    assert value(welcome, "REQUESTS[2].path") == "/auth/welcome/acknowledge"


def test_failed_ack_stays_visible_and_retryable_without_false_success(welcome):
    welcome.eval("mountWelcome(); REQUESTS[0].reject({detail:'Storage unavailable'});")
    welcome.eval("MR.settle();")
    assert "Storage unavailable" in value(welcome, "MR.texts().join(' ')")
    assert value(welcome, "CHANGES") == []
    assert value(welcome, "button('Start a new project').props['aria-disabled']") == "true"
    assert value(welcome, "!!button('Start a new project').props.disabled") is False
    welcome.eval("button('Retry saving').props.onClick(); MR.settle();")
    resolve(welcome, 1)
    assert value(welcome, "!!button('Start a new project').props['aria-disabled']") is False
    welcome.eval("button('Start a new project').props.onClick();")
    welcome.eval("MR.settle();")
    assert "runbooks/moderate-a-request" in value(welcome, "SENT[0]")
    assert value(welcome, "!!MR.find('nv-welcome')") is False


def test_saving_ack_keeps_controls_focusable_and_refuses_actions(welcome):
    welcome.eval("mountWelcome();")
    for label in ("Start a new project", "Dismiss welcome"):
        assert value(welcome, f"!!button({json.dumps(label)}).props.disabled") is False
        assert value(welcome, f"button({json.dumps(label)}).props['aria-disabled']") == "true"
        welcome.eval(f"button({json.dumps(label)}).props.onClick(); MR.settle();")
    assert value(welcome, "SENT") == []
    assert value(welcome, "!!MR.find('nv-welcome')") is True
    assert value(welcome, "FOCUSED") == 0


def test_reopen_keeps_focusable_busy_control_and_refuses_duplicate_click(welcome):
    welcome.eval("CON.welcomeState.show=false; mountWelcome(); MR.click('nv-welcome-reopen');")
    assert value(welcome, "MR.find('nv-welcome-reopen').props['aria-busy']") == "true"
    assert value(welcome, "MR.find('nv-welcome-reopen').props['aria-disabled']") == "true"
    assert value(welcome, "!!MR.find('nv-welcome-reopen').props.disabled") is False
    welcome.eval("MR.click('nv-welcome-reopen');")
    assert value(welcome, "REQUESTS.length") == 1
    resolve(welcome, 0, show=True)
    assert value(welcome, "REQUESTS[1].path") == "/auth/welcome/acknowledge"
    resolve(welcome, 1)
    assert value(welcome, "!!button('Dismiss welcome').props['aria-disabled']") is False


def test_failed_reopen_retry_reopens_before_acknowledging(welcome):
    welcome.eval("CON.welcomeState.show=false; mountWelcome(); MR.click('nv-welcome-reopen'); REQUESTS[0].reject({detail:'Offline'});")
    welcome.eval("MR.settle(); button('Retry saving').props.onClick(); MR.settle();")
    assert value(welcome, "REQUESTS[1].path") == "/auth/welcome/reopen"
    resolve(welcome, 1, show=True)
    assert value(welcome, "!!MR.find('nv-welcome')") is True
    assert value(welcome, "REQUESTS[2].path") == "/auth/welcome/acknowledge"


@pytest.mark.parametrize("role,own,other", [
    ("user", "Start a new project", "Enable internal collections"),
    ("admin", "Enable internal collections", "Start a new project"),
])
def test_role_variant_and_eligibility_never_leak_other_role(welcome, role, own, other):
    welcome.eval(f"CON.role={json.dumps(role)}; CON.welcomeState.variant=CON.role; mountWelcome();")
    text = value(welcome, "MR.texts()")
    assert own in text and other not in text
    assert value(welcome, "NV_welcomeEligible(CON,{workspace_id:'w',binding:{kind:'agent',agent_id:'operator'}})") is True
    for change in ["CON.role='restricted'", "CON.wid='other'", "CON.viewer=null", "CON.welcomeState=null"]:
        assert value(welcome, "(function(){var saved=Object.assign({},CON); " + change + "; var ok=NV_welcomeEligible(CON,{workspace_id:'w',binding:{kind:'agent',agent_id:'operator'}}); CON=saved; return ok;})()") is False


def test_unmount_during_ack_does_not_update_next_person(welcome):
    welcome.eval("mountWelcome(); MR.mount(function(){return null;}); CON.viewer={id:'b'};")
    resolve(welcome, 0)
    assert value(welcome, "CHANGES") == []


def test_operator_reopen_is_read_after_a_turn_without_a_poll_or_token_refetch(welcome):
    welcome.eval("""
CON.welcomeState.show=false;
var REFRESHED=[];
window.primerApi._resource={refetchKey:function(key){REFRESHED.push(key);}};
MR.mount(NV_SessionWelcome,{sid:'s',composer:COMPOSER,running:false});
MR.rerender({sid:'s',composer:COMPOSER,running:true});
MR.rerender(); MR.rerender();
""")
    assert value(welcome, "REFRESHED") == []
    welcome.eval("MR.rerender({sid:'s',composer:COMPOSER,running:false}); MR.rerender();")
    assert value(welcome, "REFRESHED") == ["nv-shell:auth-status"]
    # The refreshed server state is what opens the card, never message text
    # or a guessed tool name. It is acknowledged only after mounting again.
    welcome.eval("CON.welcomeState={variant:'user',show:true}; MR.rerender();")
    assert value(welcome, "!!MR.find('nv-welcome')") is True
    assert value(welcome, "REQUESTS[0].path") == "/auth/welcome/acknowledge"


@pytest.mark.parametrize("succeeds", [True, False])
def test_chip_uses_real_composer_send_and_preserves_unrelated_draft(welcome, succeeds):
    welcome.eval("""
var SEND_REQUESTS = [], STARTED = 0;
var SH_api = {fileUpload:function(){return Promise.resolve({});}};
function FileReader() {}
FileReader.prototype.readAsDataURL=function(){this.result='data:text/plain;base64,eA==';this.onload();};
MR.mount(NV_Composer, {sid:'s',welcomeComposer:COMPOSER,running:true,
  onSend:function(text,id,attachments){return new Promise(function(resolve,reject){SEND_REQUESTS.push({text:text,id:id,attachments:attachments,resolve:resolve,reject:reject});});},
  onSendStarted:function(){STARTED++;}});
MR.find('nv-composer-input').props.onChange({target:{value:'My unfinished draft'}}); MR.settle();
MR.find('nv-attach-input').props.onChange({target:{files:[{name:'draft.txt',type:'text/plain'}]}});
""")
    welcome.eval("MR.settle();")
    assert value(welcome, "MR.findAll('nv-attach-chip:').length") == 1
    welcome.eval("""
COMPOSER.current.send('A welcome suggestion'); MR.settle();
""")
    assert value(welcome, "SEND_REQUESTS[0].text") == "A welcome suggestion"
    assert value(welcome, "MR.find('nv-send').props['data-mode']") == "queue"
    welcome.eval("SEND_REQUESTS[0]." + ("resolve({})" if succeeds else "reject({detail:'No connection'})") + ";")
    welcome.eval("MR.settle();")
    assert value(welcome, "NV_DRAFTS.s") == "My unfinished draft"
    assert value(welcome, "MR.find('nv-composer-input').props.value") == "My unfinished draft"
    assert value(welcome, "MR.findAll('nv-attach-chip:').length") == 1
    assert value(welcome, "!!SEND_REQUESTS[0].attachments") is False
    assert value(welcome, "STARTED") == int(succeeds)
    if not succeeds:
        assert "No connection" in value(welcome, "MR.texts().join(' ')")
    welcome.eval("MR.click('nv-send'); SEND_REQUESTS[1].resolve({});")
    welcome.eval("MR.settle();")
    assert value(welcome, "SEND_REQUESTS[1].text").startswith("My unfinished draft\n\nAttached file: uploads/")
    assert value(welcome, "MR.find('nv-composer-input').props.value") == ""
    assert value(welcome, "MR.findAll('nv-attach-chip:').length") == 0
