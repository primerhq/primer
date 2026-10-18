/* global window */
// register-common.jsx - shared register pieces for the provider catalog
// and the setup wizard.
//
// This file currently holds only the profile id helpers (plan 02 Task 6,
// seam I-5); the class table and the other shared register pieces land in
// later tasks and import from here.
//
// A profile id is what the console's URL paths and the agents reference,
// so it must match the id rule: the first character alphanumeric, the
// rest alphanumeric plus ".", "_" and "-". A model name like
// "anthropic/claude-3.5:beta" (OpenRouter-style) carries "/" and ":",
// which the rule rejects, so a raw concatenation built ids the server
// could not address (the wizard's own GET 404'd on the "/" it contained).

// The id rule the helpers produce.
const PC_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

// One part of a profile id. Every character outside [A-Za-z0-9._-]
// becomes "-", runs of "-" become one "-", then leading and trailing
// "-", "." and "_" are trimmed, so a part is either "" or matches
// PC_ID_RE by itself.
function PC_idPart(text) {
  const s = String(text == null ? "" : text)
    .replace(/[^A-Za-z0-9._-]/g, "-")
    .replace(/-+/g, "-");
  return s.replace(/^[-._]+/, "").replace(/[-._]+$/, "");
}

// The profile id for one (provider, model) pair: the two parts joined
// with "--", or "" when either part is "" (an empty id omits the key,
// and the server makes one: primer/model/common.py).
function PC_profileIdFor(providerId, modelName) {
  const a = PC_idPart(providerId);
  const b = PC_idPart(modelName);
  if (!a || !b) return "";
  return a + "--" + b;
}

window.PC_ID_RE = PC_ID_RE;
window.PC_idPart = PC_idPart;
window.PC_profileIdFor = PC_profileIdFor;
