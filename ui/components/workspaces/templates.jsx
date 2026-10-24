/* global React, Icon, Btn, Modal, Banner, CardList, Card, Fab, WS_FieldRow, WS_Section */

// ---- WT pure helpers: no JSX, no React (tests/ui/test_wt_helpers.py runs them in MiniRacer) ----
// The Workspace templates page and its template overlay (platform revamp Plan 08, Tasks 9 to 11; design 14.2) are built on these. They decide the units
// (memory and disk are GiB here and bytes or a Kubernetes quantity on the wire), what a card says, which admin-only settings a template holds (the
// server's own rule), the body a save sends (only what the person changed, written onto a copy of the row the form loaded, so a field the form does not
// show is never dropped), where a refusal is shown, and every template past the 200 of one page.

// The list key of the page, and every list a template write must refresh (the page's and the new-workspace dialog's).
var WT_LIST_KEY = "ws:templates";
var WT_TEMPLATE_KEYS = ["ws:templates", "nv-ov:workspace-templates"];

// The templates primer ships (primer/bootstrap/defaults.py RESERVED_WORKSPACE_TEMPLATES): the server refuses to change or delete them.
var WT_RESERVED_IDS = ["local-default"];

function WT_isReserved(t) {
  return !!t && WT_RESERVED_IDS.indexOf(t.id) >= 0;
}

var WT_KIND_LABELS = { local: "Local", container: "Docker", kubernetes: "Kubernetes" };
var WT_KIND_ICONS = {
  local: "M2 3h10v8H2Z M2 6h10",
  container: "M2 4.5 7 2l5 2.5v5L7 12 2 9.5Z M7 7v5 M2 4.5 7 7l5-2.5",
  kubernetes: "M7 1.5 12 4.5v5L7 12.5 2 9.5v-5Z M7 4.5v5 M4.5 6l5 2 M9.5 6l-5 2",
};

// The Kubernetes backend fields only an admin may set (primer/workspace/template_privilege.py _K8S_FIELDS, in its order).
var WT_K8S_ADMIN_FIELDS = [
  "extra_volumes", "extra_volume_mounts", "pod_overrides", "container_security_context_overrides", "service_account_name", "tolerations",
];
// The GPU resource a user may name; any other is admin-only (lead ruling 15 of plan 08).
var WT_DEFAULT_GPU_RESOURCE = "nvidia.com/gpu";
// With no command the Kubernetes backend runs `sleep infinity`, and the runtime never starts, so a new Kubernetes template is given this one.
var WT_K8S_DEFAULT_COMMAND = ["python", "-m", "primer_runtime.server"];

var WT_GIB = 1073741824;
var WT_MIB = 1048576;
// The most one workspace may have (primer/model/workspace.py MAX_WORKSPACE_*, MAX_GPU_COUNT): a template above them stores, but no workspace can be made from it.
var WT_MAX_CPU = 1024;
var WT_MAX_GIB = 65536;
var WT_MAX_GPUS = 64;
// The most pages of 200 one listing asks for (20,000 templates), so a server that always answers a full page cannot keep the page asking.
var WT_MAX_PAGES = 100;

function WT_trim(value) {
  return value == null ? "" : String(value).trim();
}

function WT_has(obj, key) {
  return !!obj && Object.prototype.hasOwnProperty.call(obj, key);
}

function WT_copy(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

// A Kubernetes quantity, as the server reads one (primer/model/workspace.py parse_k8s_quantity): a number and at most one suffix, with no space.
var WT_QUANTITY = /^([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))(Ki|Mi|Gi|Ti|Pi|Ei|[eE][+-]?[0-9]{1,4}|[mkMGTPE])?$/;
var WT_QUANTITY_SCALE = {
  "": 1, m: 0.001, k: 1e3, M: 1e6, G: 1e9, T: 1e12, P: 1e15, E: 1e18,
  Ki: 1024, Mi: 1048576, Gi: 1073741824, Ti: 1099511627776, Pi: 1125899906842624, Ei: 1152921504606846976,
};

// GiB in a Kubernetes quantity ("500Mi" is 0.48828125), or null when it is not one.
function WT_quantityToGiB(q) {
  if (q == null) return null;
  var m = WT_QUANTITY.exec(String(q));
  if (!m) return null;
  var suffix = m[2] || "";
  var bytes = WT_has(WT_QUANTITY_SCALE, suffix) ? Number(m[1]) * WT_QUANTITY_SCALE[suffix] : Number(m[1]) * Math.pow(10, Number(suffix.slice(1)));
  var gib = bytes / WT_GIB;
  return isFinite(gib) ? gib : null;
}

// At most two decimals, trailing zeros trimmed: 0.49, 1.5, 10.
function WT_formatGiB(g) {
  if (g == null || !isFinite(g)) return "";
  return String(Math.round(g * 100) / 100);
}

// A plain decimal number above zero ("2", "1.5"), or null: "1e3", "0x10" and "" are not what a person typed into a size.
var WT_DECIMAL = /^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$/;

function WT_positiveNumber(text) {
  var s = WT_trim(text);
  if (!WT_DECIMAL.test(s)) return null;
  var n = Number(s);
  return isFinite(n) && n > 0 ? n : null;
}

// GiB typed into a field, as whole bytes; null when it is not a positive number or is less than one byte.
function WT_giBToBytes(text) {
  var n = WT_positiveNumber(text);
  if (n == null) return null;
  var bytes = Math.round(n * WT_GIB);
  return bytes >= 1 ? bytes : null;
}

// A disk typed in GiB as the quantity the template stores: <n>Gi when whole GiB, <n>Mi when whole MiB, else plain bytes.
function WT_diskQuantity(text) {
  var bytes = WT_giBToBytes(text);
  if (bytes == null) return null;
  if (bytes % WT_GIB === 0) return bytes / WT_GIB + "Gi";
  if (bytes % WT_MIB === 0) return bytes / WT_MIB + "Mi";
  return String(bytes);
}

function WT_kindOf(t) {
  return (t && t.backend && t.backend.kind) || "local";
}

function WT_label(t, choice) {
  var kind = WT_kindOf(t);
  return (choice && choice.label) || WT_KIND_LABELS[kind] || kind;
}

// The size a template gives its workspaces, where each backend reads it: Kubernetes from resources and backend.pvc_size, Docker from its backend.
function WT_templateSize(t) {
  var kind = WT_kindOf(t);
  var b = (t && t.backend) || {};
  var r = (t && t.resources) || {};
  var out = { cpu: null, memoryGiB: null, diskGiB: null, diskRaw: null };
  var src = kind === "kubernetes" ? r : kind === "container" ? b : null;
  if (!src) return out;
  out.cpu = src.cpu_cores != null ? Number(src.cpu_cores) : null;
  out.memoryGiB = src.memory_bytes != null ? Number(src.memory_bytes) / WT_GIB : null;
  if (kind === "kubernetes" && b.pvc_size != null) {
    out.diskRaw = String(b.pvc_size);
    out.diskGiB = WT_quantityToGiB(out.diskRaw);
  }
  return out;
}

// "2 CPU · 4 GiB · 10 GiB disk", the parts there are; a disk that is not a quantity is shown as stored.
function WT_sizeText(size) {
  var parts = [];
  if (size.cpu != null) parts.push(WT_formatGiB(size.cpu) + " CPU");
  if (size.memoryGiB != null) parts.push(WT_formatGiB(size.memoryGiB) + " GiB");
  if (size.diskGiB != null) parts.push(WT_formatGiB(size.diskGiB) + " GiB disk");
  else if (size.diskRaw) parts.push(size.diskRaw + " disk");
  return parts.join(" · ");
}

// description · <Label> on <provider> · the size.
function WT_summaryLine(t, choice) {
  var parts = [];
  if (t.description) parts.push(t.description);
  parts.push(WT_label(t, choice) + " on " + t.provider_id);
  var size = WT_sizeText(WT_templateSize(t));
  if (size) parts.push(size);
  return parts.join(" · ");
}

// A card's facts: the provider, then the image and the size when there are any (one to three lines; design 14.2 and the kit's three-fact card).
function WT_cardFacts(t, choice) {
  var facts = [["Provider", t.provider_id + " · " + WT_label(t, choice)]];
  var image = WT_kindOf(t) !== "local" && t.backend ? t.backend.image : null;
  if (image) facts.push(["Image", image]);
  var size = WT_sizeText(WT_templateSize(t));
  if (size) facts.push(["Size", size]);
  return facts;
}

// Python's `value or None` for a served value: null, "", [] and {} hold nothing.
function WT_holds(value) {
  if (value == null || value === "" || value === false) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

// The admin-only settings a template holds, in the server's order (primer/workspace/template_privilege.py admin_only_settings_held). A non-admin may
// neither change nor delete such a template, so the console opens it read-only for them.
function WT_heldAdminFields(t) {
  var out = [];
  var kind = WT_kindOf(t);
  var b = (t && t.backend) || {};
  if (kind === "container" && WT_holds(b.extra_mounts)) out.push("backend.extra_mounts");
  var files = (t && t.files) || [];
  if (files.some(function (f) { return f && f.source && f.source.kind === "secret"; })) out.push("files.secret");
  if (kind === "kubernetes") {
    WT_K8S_ADMIN_FIELDS.forEach(function (f) { if (WT_holds(b[f])) out.push("backend." + f); });
    var gpu = b.gpu;
    if (gpu && (gpu.resource_name || WT_DEFAULT_GPU_RESOURCE) !== WT_DEFAULT_GPU_RESOURCE) out.push("backend.gpu.resource_name");
  }
  return out;
}

// The flat form: a string per input (the key decides the body path), rows for the tables, FileMount objects for the files.
function WT_blankForm() {
  return {
    id: "", description: "", provider_id: "", kind: "local",
    image: "", cpu: "", memoryGiB: "",
    diskGiB: "", storageClass: "", diskAccess: "one", nodeSelectorRows: [], tolerationRows: [], gpuCount: "", gpuResource: "",
    serviceAccount: "", pullPolicy: "", command: "", args: "", workdir: "/workspace", extraVolumesJson: "", extraVolumeMountsJson: "",
    podSharePid: "", podDns: "", podRestart: "", podGrace: "",
    user: "", extraMountRows: [],
    files: [], envRows: [], init: "", statePath: ".state", tmpPath: ".tmp", strict: "off",
  };
}

// A new template's form on a provider: the provider's runtime image for Docker, the runtime command for Kubernetes, a 10 GiB disk.
function WT_emptyForm(choice) {
  var form = WT_blankForm();
  return WT_formForChoice(form, choice, null);
}

// The form moved to another provider: its kind, and the prefills that come with it, replacing a prefill of the old provider nobody changed.
function WT_formForChoice(form, choice, previous) {
  var next = Object.assign({}, form);
  var kind = (choice && choice.kind) || "local";
  next.provider_id = (choice && choice.id) || "";
  next.kind = kind;
  var oldImage = (previous && previous.default_image) || "";
  if (WT_trim(next.image) === "" || next.image === oldImage) next.image = kind === "container" ? (choice.default_image || "") : "";
  var defaultCommand = WT_K8S_DEFAULT_COMMAND.join("\n");
  if (kind === "kubernetes" && WT_trim(next.command) === "") next.command = defaultCommand;
  if (kind !== "kubernetes" && next.command === defaultCommand) next.command = "";
  if (WT_trim(next.diskGiB) === "") next.diskGiB = "10";
  return next;
}

function WT_lines(value) {
  return String(value == null ? "" : value).split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
}

// The fields that show a list one item per line, and where each list is stored. A stored list fits when splitting its lines gives it back: an item with a
// line break (`sh -c` with a script, a heredoc), an empty item or one with spaces at its ends does not, and re-splitting it would change the argv or the
// commands. Such a field is shown as stored (its JSON) and read-only, and a save never writes it (round 2 of #886).
var WT_LINE_LISTS = { command: ["backend", "entrypoint"], args: ["backend", "args"], init: ["init_commands"] };

function WT_storedList(t, key) {
  var path = WT_LINE_LISTS[key];
  var node = t;
  for (var i = 0; i < path.length && node != null; i++) node = node[path[i]];
  return node;
}

function WT_lineListFits(list) {
  if (list == null) return true;
  if (!Array.isArray(list) || list.some(function (item) { return typeof item !== "string"; })) return false;
  return JSON.stringify(WT_lines(list.join("\n"))) === JSON.stringify(list);
}

// The line-list keys of a stored template that the form shows read-only, in WT_LINE_LISTS order (Command and Arguments are Kubernetes only).
function WT_lockedLineKeys(t) {
  var kubernetes = WT_kindOf(t) === "kubernetes";
  return Object.keys(WT_LINE_LISTS).filter(function (key) {
    return (key === "init" || kubernetes) && !WT_lineListFits(WT_storedList(t, key));
  });
}

function WT_lineText(list) {
  return WT_lineListFits(list) ? (list || []).join("\n") : JSON.stringify(list);
}

function WT_jsonText(value) {
  return Array.isArray(value) && value.length ? JSON.stringify(value, null, 2) : "";
}

// The form of a stored template. A value the form shows in other units (500Mi as 0.49 GiB) is only rewritten if the person edits that field.
function WT_formFromTemplate(t) {
  var form = WT_blankForm();
  var kind = WT_kindOf(t);
  var b = t.backend || {};
  var size = WT_templateSize(t);
  form.id = t.id || "";
  form.description = t.description || "";
  form.provider_id = t.provider_id || "";
  form.kind = kind;
  if (kind !== "local") {
    form.image = b.image || "";
    form.cpu = size.cpu != null ? String(size.cpu) : "";
    form.memoryGiB = size.memoryGiB != null ? WT_formatGiB(size.memoryGiB) : "";
    form.workdir = b.workdir || "/workspace";
  }
  if (kind === "kubernetes") {
    form.diskGiB = size.diskGiB != null ? WT_formatGiB(size.diskGiB) : (size.diskRaw || "");
    form.storageClass = b.storage_class || "";
    form.diskAccess = (b.pvc_access_modes || []).indexOf("ReadWriteMany") >= 0 ? "many" : "one";
    var selector = b.node_selector || {};
    form.nodeSelectorRows = Object.keys(selector).map(function (k) { return { key: k, value: selector[k] == null ? "" : String(selector[k]) }; });
    form.tolerationRows = (b.tolerations || []).map(function (tol) {
      return { key: tol.key || "", operator: tol.operator || "Equal", value: tol.value || "", effect: tol.effect || "Any" };
    });
    form.gpuCount = b.gpu ? String(b.gpu.count) : "";
    form.gpuResource = b.gpu ? (b.gpu.resource_name || "") : "";
    form.serviceAccount = b.service_account_name || "";
    form.pullPolicy = b.image_pull_policy || "";
    form.command = WT_lineText(b.entrypoint);
    form.args = WT_lineText(b.args);
    form.extraVolumesJson = WT_jsonText(b.extra_volumes);
    form.extraVolumeMountsJson = WT_jsonText(b.extra_volume_mounts);
    var pod = b.pod_overrides || {};
    form.podSharePid = pod.shareProcessNamespace === true ? "on" : pod.shareProcessNamespace === false ? "off" : "";
    form.podDns = pod.dnsPolicy || "";
    form.podRestart = pod.restartPolicy || "";
    form.podGrace = pod.terminationGracePeriodSeconds != null ? String(pod.terminationGracePeriodSeconds) : "";
  }
  if (kind === "container") {
    form.user = b.user || "";
    form.extraMountRows = (b.extra_mounts || []).map(function (m) {
      return { host: m.host || "", container: m.container || "", readonly: m.readonly ? "yes" : "no" };
    });
  }
  form.files = WT_copy(t.files || []);
  var env = t.env || {};
  form.envRows = Object.keys(env).map(function (k) { return { key: k, value: env[k] == null ? "" : String(env[k]) }; });
  form.init = WT_lineText(t.init_commands);
  form.statePath = t.state_path || ".state";
  form.tmpPath = t.tmp_path || ".tmp";
  form.strict = t.strict_write_locking ? "on" : "off";
  return form;
}

// The form keys each kind shows (and so may write): the shared recipe, then the backend's own.
var WT_SHARED_KEYS = ["description", "files", "envRows", "init", "statePath", "tmpPath", "strict"];
var WT_KIND_KEYS = {
  local: [],
  container: ["image", "cpu", "memoryGiB", "user", "workdir", "extraMountRows"],
  kubernetes: [
    "image", "cpu", "memoryGiB", "diskGiB", "storageClass", "diskAccess", "nodeSelectorRows", "tolerationRows", "gpuCount", "gpuResource",
    "serviceAccount", "pullPolicy", "command", "args", "workdir", "extraVolumesJson", "extraVolumeMountsJson",
    "podSharePid", "podDns", "podRestart", "podGrace",
  ],
};

function WT_formKeys(kind) {
  return WT_SHARED_KEYS.concat(WT_KIND_KEYS[kind] || []);
}

function WT_orNull(value) {
  var s = WT_trim(value);
  return s ? s : null;
}

// A JSON list typed into a field, or null when it is not one ("" is the empty list).
function WT_jsonList(text) {
  if (!WT_trim(text)) return [];
  try {
    var value = JSON.parse(text);
    return Array.isArray(value) ? value : null;
  } catch (e) {
    return null;
  }
}

// Toleration rows as the model takes them: a blank cell is null, "Any" effect is every effect, and an untouched blank row is dropped.
function WT_tolerations(rows) {
  return (rows || []).filter(function (r) {
    return WT_trim(r.key) || WT_trim(r.value) || (r.operator && r.operator !== "Equal") || (r.effect && r.effect !== "Any");
  }).map(function (r) {
    return {
      key: WT_orNull(r.key), operator: r.operator || "Equal", value: WT_orNull(r.value),
      effect: !r.effect || r.effect === "Any" ? null : r.effect,
    };
  });
}

// The stored pod_overrides with the four pod settings the form shows set or removed; any other key it holds is kept. Null when nothing is left.
function WT_podOverrides(stored, form) {
  var pod = stored && typeof stored === "object" ? WT_copy(stored) : {};
  function put(key, value) {
    if (value === null) delete pod[key];
    else pod[key] = value;
  }
  put("shareProcessNamespace", form.podSharePid === "on" ? true : form.podSharePid === "off" ? false : null);
  put("dnsPolicy", form.podDns || null);
  put("restartPolicy", form.podRestart || null);
  put("terminationGracePeriodSeconds", WT_trim(form.podGrace) ? Number(WT_trim(form.podGrace)) : null);
  return Object.keys(pod).length ? pod : null;
}

function WT_resources(body) {
  if (!body.resources || typeof body.resources !== "object") body.resources = {};
  return body.resources;
}

// One form key onto the body, at its path. The GPU pair and the four pod settings are written as one value each, once.
function WT_writeKey(body, form, key, done) {
  var b = body.backend;
  var kubernetes = form.kind === "kubernetes";
  if (key === "gpuCount" || key === "gpuResource") {
    if (done.gpu) return;
    done.gpu = true;
    b.gpu = WT_trim(form.gpuCount) ? { count: Number(WT_trim(form.gpuCount)), resource_name: WT_trim(form.gpuResource) || WT_DEFAULT_GPU_RESOURCE } : null;
    return;
  }
  if (key === "podSharePid" || key === "podDns" || key === "podRestart" || key === "podGrace") {
    if (done.pod) return;
    done.pod = true;
    b.pod_overrides = WT_podOverrides(b.pod_overrides, form);
    return;
  }
  switch (key) {
    case "description": body.description = WT_trim(form.description); return;
    case "image": b.image = WT_trim(form.image); return;
    case "cpu":
      if (kubernetes) WT_resources(body).cpu_cores = WT_positiveNumber(form.cpu);
      else b.cpu_cores = WT_positiveNumber(form.cpu);
      return;
    case "memoryGiB":
      if (kubernetes) WT_resources(body).memory_bytes = WT_giBToBytes(form.memoryGiB);
      else b.memory_bytes = WT_giBToBytes(form.memoryGiB);
      return;
    case "diskGiB": {
      var disk = WT_diskQuantity(form.diskGiB);
      if (disk == null) delete b.pvc_size;
      else b.pvc_size = disk;
      return;
    }
    case "storageClass": b.storage_class = WT_orNull(form.storageClass); return;
    case "diskAccess": b.pvc_access_modes = [form.diskAccess === "many" ? "ReadWriteMany" : "ReadWriteOnce"]; return;
    case "nodeSelectorRows": {
      var selector = {};
      (form.nodeSelectorRows || []).forEach(function (r) { if (WT_trim(r.key)) selector[WT_trim(r.key)] = WT_trim(r.value); });
      b.node_selector = selector;
      return;
    }
    case "tolerationRows": b.tolerations = WT_tolerations(form.tolerationRows); return;
    case "serviceAccount": b.service_account_name = WT_orNull(form.serviceAccount); return;
    case "pullPolicy": b.image_pull_policy = form.pullPolicy || null; return;
    case "command": b.entrypoint = WT_lines(form.command).length ? WT_lines(form.command) : null; return;
    case "args": b.args = WT_lines(form.args).length ? WT_lines(form.args) : null; return;
    case "workdir": b.workdir = WT_trim(form.workdir) || "/workspace"; return;
    case "extraVolumesJson": b.extra_volumes = WT_jsonList(form.extraVolumesJson) || []; return;
    case "extraVolumeMountsJson": b.extra_volume_mounts = WT_jsonList(form.extraVolumeMountsJson) || []; return;
    case "user": b.user = WT_orNull(form.user); return;
    case "extraMountRows":
      b.extra_mounts = (form.extraMountRows || []).filter(function (r) { return WT_trim(r.host) || WT_trim(r.container); }).map(function (r) {
        return { host: WT_trim(r.host), container: WT_trim(r.container), readonly: r.readonly === "yes" };
      });
      return;
    case "files":
      body.files = (form.files || []).filter(function (f) { return f && WT_trim(f.path); }).map(WT_copy);
      return;
    case "envRows": {
      var env = {};
      (form.envRows || []).forEach(function (r) { if (WT_trim(r.key)) env[WT_trim(r.key)] = r.value == null ? "" : String(r.value); });
      body.env = env;
      return;
    }
    case "init": body.init_commands = WT_lines(form.init); return;
    case "statePath": body.state_path = WT_trim(form.statePath) || ".state"; return;
    case "tmpPath": body.tmp_path = WT_trim(form.tmpPath) || ".tmp"; return;
    case "strict": body.strict_write_locking = form.strict === "on"; return;
    default: return;
  }
}

// The body a save sends. An edit (initial is the form as it was opened) writes every key whose value the person changed onto a copy of the row it
// loaded, so every field the form does not show, a masked secret and a key from a later version included, goes back as it was served (the server
// restores a masked value it is sent). A create (initial null) writes every key of the form's kind onto a new object. Both name the provider and the kind.
function WT_templateBody(base, form, initial) {
  var body = initial ? WT_copy(base || {}) : {};
  if (!body.backend || typeof body.backend !== "object") body.backend = {};
  body.backend.kind = form.kind || "local";
  body.provider_id = form.provider_id;
  if (!initial && WT_trim(form.id)) body.id = WT_trim(form.id);
  var done = {};
  var locked = initial ? WT_lockedLineKeys(base || {}) : [];
  WT_formKeys(form.kind || "local").forEach(function (key) {
    if (initial && JSON.stringify(form[key]) === JSON.stringify(initial[key])) return;
    if (locked.indexOf(key) >= 0) return;
    WT_writeKey(body, form, key, done);
  });
  return body;
}

function WT_sizeError(text, max, tooMuch) {
  var n = WT_positiveNumber(text);
  if (n == null) return "Enter a positive number";
  return n > max ? tooMuch : null;
}

function WT_twice(rows) {
  var seen = {};
  return (rows || []).some(function (r) {
    var k = WT_trim(r.key);
    if (!k) return false;
    if (seen[k]) return true;
    seen[k] = true;
    return false;
  });
}

// The form's own errors, by form key (an empty object when it may be sent). Sizes are checked against what a workspace may have, so the form never
// stores a template no workspace can be made from. opts.initial is the form as it was opened (an edit): a size, a count, a grace period or a file mode is
// checked only when the person changed it, because a stored value the server took (a 1Mi disk shown as 0 GiB, 1e-7 cores, a size over the cap of an older
// row, a mode written another way) must not make a save of another field impossible; it goes back as stored. A create (no initial) checks everything.
function WT_validateForm(form, opts) {
  var isAdmin = !!(opts && opts.isAdmin);
  var initial = (opts && opts.initial) || null;
  var errs = {};
  var kind = form.kind;
  function changed(key) {
    return !initial || JSON.stringify(form[key]) !== JSON.stringify(initial[key]);
  }
  function size(key, max, tooMuch, bytes) {
    if (!changed(key) || !WT_trim(form[key])) return;
    var problem = bytes && WT_giBToBytes(form[key]) == null ? "Enter a positive number" : WT_sizeError(form[key], max, tooMuch);
    if (problem) errs[key] = problem;
  }
  if (!WT_trim(form.description)) errs.description = "Enter a description";
  if (kind === "kubernetes" || kind === "container") {
    if (!WT_trim(form.image)) errs.image = "Enter an image";
    size("cpu", WT_MAX_CPU, "At most 1024 CPU cores", false);
    size("memoryGiB", WT_MAX_GIB, "At most 65536 GiB (64 TiB)", true);
  }
  if (kind === "kubernetes") {
    size("diskGiB", WT_MAX_GIB, "At most 65536 GiB (64 TiB)", true);
    if ((changed("gpuCount") || changed("gpuResource")) && WT_trim(form.gpuCount)) {
      var gpus = /^[0-9]+$/.test(WT_trim(form.gpuCount)) ? Number(WT_trim(form.gpuCount)) : 0;
      if (gpus < 1) errs.gpuCount = "Enter a whole number of at least 1";
      else if (gpus > WT_MAX_GPUS) errs.gpuCount = "At most 64 GPUs";
      if (!isAdmin && WT_trim(form.gpuResource) && WT_trim(form.gpuResource) !== WT_DEFAULT_GPU_RESOURCE) {
        errs.gpuResource = "Only an admin may name another GPU resource";
      }
    }
    if (WT_jsonList(form.extraVolumesJson) === null) errs.extraVolumesJson = "Enter a JSON list";
    if (WT_jsonList(form.extraVolumeMountsJson) === null) errs.extraVolumeMountsJson = "Enter a JSON list";
    if (changed("podGrace") && WT_trim(form.podGrace) && !/^[0-9]+$/.test(WT_trim(form.podGrace))) errs.podGrace = "Enter a whole number of seconds";
    if (WT_twice(form.nodeSelectorRows)) errs.nodeSelectorRows = "Each name once";
  }
  // A file's mode is checked when the person wrote it: a row whose path and mode the opened form already had is the stored one.
  var openedModes = initial ? (initial.files || []).map(function (f) { return f ? f.path + "\u0000" + f.mode : ""; }) : [];
  if ((form.files || []).some(function (f) {
    return f && WT_trim(f.mode) && openedModes.indexOf(f.path + "\u0000" + f.mode) < 0 && !/^[0-7]{3,4}$/.test(WT_trim(f.mode));
  })) {
    errs.files = "Use an octal mode such as 0644";
  }
  if (WT_twice(form.envRows)) errs.envRows = "Each name once";
  return errs;
}

// Where each body path is shown: the form key whose control writes it.
var WT_FORM_PATHS = [
  ["id", "id"], ["description", "description"], ["backend.image", "image"],
  ["resources.cpu_cores", "cpu"], ["backend.cpu_cores", "cpu"], ["resources.memory_bytes", "memoryGiB"], ["backend.memory_bytes", "memoryGiB"],
  ["backend.pvc_size", "diskGiB"], ["backend.storage_class", "storageClass"], ["backend.pvc_access_modes", "diskAccess"],
  ["backend.node_selector", "nodeSelectorRows"], ["backend.tolerations", "tolerationRows"],
  ["backend.gpu", "gpuCount"], ["backend.gpu.count", "gpuCount"], ["backend.gpu.resource_name", "gpuResource"],
  ["backend.service_account_name", "serviceAccount"], ["backend.image_pull_policy", "pullPolicy"],
  ["backend.entrypoint", "command"], ["backend.args", "args"], ["backend.workdir", "workdir"],
  ["backend.extra_volumes", "extraVolumesJson"], ["backend.extra_volume_mounts", "extraVolumeMountsJson"],
  ["backend.pod_overrides.shareProcessNamespace", "podSharePid"], ["backend.pod_overrides.dnsPolicy", "podDns"],
  ["backend.pod_overrides.restartPolicy", "podRestart"], ["backend.pod_overrides.terminationGracePeriodSeconds", "podGrace"],
  ["backend.user", "user"], ["backend.extra_mounts", "extraMountRows"],
  ["files", "files"], ["env", "envRows"], ["init_commands", "init"], ["state_path", "statePath"], ["tmp_path", "tmpPath"],
  ["strict_write_locking", "strict"],
];

// A refusal's loc as a body path: without "body", and without the backend's kind tag ("backend.kubernetes.pvc_size" is backend.pvc_size).
function WT_cleanLoc(loc) {
  var parts = Array.isArray(loc) ? loc.map(String) : [];
  if (parts[0] === "body") parts.shift();
  if (parts[0] === "backend" && WT_has(WT_KIND_LABELS, parts[1])) parts.splice(1, 1);
  return parts;
}

// The form key a refusal's loc names (the longest path of WT_FORM_PATHS it starts with), or null.
function WT_fieldKeyForLoc(loc) {
  var parts = WT_cleanLoc(loc);
  var best = null;
  var bestLength = 0;
  WT_FORM_PATHS.forEach(function (entry) {
    var path = entry[0].split(".");
    if (path.length <= bestLength || path.length > parts.length) return;
    for (var i = 0; i < path.length; i++) if (path[i] !== parts[i]) return;
    best = entry[1];
    bestLength = path.length;
  });
  return best;
}

// A refused save, split: each field error under the key keyForLoc names (several on one key are joined), and what no field shows in one line. A
// refusal with no field errors is said through the one reader (window.primerApi.readRefusal). opts.withPath puts each unplaced error's field in front.
// pydantic's "Value error, " in front of a validator's sentence is dropped, as the toolset form drops it (TS_fieldErrorsOf).
function WT_splitErrors(err, keyForLoc, opts) {
  var fields = {};
  var rest = [];
  var entries = err && Array.isArray(err.fieldErrors) ? err.fieldErrors : [];
  entries.forEach(function (fe) {
    var msg = fe && typeof fe.msg === "string" ? fe.msg.replace(/^Value error, /, "") : "";
    var key = keyForLoc(fe ? fe.loc : null);
    if (key) {
      fields[key] = fields[key] ? fields[key] + "; " + msg : msg;
      return;
    }
    var path = WT_cleanLoc(fe ? fe.loc : null).join(".");
    rest.push(opts && opts.withPath && path ? path + ": " + msg : msg);
  });
  if (!entries.length) return { fields: fields, general: window.primerApi.readRefusal(err).message };
  return { fields: fields, general: rest.length ? rest.join("; ") : null };
}

// Every template, a page of 200 at a time in id order, until a short page says the list has ended (or WT_MAX_PAGES pages were read). Each id is listed once:
// a template created between two pages shifts the next page back and repeats a row, and a repeated id would be a second card with the same key. A full page
// that adds no new id ends the listing too (a server that ignores the offset answers every page with the first).
function WT_fetchAllTemplates(apiFetch, signal) {
  var items = [];
  var seen = {};
  function page(n) {
    var path = "/workspace_templates?limit=200&offset=" + n * 200 + "&order_by=id:asc";
    return apiFetch("GET", path, null, { signal: signal }).then(function (answer) {
      var rows = (answer && answer.items) || [];
      var added = 0;
      rows.forEach(function (row) {
        if (row && row.id != null && WT_has(seen, row.id)) return;
        if (row && row.id != null) seen[row.id] = true;
        items.push(row);
        added += 1;
      });
      if (rows.length < 200 || added === 0 || n + 1 >= WT_MAX_PAGES) return { items: items, total: items.length };
      return page(n + 1);
    });
  }
  return page(0);
}

Object.assign(window, {
  WT_LIST_KEY, WT_TEMPLATE_KEYS, WT_RESERVED_IDS, WT_isReserved, WT_KIND_LABELS, WT_KIND_ICONS, WT_K8S_ADMIN_FIELDS, WT_DEFAULT_GPU_RESOURCE,
  WT_K8S_DEFAULT_COMMAND, WT_GIB, WT_MAX_PAGES, WT_quantityToGiB, WT_formatGiB, WT_giBToBytes, WT_diskQuantity, WT_templateSize, WT_sizeText,
  WT_summaryLine, WT_cardFacts, WT_heldAdminFields, WT_emptyForm, WT_formForChoice, WT_formFromTemplate, WT_templateBody, WT_validateForm,
  WT_FORM_PATHS, WT_fieldKeyForLoc, WT_splitErrors, WT_fetchAllTemplates, WT_lockedLineKeys,
});
// ---- end WT pure helpers ----

// Security review 2026-10-08: host mounts, Kubernetes overlays and secret file sources reach the host, the cluster or
// operator secrets, so only an admin may set or change them; the server answers a non-admin with 403 forbidden_role.
const WT_ADMIN_ONLY_NOTE =
  "Admin only: only an admin can set or change these fields, and only an admin can edit or delete a template that has any of them set. Users can still create workspaces from such a template and run their own commands next to its mounts, secrets and service account, so do not put host-privileged mounts, secrets or a service account with real permissions in a template ordinary users may use. Kubernetes overlays must also stay on the server's allowlist.";

// ---- WT_FilesEditor and WT_TemplateOverlay (platform revamp Plan 08, Task 10; design 14.2; tests/ui/test_wt_template_overlay.py) ----------------------

var WT_SOURCE_OPTIONS = [
  { value: "inline", label: "Text" }, { value: "url", label: "URL" }, { value: "document", label: "Document" }, { value: "secret", label: "Secret" },
];

function WT_blankSource(kind) {
  if (kind === "url") return { kind: "url", url: "" };
  if (kind === "document") return { kind: "document", collection_id: "", document_id: "" };
  if (kind === "secret") return { kind: "secret", name: "" };
  return { kind: "inline", content: "" };
}

// The files a template (or, later, one workspace) seeds: a row per FileMount with its path, its source and its mode, and the source's own fields. A new
// source replaces the row's source with a blank one of that kind; editing a part changes that key only, so a part the row does not show (a url's
// sha256 when the url is edited) is kept. A Secret source reads an operator secret, so it is offered to admins only (and shown on a row that holds one).
function WT_FilesEditor(props) {
  var files = props.value || [];
  var t = props.testid;
  var ro = !!props.readOnly;
  function put(i, next) {
    props.onChange(files.map(function (f, j) { return j === i ? next : f; }));
  }
  function setPart(i, key, value) {
    var source = Object.assign({}, files[i].source);
    source[key] = value;
    put(i, Object.assign({}, files[i], { source: source }));
  }
  function part(i, source, key, label, testid, opts) {
    var o = opts || {};
    return (
      <PK_Field label={label} help={o.help}>
        <input className="input mono" data-testid={t + "-" + testid + ":" + i} value={source[key] == null ? "" : source[key]} readOnly={ro || undefined}
          placeholder={o.placeholder} onChange={function (e) { setPart(i, key, o.nullWhenBlank && e.target.value === "" ? null : e.target.value); }} />
      </PK_Field>
    );
  }
  return (
    <PK_Section title="FILES" cols={1} testid={t}>
      {files.map(function (f, i) {
        var source = f.source || WT_blankSource("inline");
        var options = WT_SOURCE_OPTIONS.filter(function (o) { return o.value !== "secret" || props.isAdmin || source.kind === "secret"; });
        return (
          <div key={i} className="col" style={{ gap: 10, paddingBottom: 12, borderBottom: "1px dashed var(--border)" }}>
            <PK_Grid cols={3}>
              <PK_Field label="Path">
                <input className="input mono" data-testid={t + "-path:" + i} value={f.path || ""} readOnly={ro || undefined}
                  onChange={function (e) { put(i, Object.assign({}, f, { path: e.target.value })); }} />
              </PK_Field>
              <PK_Field label="Source">
                <PK_Segmented testid={t + "-source:" + i} ariaLabel={"Source of file " + (i + 1)} value={source.kind} options={options} disabled={ro}
                  onChange={function (kind) { put(i, Object.assign({}, f, { source: WT_blankSource(kind) })); }} />
              </PK_Field>
              <PK_Field label="Mode" help="Octal, for example 0644">
                <input className="input mono" data-testid={t + "-mode:" + i} value={f.mode || ""} readOnly={ro || undefined} placeholder="0644"
                  onChange={function (e) { put(i, Object.assign({}, f, { mode: e.target.value === "" ? null : e.target.value })); }} />
              </PK_Field>
            </PK_Grid>
            {source.kind === "inline" ? (
              <PK_Field label="Content">
                <textarea className="input mono" rows={4} data-testid={t + "-content:" + i} value={source.content || ""} readOnly={ro || undefined}
                  style={{ width: "100%" }} onChange={function (e) { setPart(i, "content", e.target.value); }} />
              </PK_Field>
            ) : null}
            {source.kind === "url" ? (
              <PK_Grid cols={2}>
                {part(i, source, "url", "URL", "url", { placeholder: "https://" })}
                {part(i, source, "sha256", "SHA-256", "sha256", { nullWhenBlank: true })}
              </PK_Grid>
            ) : null}
            {source.kind === "document" ? (
              <PK_Grid cols={2}>
                {part(i, source, "collection_id", "Collection id", "collection")}
                {part(i, source, "document_id", "Document id", "document")}
              </PK_Grid>
            ) : null}
            {source.kind === "secret" ? <PK_Grid cols={2}>{part(i, source, "name", "Secret name", "secret")}</PK_Grid> : null}
            {ro ? null : (
              <div>
                <Btn kind="ghost" size="sm" data-testid={t + "-remove:" + i}
                  onClick={function () { props.onChange(files.filter(function (_f, j) { return j !== i; })); }}>Remove</Btn>
              </div>
            )}
          </div>
        );
      })}
      {props.error ? <div className="field-help" role="alert" data-testid={t + "-error"} style={{ color: "var(--red)" }}>{props.error}</div> : null}
      {ro ? null : (
        <div>
          <button type="button" className="pk-rowtable-add" data-testid={t + "-add"}
            onClick={function () { props.onChange(files.concat([{ path: "", source: WT_blankSource("inline"), mode: null }])); }}>+ Add</button>
        </div>
      )}
    </PK_Section>
  );
}

// A kit row table across its grid row, with its help line and its error under it (where a PK_Field puts them).
function WT_TableWithNotes(props) {
  var table = props.table;
  return (
    <div className="pk-span-full">
      <PK_RowTable {...table} />
      {props.help ? <div className="field-help">{props.help}</div> : null}
      {props.error ? (
        <div className="field-help" role="alert" data-testid={table.testid + "-error"} style={{ color: "var(--red)" }}>{props.error}</div>
      ) : null}
    </div>
  );
}

// The form keys inside each fold: a refusal of one of them opens its fold, so no error is drawn where nobody can see it.
var WT_FOLD_KEYS = {
  k8sMore: ["command", "args", "workdir", "extraVolumesJson", "extraVolumeMountsJson", "podSharePid", "podDns", "podRestart", "podGrace"],
  dockerMore: ["user", "workdir", "extraMountRows"],
  advanced: ["statePath", "tmpPath", "strict"],
};

var WT_NOTE_STYLE = { padding: "8px 10px", border: "1px solid var(--border)", borderRadius: 8 };

// The one overlay that creates, edits and shows a workspace template. Mode: create; else view for the built-in template, and for a non-admin on a template
// that holds admin-only settings (the server would answer their save or delete with 403); else edit. A row comes from props.row (a card) or is read by
// props.templateId (the overlay address workspaces:templates:<id>). The save writes only what changed onto the row it opened (WT_templateBody).
function WT_TemplateOverlay(props) {
  var con = NV_useConsole();
  var isAdmin = con.role === "admin";
  var api = window.primerApi;
  var apiFetch = api.apiFetch;
  var creating = !!props.create;
  var given = props.row || null;
  var id = creating ? null : props.templateId || (given && given.id) || null;

  var choicesRes = api.useResource("ws:provider-choices", function (signal) {
    return apiFetch("GET", "/workspace_providers/_choices", null, { signal: signal });
  }, { pollMs: 0 });
  var rowRes = api.useResource(!creating && !given && id ? "ws:template:" + id : null, function (signal) {
    return apiFetch("GET", "/workspace_templates/" + encodeURIComponent(id), null, { signal: signal });
  }, { pollMs: 0 });
  var choices = (choicesRes.data && choicesRes.data.items) || [];
  var row = creating ? null : given || rowRes.data || null;

  // {form, initial, base}: the form, the form as it was opened (null on create) and the row it was opened from.
  var stState = React.useState(null);
  var st = stState[0];
  var setSt = stState[1];
  React.useEffect(function () {
    if (st) return;
    if (creating) {
      if (choices.length) setSt({ form: WT_emptyForm(choices[0]), initial: null, base: null });
      return;
    }
    if (row) setSt({ form: WT_formFromTemplate(row), initial: WT_formFromTemplate(row), base: row });
  }, [creating, row, choices.length]);

  var serverState = React.useState({ fields: {}, general: null });
  var server = serverState[0];
  var setServer = serverState[1];
  var attemptedState = React.useState(false);
  var foldState = React.useState({ k8sMore: 0, dockerMore: 0, advanced: 0 });
  var folds = foldState[0];

  var form = st ? st.form : null;
  var kind = form ? form.kind : (row ? WT_kindOf(row) : null);
  var held = row ? WT_heldAdminFields(row) : [];
  var mode = creating ? "create" : row && (WT_isReserved(row) || (!isAdmin && held.length)) ? "view" : "edit";
  var view = mode === "view";
  var live = attemptedState[0] && form && !view ? WT_validateForm(form, { isAdmin: isAdmin, initial: st.initial }) : {};

  function fieldError(key) {
    return server.fields[key] || live[key] || null;
  }
  function openFoldsFor(keys) {
    foldState[1](function (f) {
      var next = Object.assign({}, f);
      Object.keys(WT_FOLD_KEYS).forEach(function (fold) {
        if (keys.some(function (k) { return WT_FOLD_KEYS[fold].indexOf(k) >= 0; })) next[fold] = f[fold] + 1;
      });
      return next;
    });
  }
  function set(key, value) {
    setSt(function (s) {
      var next = Object.assign({}, s.form);
      next[key] = value;
      return Object.assign({}, s, { form: next });
    });
    setServer(function (e) {
      if (!e.fields[key]) return e;
      var fields = Object.assign({}, e.fields);
      delete fields[key];
      return { fields: fields, general: e.general };
    });
  }
  function choiceFor(providerId) {
    return choices.filter(function (c) { return c.id === providerId; })[0] || null;
  }
  // A refusal's field, when this form draws it: a key of the form's kind (the id on create), and never a stored resources.* of a template whose
  // size lives in its backend (Docker reads backend.cpu_cores; a Docker template's resources has no field here).
  function keyForLoc(loc) {
    var key = WT_fieldKeyForLoc(loc);
    if (!key) return null;
    if (WT_cleanLoc(loc)[0] === "resources" && kind !== "kubernetes") return null;
    return key === "id" || WT_formKeys(kind).indexOf(key) >= 0 ? key : null;
  }

  // The server's PUT replaces the whole row, so an edit reads the template again and writes only what this form changed onto what the server holds now:
  // another person's change to a field this form left alone survives (round 2 of #886). A template deleted meanwhile is the GET's 404, and nothing is written.
  var mutation = api.useMutation(function (form) {
    if (creating) return apiFetch("POST", "/workspace_templates", WT_templateBody(null, form, null));
    var path = "/workspace_templates/" + encodeURIComponent(st.base.id);
    var initial = st.initial;
    return apiFetch("GET", path).then(function (fresh) {
      return apiFetch("PUT", path, WT_templateBody(fresh, form, initial));
    });
  }, {
    invalidates: WT_TEMPLATE_KEYS.concat(id ? ["ws:template:" + id] : []),
    onSuccess: function (saved) {
      var savedId = (saved && saved.id) || id;
      con.toast((creating ? "Template created: " : "Template saved: ") + savedId);
      if (props.onSaved) props.onSaved(saved);
      props.onClose();
    },
    onError: function (err) {
      var split = WT_splitErrors(err, keyForLoc, { withPath: true });
      setServer(split);
      openFoldsFor(Object.keys(split.fields));
    },
  });
  var saving = !!mutation.loading;

  function save() {
    if (!st || view) return;
    var errs = WT_validateForm(st.form, { isAdmin: isAdmin, initial: st.initial });
    attemptedState[1](true);
    setServer({ fields: {}, general: null });
    if (Object.keys(errs).length) {
      openFoldsFor(Object.keys(errs));
      return;
    }
    mutation.mutate(st.form).catch(function () { /* onError shows it */ });
  }

  var title = creating ? "New workspace template" : mode === "edit" && st ? "Edit workspace template" : "Workspace template";
  var close = <Btn kind="ghost" data-testid="wt-close" onClick={props.onClose}>Close</Btn>;

  // The states before there is a form.
  var state = null;
  if (!st) {
    if (creating && choicesRes.error && !choices.length) {
      state = (
        <div data-testid="wt-providers-error">
          <Banner kind="error" title="Couldn't load the workspace providers." detail={api.readRefusal(choicesRes.error).message}
            actions={<Btn size="sm" icon="refresh" data-testid="wt-providers-retry" onClick={choicesRes.refetch}>Retry</Btn>} />
        </div>
      );
    } else if (creating && choicesRes.data && !choices.length) {
      state = <div className="field-help" data-testid="wt-no-providers" style={WT_NOTE_STYLE}>No workspace providers yet. An admin registers one under Providers › Workspace.</div>;
    } else if (!creating && rowRes.error && rowRes.error.status === 404) {
      state = <div className="field-help" data-testid="wt-gone" style={WT_NOTE_STYLE}>This template no longer exists.</div>;
    } else if (!creating && rowRes.error) {
      state = (
        <div data-testid="wt-load-error">
          <Banner kind="error" title="Couldn't load the template." detail={api.readRefusal(rowRes.error).message}
            actions={<Btn size="sm" icon="refresh" data-testid="wt-load-retry" onClick={rowRes.refetch}>Retry</Btn>} />
        </div>
      );
    } else {
      state = <div className="nv-bind-empty" role="status" data-testid="wt-overlay-loading">Loading the template…</div>;
    }
    return (
      <PK_FormOverlay title={title} testid="wt-overlay" onClose={props.onClose} footer={{ right: close }}>
        {state}
      </PK_FormOverlay>
    );
  }

  var options = choices.map(function (c) { return { value: c.id, label: c.id + " · " + c.label, iconPath: WT_KIND_ICONS[c.kind] }; });
  if (!choiceFor(form.provider_id)) {
    options = options.concat([{ value: form.provider_id, label: form.provider_id + " · " + (WT_KIND_LABELS[form.kind] || form.kind), iconPath: WT_KIND_ICONS[form.kind] }]);
  }
  var selector = {
    prefix: "Runs on", value: form.provider_id, options: options, locked: !creating, testid: "ws-template-provider",
    onChange: function (providerId) {
      var next = choiceFor(providerId);
      if (!next) return;
      setSt(function (s) { return Object.assign({}, s, { form: WT_formForChoice(s.form, next, choiceFor(s.form.provider_id)) }); });
      setServer({ fields: {}, general: null });
    },
  };
  var footer = view ? { right: close } : {
    left: <Btn kind="ghost" data-testid="wt-cancel" blocked={saving} onClick={props.onClose}>Cancel</Btn>,
    right: (
      <Btn kind="primary" data-testid="wt-save" busy={saving} onClick={save}>
        {saving ? (creating ? "Creating…" : "Saving…") : (creating ? "Create" : "Save")}
      </Btn>
    ),
  };

  var ro = view || undefined;
  var adminOff = !isAdmin || undefined;
  var lockedLines = mode === "edit" ? WT_lockedLineKeys(st.base) : [];
  var LOCKED_LINES = "Read only: a stored item has a line break, is empty or has spaces at its ends, which one item per line cannot show. It is kept as stored.";
  function lineHelp(key, help) {
    return lockedLines.indexOf(key) >= 0 ? LOCKED_LINES : help;
  }
  function lineLock(key) {
    return lockedLines.indexOf(key) >= 0 ? { readOnly: true } : {};
  }
  function input(key, testid, extra) {
    return (
      <input className="input" data-testid={testid} value={form[key]} readOnly={ro} {...(extra || {})}
        onChange={function (e) { set(key, e.target.value); }} />
    );
  }
  function area(key, testid, extra) {
    return (
      <textarea className="input mono" rows={3} data-testid={testid} value={form[key]} readOnly={ro} style={{ width: "100%" }} {...(extra || {})}
        onChange={function (e) { set(key, e.target.value); }} />
    );
  }
  function segmented(key, testid, label, choicesOf, disabled) {
    return (
      <PK_Segmented testid={testid} ariaLabel={label} value={form[key]} disabled={view || !!disabled}
        options={choicesOf.map(function (c) { return { value: c[0], label: c[1] }; })} onChange={function (v) { set(key, v); }} />
    );
  }
  var adminNote = isAdmin ? <div className="field-help" data-testid="ws-template-admin-only-note">{WT_ADMIN_ONLY_NOTE}</div> : null;
  var sizeFields = (
    <React.Fragment>
      <PK_Field label="CPU cores" error={fieldError("cpu")}>{input("cpu", "wt-cpu", { inputMode: "decimal", placeholder: "No limit" })}</PK_Field>
      <PK_Field label="Memory (GiB)" error={fieldError("memoryGiB")}>{input("memoryGiB", "wt-memory", { inputMode: "decimal", placeholder: "No limit" })}</PK_Field>
    </React.Fragment>
  );

  var section = null;
  if (kind === "kubernetes") {
    section = (
      <PK_Section title="KUBERNETES" cols={3} testid="wt-k8s">
        <PK_Field label="Image" span="full" required help="Must include the primer workspace runtime" error={fieldError("image")}>
          {input("image", "ws-template-image", { className: "input mono", "aria-required": "true" })}
        </PK_Field>
        {sizeFields}
        <PK_Field label="Disk (GiB)" error={fieldError("diskGiB")}>{input("diskGiB", "wt-disk", { inputMode: "decimal" })}</PK_Field>
        <PK_Field label="Storage class" error={fieldError("storageClass")}>{input("storageClass", "wt-storage-class", { placeholder: "Cluster default" })}</PK_Field>
        <PK_Field label="Disk access" error={fieldError("diskAccess")}>
          {segmented("diskAccess", "wt-disk-access", "Disk access", [["one", "One node"], ["many", "Many nodes"]])}
        </PK_Field>
        <PK_Field label="Image pull policy" error={fieldError("pullPolicy")}>
          {segmented("pullPolicy", "wt-pull-policy", "Image pull policy", [["", "Default"], ["Always", "Always"], ["IfNotPresent", "IfNotPresent"], ["Never", "Never"]])}
        </PK_Field>
        <PK_Field label="GPU count" error={fieldError("gpuCount")}>{input("gpuCount", "wt-gpu-count", { inputMode: "numeric", placeholder: "None" })}</PK_Field>
        <PK_Field label="GPU resource" help={isAdmin ? undefined : "Admin only"} error={fieldError("gpuResource")}>
          {input("gpuResource", "wt-gpu-resource", { className: "input mono", placeholder: WT_DEFAULT_GPU_RESOURCE, disabled: adminOff })}
        </PK_Field>
        <PK_Field label="Service account" help="Admin only" error={fieldError("serviceAccount")}>
          {input("serviceAccount", "wt-service-account", { className: "input mono", disabled: adminOff })}
        </PK_Field>
        <WT_TableWithNotes error={fieldError("nodeSelectorRows")} table={{
          title: "NODE SELECTOR", testid: "wt-node-selector", addLabel: "+ Add", readOnly: view, rows: form.nodeSelectorRows,
          columns: [{ key: "key", label: "Label", input: "text" }, { key: "value", label: "Value", input: "text" }],
          onChange: function (rows) { set("nodeSelectorRows", rows); },
        }} />
        <WT_TableWithNotes help="Admin only" error={fieldError("tolerationRows")} table={{
          title: "TOLERATIONS", testid: "wt-tolerations", addLabel: "+ Add", readOnly: view || !isAdmin, rows: form.tolerationRows,
          columns: [
            { key: "key", label: "Key", input: "text" }, { key: "operator", label: "Operator", input: "select", options: ["Equal", "Exists"] },
            { key: "value", label: "Value", input: "text" },
            { key: "effect", label: "Effect", input: "select", options: ["Any", "NoSchedule", "PreferNoSchedule", "NoExecute"] },
          ],
          onChange: function (rows) { set("tolerationRows", rows); },
        }} />
        <PK_Fold key={"k8s-more-" + folds.k8sMore} open={folds.k8sMore > 0} label="More" testid="wt-k8s-more">
          <div className="col" style={{ gap: 12 }}>
            {adminNote}
            <PK_Grid cols={2}>
              <PK_Field label="Command" error={fieldError("command")}
                help={lineHelp("command", "One item per line. The default starts the primer workspace runtime; keep it unless your image starts it another way.")}>
                {area("command", "wt-command", lineLock("command"))}
              </PK_Field>
              <PK_Field label="Arguments" help={lineHelp("args", "One item per line")} error={fieldError("args")}>{area("args", "wt-args", lineLock("args"))}</PK_Field>
              <PK_Field label="Working directory" error={fieldError("workdir")}>{input("workdir", "wt-workdir", { className: "input mono", placeholder: "/workspace" })}</PK_Field>
            </PK_Grid>
            <PK_Field label="Extra volumes" help="Admin only. A JSON list of emptyDir or configMap volumes only." error={fieldError("extraVolumesJson")}>
              {area("extraVolumesJson", "wt-extra-volumes", { disabled: adminOff, placeholder: '[{"name": "cache", "emptyDir": {}}]' })}
            </PK_Field>
            <PK_Field label="Volume mounts" help="Admin only. A JSON list of name, mountPath, readOnly, subPath." error={fieldError("extraVolumeMountsJson")}>
              {area("extraVolumeMountsJson", "wt-extra-volume-mounts", { disabled: adminOff, placeholder: '[{"name": "cache", "mountPath": "/cache"}]' })}
            </PK_Field>
            <PK_Grid cols={2}>
              <PK_Field label="Share process namespace" help="Admin only" error={fieldError("podSharePid")}>
                {segmented("podSharePid", "wt-pod-share-pid", "Share process namespace", [["", "Not set"], ["on", "On"], ["off", "Off"]], !isAdmin)}
              </PK_Field>
              <PK_Field label="DNS policy" help="Admin only" error={fieldError("podDns")}>
                <select className="select" data-testid="wt-pod-dns" value={form.podDns} disabled={view || !isAdmin || undefined}
                  onChange={function (e) { set("podDns", e.target.value); }}>
                  {[["", "Not set"], ["ClusterFirst", "ClusterFirst"], ["ClusterFirstWithHostNet", "ClusterFirstWithHostNet"], ["Default", "Default"], ["None", "None"]]
                    .map(function (o) { return <option key={o[0]} value={o[0]}>{o[1]}</option>; })}
                </select>
              </PK_Field>
              <PK_Field label="Restart policy" help="Admin only" error={fieldError("podRestart")}>
                {segmented("podRestart", "wt-pod-restart", "Restart policy", [["", "Not set"], ["Always", "Always"], ["OnFailure", "OnFailure"], ["Never", "Never"]], !isAdmin)}
              </PK_Field>
              <PK_Field label="Termination grace period (s)" help="Admin only" error={fieldError("podGrace")}>
                {input("podGrace", "wt-pod-grace", { inputMode: "numeric", disabled: adminOff })}
              </PK_Field>
            </PK_Grid>
          </div>
        </PK_Fold>
      </PK_Section>
    );
  } else if (kind === "container") {
    section = (
      <PK_Section title="DOCKER" cols={3} testid="wt-docker">
        <PK_Field label="Image" span="full" required error={fieldError("image")}
          help="The built-in runtime image is filled in. A custom image must contain the primer workspace runtime.">
          {input("image", "ws-template-image", { className: "input mono", "aria-required": "true" })}
        </PK_Field>
        {sizeFields}
        <PK_Fold key={"docker-more-" + folds.dockerMore} open={folds.dockerMore > 0} label="More" testid="wt-docker-more">
          <div className="col" style={{ gap: 12 }}>
            {adminNote}
            <PK_Grid cols={2}>
              <PK_Field label="User" error={fieldError("user")}>{input("user", "wt-user", { className: "input mono", placeholder: "uid:gid" })}</PK_Field>
              <PK_Field label="Working directory" error={fieldError("workdir")}>{input("workdir", "wt-workdir", { className: "input mono", placeholder: "/workspace" })}</PK_Field>
            </PK_Grid>
            <WT_TableWithNotes help="Admin only" error={fieldError("extraMountRows")} table={{
              title: "EXTRA MOUNTS", testid: "wt-extra-mounts", addLabel: "+ Add", readOnly: view || !isAdmin, rows: form.extraMountRows,
              columns: [
                { key: "host", label: "Host path", input: "text" }, { key: "container", label: "Container path", input: "text" },
                { key: "readonly", label: "Read only", input: "select", options: ["no", "yes"] },
              ],
              onChange: function (rows) { set("extraMountRows", rows); },
            }} />
          </div>
        </PK_Fold>
      </PK_Section>
    );
  } else {
    section = (
      <div className="field-help" data-testid="wt-local" style={WT_NOTE_STYLE}>
        Runs on the server itself, in the provider's root folder. Local workspaces have no size settings.
      </div>
    );
  }

  var note = null;
  if (view) {
    note = WT_isReserved(row)
      ? "Built in: this template ships with primer and cannot be changed."
      : "This template holds admin-only settings (" + held.join(", ") + "). Only an admin can change or delete it.";
  }

  return (
    <PK_FormOverlay title={title} testid="wt-overlay" onClose={props.onClose} selector={selector} footer={footer}>
      {note ? <div className="field-help" data-testid="wt-readonly-note" style={WT_NOTE_STYLE}>{note}</div> : null}
      {mode === "edit" ? (
        <div className="field-help" data-testid="wt-snapshot-note">
          Changes apply to workspaces made from now on; existing workspaces keep the copy of the template they were made from.
        </div>
      ) : null}
      <PK_Grid cols={2}>
        <PK_Field label="Id" help={creating ? "Made for you if left empty" : undefined} error={fieldError("id")}>
          {input("id", "ws-template-id", { className: "input mono", disabled: !creating || undefined, placeholder: creating ? "python-dev" : undefined })}
        </PK_Field>
        <PK_Field label="Description" required help="Required; shown in the template picker" error={fieldError("description")}>
          {input("description", "ws-template-description", { "aria-required": "true" })}
        </PK_Field>
      </PK_Grid>
      {section}
      <WT_FilesEditor value={form.files} isAdmin={isAdmin} readOnly={view} testid="wt-files" error={fieldError("files")}
        onChange={function (files) { set("files", files); }} />
      <WT_TableWithNotes help="Values are hidden; a saved value is kept unless you replace it" error={fieldError("envRows")} table={{
        title: "ENVIRONMENT", testid: "wt-env", addLabel: "+ Add", readOnly: view, rows: form.envRows,
        columns: [{ key: "key", label: "Name", input: "text" }, { key: "value", label: "Value", input: "secret" }],
        onChange: function (rows) { set("envRows", rows); },
      }} />
      <PK_Field label="Start-up commands" help={lineHelp("init", "One per line; run once when a workspace is created")} error={fieldError("init")}>
        {area("init", "wt-init", Object.assign({ rows: 4 }, lineLock("init")))}
      </PK_Field>
      <PK_Fold key={"advanced-" + folds.advanced} open={folds.advanced > 0} label="Advanced" testid="wt-advanced">
        <PK_Grid cols={2}>
          <PK_Field label="State folder" help="Relative to the workspace root" error={fieldError("statePath")}>
            {input("statePath", "wt-state-path", { className: "input mono" })}
          </PK_Field>
          <PK_Field label="Temp folder" help="Relative to the workspace root" error={fieldError("tmpPath")}>
            {input("tmpPath", "wt-tmp-path", { className: "input mono" })}
          </PK_Field>
          <PK_Field label="Strict write locking" span="full" error={fieldError("strict")}>
            {segmented("strict", "ws-template-strict-write-locking", "Strict write locking", [["off", "Per folder"], ["on", "Whole workspace"]])}
          </PK_Field>
        </PK_Grid>
      </PK_Fold>
      {server.general ? <div className="nv-form-error" role="alert" data-testid="wt-error">{server.general}</div> : null}
    </PK_FormOverlay>
  );
}

window.WT_FilesEditor = WT_FilesEditor;
window.WT_TemplateOverlay = WT_TemplateOverlay;

// ---- WT_TemplatesPage: Platform › Workspace templates (platform revamp Plan 08, Task 11; tests/ui/test_wt_templates_page.py, test_wt_template_verb.py) ----

// A request to open the create overlay (the palette's template.create verb). The verb moves to the page and asks; the page may not be mounted yet, so the
// request waits in a flag the page takes when it mounts, and a page that is mounted hears the `wt:create` event and takes it at once. Taken once.
var WT_CREATE_REQUEST = { pending: false };

function WT_requestCreate() {
  WT_CREATE_REQUEST.pending = true;
  if (typeof window.dispatchEvent === "function" && typeof Event === "function") window.dispatchEvent(new Event("wt:create"));
}

function WT_consumeCreateRequest() {
  var pending = WT_CREATE_REQUEST.pending;
  WT_CREATE_REQUEST.pending = false;
  return pending;
}

// A card's Delete (design 20 Q1): ask, DELETE, say so and refresh the list; a refusal is said through the one reader. A workspace keeps the copy of its
// template it was made from (#829), so the prompt promises nothing about existing workspaces failing, and nothing about a 409 the server never sends.
function WT_deleteTemplate(env, row) {
  return env.confirmDialog({
    title: "Delete " + row.id,
    message: "Permanently delete " + row.id + "? New workspaces can no longer be made from it; existing workspaces keep the copy of the template they were made from.",
    confirmLabel: "Delete",
    danger: true,
  }).then(function (ok) {
    if (!ok) return false;
    return env.apiFetch("DELETE", "/workspace_templates/" + encodeURIComponent(row.id)).then(function () {
      env.toast("Deleted " + row.id);
      env.refetch();
      return true;
    }, function (e) {
      env.toast("Delete refused: " + window.primerApi.readRefusal(e).message, { kind: "error", requestId: e && e.requestId });
      return false;
    });
  });
}

var WT_NOUN = ["workspace template", "workspace templates"];

// The custom Platform page of nav `templates` (seam I-2: NV_CustomPlatPage mounts it with no props, inside the frame that carries
// nv-plat-page:templates, so its own root is wt-templates-page). Every template (WT_fetchAllTemplates), one kit card each, the filter and pager of the
// kit, and the template overlay as local state: a card opens its template, "+ Template" (and the palette verb) a new one.
function WT_TemplatesPage() {
  var con = NV_useConsole();
  var api = window.primerApi;
  var apiFetch = api.apiFetch;
  var isAdmin = con.role === "admin";
  var list = api.useResource(WT_LIST_KEY, function (signal) { return WT_fetchAllTemplates(apiFetch, signal); }, { pollMs: 15000 });
  var choicesRes = api.useResource("ws:provider-choices", function (signal) {
    return apiFetch("GET", "/workspace_providers/_choices", null, { signal: signal });
  }, { pollMs: 0 });
  // The overlay: null, { create: true } or { row }.
  var openState = React.useState(null);
  var open = openState[0];
  var setOpen = openState[1];

  React.useEffect(function () {
    // A request is taken either way, but an overlay that is already open (an edit with unsaved changes, or a create) is left as it is.
    function take() {
      if (WT_consumeCreateRequest()) setOpen(function (o) { return o || { create: true }; });
    }
    take();
    window.addEventListener("wt:create", take);
    return function () { window.removeEventListener("wt:create", take); };
  }, []);

  var items = (list.data && list.data.items) || [];
  var choices = (choicesRes.data && choicesRes.data.items) || [];
  function choiceFor(t) {
    return choices.filter(function (c) { return c.id === t.provider_id; })[0] || null;
  }
  var paged = PK_usePagedFilter(items, {
    pageSize: 12,
    text: function (t) { return [t.id, t.description, t.provider_id, WT_label(t, choiceFor(t)), t.backend && t.backend.image].join(" "); },
  });
  function openCreate() {
    setOpen({ create: true });
  }
  // The templates whose Delete is out (its prompt or its request): a second Delete of one of them does nothing until the first has ended.
  var deletingRef = React.useRef({});
  function remove(row) {
    if (deletingRef.current[row.id]) return;
    deletingRef.current[row.id] = true;
    function done() { delete deletingRef.current[row.id]; }
    WT_deleteTemplate({ confirmDialog: window.confirmDialog, apiFetch: apiFetch, toast: con.toast, refetch: list.refetch }, row).then(done, done);
  }

  var body;
  if (!list.data && list.error) {
    body = (
      <div data-testid="wt-list-error">
        <Banner kind="error" title="Couldn't load workspace templates" detail={api.readRefusal(list.error).message}
          actions={<Btn size="sm" icon="refresh" data-testid="wt-retry" onClick={list.refetch}>Retry</Btn>} />
      </div>
    );
  } else if (!list.data) {
    body = <div className="nv-plat-state" role="status" data-testid="wt-loading">Loading workspace templates…</div>;
  } else if (!paged.visible.length) {
    var filtered = !!paged.q.trim() && items.length > 0;
    body = (
      <div className="nv-plat-empty" data-testid="wt-empty">
        <div>{window.NV_emptyText(WT_NOUN, paged.q, items.length)}</div>
        {filtered ? (
          <button type="button" className="nv-btn-secondary" data-testid="wt-clear-filter" onClick={function () { paged.setQ(""); }}>Clear filter</button>
        ) : (
          <Btn kind="primary" data-testid="wt-empty-create" data-verb="template.create" onClick={openCreate}>+ Template</Btn>
        )}
      </div>
    );
  } else {
    body = (
      <PK_CardGrid testid="wt-grid">
        {paged.visible.map(function (t) {
          var reserved = WT_isReserved(t);
          var locked = reserved || (!isAdmin && WT_heldAdminFields(t).length > 0);
          return (
            <PK_Card key={t.id} id={t.id} icon={{ path: WT_KIND_ICONS[WT_kindOf(t)] }} badge={reserved ? "built in" : undefined}
              title={t.id} subtitle={t.description || undefined} facts={WT_cardFacts(t, choiceFor(t))}
              onOpen={function () { setOpen({ row: t }); }}
              menu={locked ? undefined : [{ label: "Delete", danger: true, testid: "wt-delete:" + t.id, onClick: function () { remove(t); } }]} />
          );
        })}
      </PK_CardGrid>
    );
  }

  return (
    <div className="col" data-testid="wt-templates-page" style={{ gap: 16 }}>
      <PK_PageHeader
        title="Workspace templates"
        count={window.NV_countText(WT_NOUN, paged.total, items.length)}
        filter={{ value: paged.q, onChange: paged.setQ, label: "Filter workspace templates", testid: "wt-filter" }}
        pager={{ page: paged.page, pages: paged.pages, from: paged.from, to: paged.to, total: paged.total, onPage: paged.setPage }}
        action={{ label: "+ Template", onClick: openCreate, testid: "wt-new" }} />
      {body}
      {open ? (
        <WT_TemplateOverlay key={open.create ? "new" : "row:" + open.row.id} create={!!open.create} row={open.row || null}
          onClose={function () { setOpen(null); }} />
      ) : null}
    </div>
  );
}

window.WT_requestCreate = WT_requestCreate;
window.WT_consumeCreateRequest = WT_consumeCreateRequest;
window.WT_deleteTemplate = WT_deleteTemplate;
window.WT_TemplatesPage = WT_TemplatesPage;

// ---- ContainerMountEditor: list of {host, container, readonly} rows.
// Matches the ContainerMount pydantic model used by ContainerTemplateConfig
// (host path → container mount point, optional read-only flag).
function ContainerMountEditor({ value, onChange }) {
  const arr = Array.isArray(value) ? value : [];
  const setAt = (i, patch) => {
    const next = arr.slice();
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  const remove = (i) => {
    const next = arr.slice();
    next.splice(i, 1);
    onChange(next);
  };
  const add = () => onChange([...arr, { host: "", container: "", readonly: false }]);
  return (
    <div className="col" style={{ gap: 6 }}>
      {arr.map((m, i) => (
        <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto auto", gap: 6, alignItems: "center" }}>
          <input className="input mono" placeholder="/host/path" value={m.host || ""} onChange={(e) => setAt(i, { host: e.target.value })} />
          <input className="input mono" placeholder="/container/path" value={m.container || ""} onChange={(e) => setAt(i, { container: e.target.value })} />
          <label className="mono text-sm" style={{ display: "inline-flex", gap: 4, alignItems: "center", color: "var(--text-3)" }}>
            <input type="checkbox" checked={!!m.readonly} onChange={(e) => setAt(i, { readonly: e.target.checked })} />
            ro
          </label>
          <button className="icon-btn" style={{ width: 26, height: 26 }} onClick={() => remove(i)} title="Remove"><Icon name="x" size={10} /></button>
        </div>
      ))}
      <button className="btn" style={{ alignSelf: "flex-start", padding: "4px 10px" }} onClick={add}>
        <Icon name="plus" size={11} /> Add mount
      </button>
    </div>
  );
}

// The rows of the template form that show a 422's field error, per backend kind (a row reads the error by the last segment of its loc,
// or by its full path). Any other field error is listed in the line above the fields, so no refusal is lost (the review of #869, F4).
function _wtFieldErrorShown(kind, path, last) {
  if (last === "provider_id") return true;
  if (kind === "container") return last === "cpu_cores" || last === "memory_bytes";
  if (kind === "kubernetes") return path === "resources.cpu_cores" || path === "resources.memory_bytes" || last === "pvc_size";
  return false;
}

function _wtToastErr(pushToast, fallback) {
  return (err) => {
    if (typeof pushToast !== "function") return;
    pushToast({
      kind: "error",
      title: err?.title || fallback,
      detail: err?.detail || err?.message,
      requestId: err?.requestId,
    });
  };
}

function WorkspaceTemplatesPage({ pushToast }) {
  const { useRouter, useViewport, usePagedList, Pager } = window.primerApi;
  const { navigate } = useRouter();
  const { isMobile } = useViewport();
  const [createOpen, setCreateOpen] = React.useState(false);

  // Server-side offset pagination (bug #19).
  const list = usePagedList({
    key: WT_LIST_KEY,
    path: "/workspace_templates",
    pageSize: 50,
    pollMs: 5000,
  });
  const items = list.items;

  const modal = createOpen ? (
    <WorkspaceTemplateCreateModal
      onClose={() => setCreateOpen(false)}
      pushToast={pushToast}
    />
  ) : null;

  if (!list.loading && items.length === 0 && !list.error) {
    return (
      <>
        <div className="panel">
          <div className="empty">
            <div className="ico-wrap"><Icon name="tools" size={22} /></div>
            <div className="head">No workspace templates</div>
            <div className="sub">
              A WorkspaceTemplate is the declarative recipe — files, env, init commands — that materialises into a workspace. Each template targets a previously-registered WorkspaceProvider.
            </div>
            <div className="actions">
              <Btn kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New workspace template</Btn>
            </div>
          </div>
        </div>
        {modal}
      </>
    );
  }

  return (
    <>
      <div className="col" style={{ gap: 14 }}>
        <div className="filter-bar">
          <span className="muted text-sm tabular" style={{ marginLeft: "auto" }}>
            <span className="mono" style={{ color: "var(--green)" }}>● live</span> · /v1/workspace_templates every 5s
          </span>
          <Btn size="sm" kind="primary" icon="plus" onClick={() => setCreateOpen(true)}>New template</Btn>
        </div>
        {list.error && items.length === 0 ? (
          <Banner kind="error" title={list.error.title || "Couldn't load templates"} detail={list.error.detail || list.error.message} actions={<Btn size="sm" icon="refresh" onClick={list.refetch}>Retry</Btn>} />
        ) : isMobile ? (
          <CardList
            items={items}
            empty="No workspace templates."
            renderCard={(t) => (
              <Card
                title={t.id}
                subtitle={t.description || t.provider_id}
                pill={<window.WorkspaceBackendBadge kind={t.backend?.kind} />}
                meta={t.provider_id}
                onClick={() => navigate(`/workspaces/templates/${encodeURIComponent(t.id)}`)}
              />
            )}
          />
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Description</th>
                  <th>Backend</th>
                  <th>Provider</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((t) => (
                  <tr key={t.id} onClick={() => navigate(`/workspaces/templates/${encodeURIComponent(t.id)}`)} style={{ cursor: "pointer" }}>
                    <td className="mono">{t.id}</td>
                    <td className="muted">{t.description || <span style={{ color: "var(--text-4)" }}>—</span>}</td>
                    <td><window.WorkspaceBackendBadge kind={t.backend?.kind} /></td>
                    <td className="mono muted text-sm">
                      <a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={(e) => { e.stopPropagation(); navigate(`/workspaces/providers/${encodeURIComponent(t.provider_id)}`); }}>{t.provider_id}</a>
                    </td>
                    <td style={{ textAlign: "right", paddingRight: 12 }}><Icon name="chevron-right" size={12} className="muted" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager pager={list} label="templates" />
        {isMobile && (
          <Fab icon="plus" label="New template" onClick={() => setCreateOpen(true)} />
        )}
      </div>
      {modal}
    </>
  );
}

// ---- WT_Toggle: sliding switch, mirrors CH_Toggle (channels.jsx) /
// SSO_Toggle (sso_admin.jsx) / AG_Toggle (agents.jsx). Defined locally
// rather than referenced via window.* since templates.jsx loads BEFORE
// sso_admin.jsx per the <script> order in ui/index.html.
function WT_Toggle({ checked, onChange, label, help, disabled, "data-testid": testid }) {
  return (
    <label
      style={{
        display: "flex", alignItems: "flex-start", gap: 10,
        cursor: disabled ? "default" : "pointer", opacity: disabled ? 0.5 : 1,
      }}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        data-testid={testid}
        onClick={() => !disabled && onChange(!checked)}
        style={{
          flex: "0 0 auto", width: 34, height: 20, borderRadius: 999,
          border: "1px solid var(--border)", padding: 0, marginTop: 1,
          background: checked ? "var(--accent)" : "var(--bg-2)",
          position: "relative", cursor: disabled ? "default" : "pointer",
          transition: "background 0.12s ease",
        }}
      >
        <span
          style={{
            position: "absolute", top: 1, left: checked ? 15 : 1,
            width: 16, height: 16, borderRadius: "50%",
            background: checked ? "var(--accent-fg)" : "var(--text-3)",
            transition: "left 0.12s ease",
          }}
        />
      </button>
      <span style={{ fontSize: 12.5, lineHeight: 1.4 }}>
        {label}
        {help && <span className="muted"> - {help}</span>}
      </span>
    </label>
  );
}

function _emptyForm(provider) {
  return {
    id: "",
    description: "",
    provider_id: provider?.id || "",
    backend_kind: provider?.provider || "local",

    // ---- container variant fields ----
    c_image: "",
    c_entrypoint: [],
    c_user: "",
    c_workdir: "/workspace",
    c_cpu_cores: "",
    c_memory_bytes: "",
    c_extra_mounts: [],
    c_network_egress: "",  // "" = runtime default (null), "allow_all", "deny_all"

    // ---- kubernetes variant fields ----
    k_image: "",
    k_entrypoint: [],
    k_args: [],
    k_workdir: "/workspace",
    k_cpu_cores: "",
    k_memory_bytes: "",
    k_pvc_size: "10Gi",
    k_pvc_access_modes: ["ReadWriteOnce"],
    k_storage_class: "",
    k_extra_volumes: null,
    k_extra_volume_mounts: null,
    k_network_policy_name: "",
    k_pod_overrides: null,
    k_container_security_context_overrides: null,

    // ---- shared recipe ----
    files: [],
    env: {},
    init_commands: "",
    state_path: ".state",
    tmp_path: ".tmp",
    strict_write_locking: false,
  };
}

function _fromTemplate(t, providers) {
  const provider = (providers || []).find((p) => p.id === t.provider_id);
  const backend = t.backend || { kind: "local" };
  const kind = backend.kind || (provider?.provider) || "local";
  const isC = kind === "container";
  const isK = kind === "kubernetes";
  return {
    id: t.id,
    description: t.description || "",
    provider_id: t.provider_id,
    backend_kind: kind,

    // ---- container variant fields ----
    c_image: isC ? (backend.image || "") : "",
    c_entrypoint: isC ? (backend.entrypoint || []) : [],
    c_user: isC ? (backend.user || "") : "",
    c_workdir: isC ? (backend.workdir || "/workspace") : "/workspace",
    c_cpu_cores: isC && backend.cpu_cores != null ? String(backend.cpu_cores) : "",
    c_memory_bytes: isC && backend.memory_bytes != null ? String(backend.memory_bytes) : "",
    c_extra_mounts: isC ? (backend.extra_mounts || []) : [],
    c_network_egress: isC ? ((backend.network && backend.network.egress) || "") : "",

    // ---- kubernetes variant fields ----
    k_image: isK ? (backend.image || "") : "",
    k_entrypoint: isK ? (backend.entrypoint || []) : [],
    k_args: isK ? (backend.args || []) : [],
    k_workdir: isK ? (backend.workdir || "/workspace") : "/workspace",
    k_cpu_cores: isK && t.resources && t.resources.cpu_cores != null ? String(t.resources.cpu_cores) : "",
    k_memory_bytes: isK && t.resources && t.resources.memory_bytes != null ? String(t.resources.memory_bytes) : "",
    k_pvc_size: isK ? (backend.pvc_size || "10Gi") : "10Gi",
    k_pvc_access_modes: isK ? (backend.pvc_access_modes || ["ReadWriteOnce"]) : ["ReadWriteOnce"],
    k_storage_class: isK ? (backend.storage_class || "") : "",
    k_extra_volumes: isK ? (backend.extra_volumes && backend.extra_volumes.length ? backend.extra_volumes : null) : null,
    k_extra_volume_mounts: isK ? (backend.extra_volume_mounts && backend.extra_volume_mounts.length ? backend.extra_volume_mounts : null) : null,
    k_network_policy_name: isK ? (backend.network_policy_name || "") : "",
    k_pod_overrides: isK ? (backend.pod_overrides || null) : null,
    k_container_security_context_overrides: isK ? (backend.container_security_context_overrides || null) : null,

    // ---- shared recipe ----
    files: t.files || [],
    env: t.env || {},
    init_commands: (t.init_commands || []).join("\n"),
    state_path: t.state_path || ".state",
    tmp_path: t.tmp_path || ".tmp",
    strict_write_locking: !!t.strict_write_locking,
  };
}

function WorkspaceTemplateCreateModal({ onClose, pushToast, existing }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();

  const providers = useResource(
    "ws:templates:providers-picker",
    (signal) => apiFetch("GET", "/workspace_providers?limit=200", null, { signal }),
    {}
  );
  const providerItems = Array.isArray(providers.data?.items) ? providers.data.items : [];

  const isEdit = !!existing;
  const [form, setForm] = React.useState(() => isEdit ? _fromTemplate(existing, providerItems) : _emptyForm(providerItems[0]));
  const [fieldErrors, setFieldErrors] = React.useState({});
  const [formErrors, setFormErrors] = React.useState([]);
  const [advancedOpen, setAdvancedOpen] = React.useState(false);

  React.useEffect(() => {
    if (isEdit) return;
    if (!form.provider_id && providerItems.length > 0) {
      const p = providerItems[0];
      setForm((f) => ({ ...f, provider_id: p.id, backend_kind: p.provider }));
    }
  }, [providerItems.length, isEdit]);

  const update = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const onProviderChange = (pid) => {
    const p = providerItems.find((x) => x.id === pid);
    setForm((f) => ({ ...f, provider_id: pid, backend_kind: p ? p.provider : f.backend_kind }));
  };

  const mutation = useMutation(
    (body) => apiFetch(isEdit ? "PUT" : "POST", isEdit ? `/workspace_templates/${encodeURIComponent(existing.id)}` : "/workspace_templates", body),
    {
      invalidates: [WT_LIST_KEY, ...(isEdit ? [`ws:template:${existing.id}`] : [])],
      onSuccess: (row) => {
        onClose();
        if (pushToast) pushToast({ kind: "success", title: isEdit ? "Template updated" : "Template created", detail: row.id });
        if (!isEdit) navigate(`/workspaces/templates/${encodeURIComponent(row.id)}`);
      },
      onError: (err) => {
        if (err?.status === 422 && Array.isArray(err.fieldErrors)) {
          const map = {};
          const unshown = [];
          for (const fe of err.fieldErrors) {
            const loc = (fe.loc || []).filter((s) => s !== "body");
            // The backend's discriminator tag ("backend.kubernetes.pvc_size") is not a path the operator typed.
            const path = loc.filter((s, i) => !(i === 1 && loc[0] === "backend" && s === form.backend_kind)).join(".");
            const last = loc.length > 0 ? loc[loc.length - 1] : "";
            map[path] = fe.msg;
            if (last) map[last] = fe.msg;
            if (!_wtFieldErrorShown(form.backend_kind, path, last)) unshown.push({ key: path || "body", label: path, msg: fe.msg });
          }
          setFieldErrors(map);
          setFormErrors(unshown);
        } else {
          _wtToastErr(pushToast, isEdit ? "Update failed" : "Create failed")(err);
        }
      },
    },
  );

  const submit = () => {
    const errs = {};
    if (!form.provider_id) errs.provider_id = "value is required";
    if (form.backend_kind === "container" && !form.c_image) errs.c_image = "value is required";
    if (form.backend_kind === "kubernetes" && !form.k_image) errs.k_image = "value is required";
    if (Object.keys(errs).length > 0) {
      setFieldErrors(errs);
      setFormErrors([]);
      return;
    }
    setFieldErrors({});
    setFormErrors([]);

    let backend;
    if (form.backend_kind === "local") {
      backend = { kind: "local" };
    } else if (form.backend_kind === "container") {
      backend = {
        kind: "container",
        image: form.c_image,
        workdir: form.c_workdir || "/workspace",
        extra_mounts: form.c_extra_mounts || [],
      };
      if ((form.c_entrypoint || []).length > 0) backend.entrypoint = form.c_entrypoint;
      if (form.c_user) backend.user = form.c_user;
      if (form.c_cpu_cores !== "" && form.c_cpu_cores != null) {
        const n = Number(form.c_cpu_cores);
        if (!Number.isNaN(n)) backend.cpu_cores = n;
      }
      if (form.c_memory_bytes !== "" && form.c_memory_bytes != null) {
        const n = Number(form.c_memory_bytes);
        if (!Number.isNaN(n)) backend.memory_bytes = Math.trunc(n);
      }
      if (form.c_network_egress) {
        backend.network = { egress: form.c_network_egress };
      }
    } else {
      backend = {
        kind: "kubernetes",
        image: form.k_image,
        workdir: form.k_workdir || "/workspace",
        pvc_size: form.k_pvc_size || "10Gi",
        pvc_access_modes: form.k_pvc_access_modes || ["ReadWriteOnce"],
      };
      if ((form.k_entrypoint || []).length > 0) backend.entrypoint = form.k_entrypoint;
      if ((form.k_args || []).length > 0) backend.args = form.k_args;
      if (form.k_storage_class) backend.storage_class = form.k_storage_class;
      if (Array.isArray(form.k_extra_volumes) && form.k_extra_volumes.length > 0) backend.extra_volumes = form.k_extra_volumes;
      if (Array.isArray(form.k_extra_volume_mounts) && form.k_extra_volume_mounts.length > 0) backend.extra_volume_mounts = form.k_extra_volume_mounts;
      if (form.k_network_policy_name) backend.network_policy_name = form.k_network_policy_name;
      if (form.k_pod_overrides != null) backend.pod_overrides = form.k_pod_overrides;
      if (form.k_container_security_context_overrides != null) {
        backend.container_security_context_overrides = form.k_container_security_context_overrides;
      }
    }

    const initCmds = (form.init_commands || "").split("\n").map((s) => s.trim()).filter(Boolean);

    const edited = {
      ...(form.id ? { id: form.id } : {}),
      description: form.description || null,
      provider_id: form.provider_id,
      backend,
      files: form.files || [],
      env: form.env || {},
      init_commands: initCmds,
      state_path: form.state_path || ".state",
      tmp_path: form.tmp_path || ".tmp",
      strict_write_locking: !!form.strict_write_locking,
    };
    if (form.backend_kind === "kubernetes") {
      const resources = { ...((isEdit && existing.resources) || {}) };
      resources.cpu_cores = form.k_cpu_cores !== "" && form.k_cpu_cores != null ? Number(form.k_cpu_cores) : null;
      resources.memory_bytes = form.k_memory_bytes !== "" && form.k_memory_bytes != null ? Math.trunc(Number(form.k_memory_bytes)) : null;
      edited.resources = resources;
    }
    const body = isEdit ? {
      ...existing,
      ...edited,
      backend: { ...(existing.backend || {}), ...backend },
      ...(edited.resources ? { resources: { ...(existing.resources || {}), ...edited.resources } } : {}),
    } : edited;
    mutation.mutate(body).catch(() => { /* onError handled */ });
  };

  const isLocal = form.backend_kind === "local";
  const isContainer = form.backend_kind === "container";
  const isK8s = form.backend_kind === "kubernetes";

  return (
    <Modal
      title={isEdit ? `Edit ${existing.id}` : "New workspace template"}
      testid="ws-template-modal"
      onClose={onClose}
      footer={
        <>
          <Btn kind="ghost" onClick={onClose}>Cancel</Btn>
          <Btn kind="primary" icon={isEdit ? "save" : "plus"} busy={mutation.loading} onClick={submit}>
            {mutation.loading ? (isEdit ? "Saving…" : "Creating…") : (isEdit ? "Save" : "Create")}
          </Btn>
        </>
      }
    >
      {formErrors.length > 0 && (
        <div role="alert" data-testid="ws-template-form-errors" style={{ marginBottom: 12 }}>
          <Banner
            kind="error"
            title={isEdit ? "Could not save the template" : "Could not create the template"}
            detail={formErrors.map((e) => (
              <div key={e.key}>
                {e.label ? <><span className="mono">{e.label}</span>{": "}</> : null}
                {e.msg}
              </div>
            ))}
          />
        </div>
      )}
      {!isEdit && (
        <WS_FieldRow label="id" hint="optional · backend assigns if blank">
          <input className="input mono" value={form.id} onChange={(e) => update("id", e.target.value)} placeholder="dev-template" style={{ width: "100%" }} />
        </WS_FieldRow>
      )}
      <WS_FieldRow label="description">
        <input className="input mono" value={form.description} onChange={(e) => update("description", e.target.value)} placeholder="dev workspace" style={{ width: "100%" }} data-testid="ws-template-description" />
      </WS_FieldRow>
      <WS_FieldRow label="provider" hint={isEdit ? "immutable on edit" : "drives the backend section"} err={fieldErrors.provider_id}>
        {providerItems.length === 0 ? (
          <div className="banner banner-warning" style={{ margin: 0, fontSize: 11.5 }}>
            <Icon name="alert" size={12} className="ico" />
            <div>No workspace_providers registered. Register one before creating a template.</div>
          </div>
        ) : (
          <select
            className="select mono"
            value={form.provider_id}
            onChange={(e) => onProviderChange(e.target.value)}
            disabled={isEdit}
            style={{ width: "100%" }}
            data-testid="ws-template-provider"
          >
            {providerItems.map((p) => <option key={p.id} value={p.id}>{p.id} ({p.provider})</option>)}
          </select>
        )}
      </WS_FieldRow>

      {isLocal && (
        <Banner kind="info" title="Local backend" detail="Local templates have no backend-specific fields beyond the shared recipe (files, env, init_commands)." />
      )}

      {isContainer && (<>
        <WS_Section label="Container template" sub="image, resources, mounts, network" />
        <WS_FieldRow label="image" err={fieldErrors.c_image}>
          <input className="input mono" value={form.c_image} onChange={(e) => update("c_image", e.target.value)} placeholder="ghcr.io/primer/runtime:latest" style={{ width: "100%" }} data-testid="ws-template-image" />
        </WS_FieldRow>
        <WS_FieldRow label="entrypoint" hint="optional · overrides image ENTRYPOINT">
          <window.WorkspaceStringListEditor value={form.c_entrypoint} onChange={(v) => update("c_entrypoint", v)} placeholder="bash" />
        </WS_FieldRow>
        <WS_FieldRow label="user" hint="optional · uid:gid or username">
          <input className="input mono" value={form.c_user} onChange={(e) => update("c_user", e.target.value)} placeholder="1000:1000" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="workdir">
          <input className="input mono" value={form.c_workdir} onChange={(e) => update("c_workdir", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <WS_FieldRow label="cpu_cores" hint="optional · docker --cpus value" err={fieldErrors.cpu_cores}>
            <input className="input mono" type="number" step="0.1" min="0.1" value={form.c_cpu_cores} onChange={(e) => update("c_cpu_cores", e.target.value)} placeholder="2" style={{ width: "100%" }} />
          </WS_FieldRow>
          <WS_FieldRow label="memory_bytes" hint="optional · integer bytes" err={fieldErrors.memory_bytes}>
            <input className="input mono" type="number" step="1" min="1" value={form.c_memory_bytes} onChange={(e) => update("c_memory_bytes", e.target.value)} placeholder="2147483648" style={{ width: "100%" }} />
          </WS_FieldRow>
        </div>
        <div className="field-help" data-testid="ws-template-admin-only-note">
          <Icon name="lock" size={11} style={{ verticalAlign: -1, marginRight: 3 }} />
          {WT_ADMIN_ONLY_NOTE}
        </div>
        <WS_FieldRow label="extra_mounts" hint="host → container mounts · admin only">
          <ContainerMountEditor value={form.c_extra_mounts} onChange={(v) => update("c_extra_mounts", v)} />
        </WS_FieldRow>
        <WS_FieldRow label="network.egress" hint="null = runtime default · deny_all = --internal network">
          <select className="select mono" value={form.c_network_egress} onChange={(e) => update("c_network_egress", e.target.value)} style={{ width: "100%" }}>
            <option value="">(runtime default)</option>
            <option value="allow_all">allow_all</option>
            <option value="deny_all">deny_all</option>
          </select>
        </WS_FieldRow>
      </>)}

      {isK8s && (<>
        <WS_Section label="Kubernetes template" sub="image, resources, PVC, overrides" />
        <WS_FieldRow label="image" err={fieldErrors.k_image}>
          <input className="input mono" value={form.k_image} onChange={(e) => update("k_image", e.target.value)} placeholder="ghcr.io/primer/runtime:latest" style={{ width: "100%" }} data-testid="ws-template-image" />
        </WS_FieldRow>
        <WS_FieldRow label="entrypoint" hint="optional">
          <window.WorkspaceStringListEditor value={form.k_entrypoint} onChange={(v) => update("k_entrypoint", v)} placeholder="bash" />
        </WS_FieldRow>
        <WS_FieldRow label="args" hint="optional">
          <window.WorkspaceStringListEditor value={form.k_args} onChange={(v) => update("k_args", v)} placeholder="-c" />
        </WS_FieldRow>
        <WS_FieldRow label="workdir">
          <input className="input mono" value={form.k_workdir} onChange={(e) => update("k_workdir", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <WS_FieldRow label="cpu_cores" hint="optional · requests and limits" err={fieldErrors["resources.cpu_cores"]}>
            <input className="input mono" type="number" step="0.1" min="0.1" value={form.k_cpu_cores} onChange={(e) => update("k_cpu_cores", e.target.value)} placeholder="2" style={{ width: "100%" }} data-testid="ws-template-cpu-cores" />
          </WS_FieldRow>
          <WS_FieldRow label="memory_bytes" hint="optional · requests and limits" err={fieldErrors["resources.memory_bytes"]}>
            <input className="input mono" type="number" step="1" min="1" value={form.k_memory_bytes} onChange={(e) => update("k_memory_bytes", e.target.value)} placeholder="2147483648" style={{ width: "100%" }} data-testid="ws-template-memory-bytes" />
          </WS_FieldRow>
        </div>
        <WS_FieldRow label="pvc_size" hint="a Kubernetes quantity · more than zero, at most 64Ti" err={fieldErrors.pvc_size}>
          <input className="input mono" value={form.k_pvc_size} onChange={(e) => update("k_pvc_size", e.target.value)} placeholder="10Gi" style={{ width: "100%" }} data-testid="ws-template-pvc-size" />
        </WS_FieldRow>
        <WS_FieldRow label="pvc_access_modes">
          <window.WorkspaceStringListEditor value={form.k_pvc_access_modes} onChange={(v) => update("k_pvc_access_modes", v)} placeholder="ReadWriteOnce" />
        </WS_FieldRow>
        <WS_FieldRow label="storage_class" hint="optional · null = cluster default">
          <input className="input mono" value={form.k_storage_class} onChange={(e) => update("k_storage_class", e.target.value)} placeholder="" style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="network_policy_name" hint="optional · pre-existing NetworkPolicy in the namespace">
          <input className="input mono" value={form.k_network_policy_name} onChange={(e) => update("k_network_policy_name", e.target.value)} placeholder="" style={{ width: "100%" }} />
        </WS_FieldRow>
        <div style={{ marginTop: 12 }}>
          <button className="btn" style={{ padding: "4px 10px" }} onClick={() => setAdvancedOpen(!advancedOpen)}>
            <Icon name={advancedOpen ? "chevron-down" : "chevron-right"} size={11} />
            <span>Advanced (extra volumes, pod / container overrides)</span>
          </button>
        </div>
        {advancedOpen && (<>
          <div className="field-help" data-testid="ws-template-admin-only-note">
            <Icon name="lock" size={11} style={{ verticalAlign: -1, marginRight: 3 }} />
            {WT_ADMIN_ONLY_NOTE}
          </div>
          <WS_FieldRow label="extra_volumes" hint="JSON array · emptyDir or configMap volumes only · admin only">
            <window.WorkspaceJsonTextareaField value={form.k_extra_volumes} onChange={(v) => update("k_extra_volumes", v)} placeholder='[{"name": "cache", "emptyDir": {}}]' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="extra_volume_mounts" hint="JSON array · name, mountPath, readOnly, subPath · admin only">
            <window.WorkspaceJsonTextareaField value={form.k_extra_volume_mounts} onChange={(v) => update("k_extra_volume_mounts", v)} placeholder='[{"name": "cache", "mountPath": "/cache"}]' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="pod_overrides" hint="JSON object · only shareProcessNamespace, dnsPolicy, restartPolicy, terminationGracePeriodSeconds · admin only">
            <window.WorkspaceJsonTextareaField value={form.k_pod_overrides} onChange={(v) => update("k_pod_overrides", v)} placeholder='{"restartPolicy": "Always"}' rows={4} />
          </WS_FieldRow>
          <WS_FieldRow label="container_security_context_overrides" hint="not applied by the backend · any non-empty value is refused">
            <window.WorkspaceJsonTextareaField value={form.k_container_security_context_overrides} onChange={(v) => update("k_container_security_context_overrides", v)} placeholder="{}" rows={4} />
          </WS_FieldRow>
        </>)}
      </>)}

      <WS_Section label="Recipe" sub="files, env, init commands — shared across backends" />
      <WS_FieldRow label="files" hint="inline-text only · url sources via API · secret sources are admin only">
        <window.WorkspaceFileRowEditor value={form.files} onChange={(v) => update("files", v)} />
      </WS_FieldRow>
      <WS_FieldRow label="env" hint="key/value pairs · values stored encrypted as SecretStr">
        <window.WorkspaceEnvPairEditor value={form.env} onChange={(v) => update("env", v)} />
      </WS_FieldRow>
      <WS_FieldRow label="init_commands" hint="one shell command per line · failure aborts materialisation">
        <textarea className="input mono" style={{ width: "100%", fontSize: 12, minHeight: 90 }} value={form.init_commands} onChange={(e) => update("init_commands", e.target.value)} placeholder="apt-get install -y git" />
      </WS_FieldRow>

      <WS_Section label="Paths" />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
        <WS_FieldRow label="state_path" hint="relative to workspace root">
          <input className="input mono" value={form.state_path} onChange={(e) => update("state_path", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
        <WS_FieldRow label="tmp_path" hint="relative to workspace root">
          <input className="input mono" value={form.tmp_path} onChange={(e) => update("tmp_path", e.target.value)} style={{ width: "100%" }} />
        </WS_FieldRow>
      </div>
      <WS_FieldRow label="strict write locking" hint="workspace-wide lock domain instead of per-workdir">
        <WT_Toggle
          checked={!!form.strict_write_locking}
          onChange={(v) => update("strict_write_locking", v)}
          label="Serialize all writers workspace-wide"
          help="closes the cross-workdir overlap gap; reduces parallelism"
          data-testid="ws-template-strict-write-locking"
        />
      </WS_FieldRow>
    </Modal>
  );
}

function WorkspaceTemplateDetail({ templateId, pushToast }) {
  const { useResource, useMutation, useRouter, apiFetch } = window.primerApi;
  const { navigate } = useRouter();
  const [tab, setTab] = React.useState("overview");
  const [editOpen, setEditOpen] = React.useState(false);
  const [showDelete, setShowDelete] = React.useState(false);
  const [deleteError, setDeleteError] = React.useState(null);

  const detailKey = `ws:template:${templateId}`;
  const detail = useResource(
    detailKey,
    (signal) => apiFetch("GET", `/workspace_templates/${encodeURIComponent(templateId)}`, null, { signal }),
    { deps: [templateId] }
  );

  const workspaces = useResource(
    `ws:template:${templateId}:workspaces`,
    (signal) => apiFetch("GET", "/workspaces?limit=200", null, { signal }),
    { deps: [templateId] }
  );
  const referencingWorkspaces = React.useMemo(() => {
    const all = workspaces.data?.items ?? [];
    return all.filter((w) => w.template_id === templateId);
  }, [workspaces.data, templateId]);

  const del = useMutation(
    () => apiFetch("DELETE", `/workspace_templates/${encodeURIComponent(templateId)}`),
    {
      invalidates: [WT_LIST_KEY],
      onSuccess: () => {
        if (pushToast) pushToast({ kind: "warning", title: "Template deleted", detail: templateId });
        setShowDelete(false);
        navigate("/workspaces/templates");
      },
      onError: (err) => {
        if (err?.status === 409) {
          setDeleteError(err.detail || "Cannot delete — workspaces still reference this template.");
        } else {
          setShowDelete(false);
          _wtToastErr(pushToast, "Delete failed")(err);
        }
      },
    },
  );

  if (detail.loading && !detail.data) {
    return <div className="panel"><div className="panel-body" style={{ padding: 18 }}><span className="muted text-sm">Loading template…</span></div></div>;
  }
  if (detail.error && !detail.data) {
    return <Banner kind="error" title={detail.error.title || `Couldn't load ${templateId}`} detail={detail.error.detail || detail.error.message} actions={<Btn size="sm" icon="refresh" onClick={detail.refetch}>Retry</Btn>} />;
  }
  const t = detail.data;
  if (!t) return null;

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="panel">
        <div className="panel-body" style={{ padding: "14px 18px", display: "flex", alignItems: "center", gap: 14 }}>
          <window.WorkspaceBackendBadge kind={t.backend?.kind || "local"} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{t.id}</div>
            <div className="muted text-sm mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {t.description || <span style={{ color: "var(--text-4)" }}>—</span>} · provider {t.provider_id}
            </div>
          </div>
          <Btn size="sm" kind="ghost" icon="edit" onClick={() => setEditOpen(true)}>Edit</Btn>
          <Btn size="sm" kind="danger" icon="trash" onClick={() => { setDeleteError(null); setShowDelete(true); }}>Delete</Btn>
        </div>
        <div style={{ display: "flex", alignItems: "center", borderTop: "1px solid var(--border)", borderBottom: "1px solid var(--border)", padding: "0 12px" }}>
          {[
            { id: "overview", label: "Overview", icon: "info" },
            { id: "recipe", label: "Recipe", icon: "settings" },
            { id: "workspaces", label: "Workspaces", icon: "box", count: referencingWorkspaces.length },
          ].map((tt) => (
            <button
              key={tt.id}
              onClick={() => setTab(tt.id)}
              style={{
                background: "none", border: "none",
                padding: "10px 14px", cursor: "pointer",
                color: tab === tt.id ? "var(--text)" : "var(--text-3)",
                fontSize: 12.5, fontWeight: tab === tt.id ? 600 : 400,
                borderBottom: tab === tt.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
                display: "inline-flex", alignItems: "center", gap: 6,
              }}
            >
              <Icon name={tt.icon} size={13} />
              {tt.label}
              {tt.count != null && tt.count > 0 && <span className="count" style={{ marginLeft: 4 }}>{tt.count}</span>}
            </button>
          ))}
        </div>
        <div style={{ padding: 18 }}>
          {tab === "overview" && (
            <dl className="kv" style={{ gridTemplateColumns: "200px 1fr" }}>
              <dt>id</dt><dd className="mono">{t.id}</dd>
              <dt>description</dt><dd>{t.description || <span style={{ color: "var(--text-4)" }}>—</span>}</dd>
              <dt>provider</dt><dd className="mono"><a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => navigate(`/workspaces/providers/${encodeURIComponent(t.provider_id)}`)}>{t.provider_id}</a></dd>
              <dt>backend</dt><dd><window.WorkspaceBackendBadge kind={t.backend?.kind || "local"} /></dd>
              <dt>files</dt><dd className="mono">{(t.files || []).length}</dd>
              <dt>env keys</dt><dd className="mono">{Object.keys(t.env || {}).length}</dd>
              <dt>init_commands</dt><dd className="mono">{(t.init_commands || []).length}</dd>
              <dt>state_path</dt><dd className="mono">{t.state_path}</dd>
              <dt>tmp_path</dt><dd className="mono">{t.tmp_path}</dd>
            </dl>
          )}
          {tab === "recipe" && (
            <div>
              <div className="muted text-sm mb-2">Raw template recipe (server-redacted secrets where applicable).</div>
              <div className="code-block" style={{ maxHeight: 420, overflow: "auto", whiteSpace: "pre", fontFamily: "var(--font-mono, monospace)", fontSize: 12 }}>
                {JSON.stringify(t, null, 2)}
              </div>
            </div>
          )}
          {tab === "workspaces" && (
            referencingWorkspaces.length === 0 ? (
              <div className="empty" style={{ padding: 20 }}>
                <div className="head">No workspaces materialised</div>
                <div className="sub">No workspaces are using this template yet.</div>
              </div>
            ) : (
              <table className="tbl">
                <thead><tr><th>ID</th><th>Created</th></tr></thead>
                <tbody>
                  {referencingWorkspaces.map((w) => (
                    <tr key={w.id} onClick={() => navigate(`/workspaces/${encodeURIComponent(w.id)}`)} style={{ cursor: "pointer" }}>
                      <td className="mono">{w.id}</td>
                      <td className="muted text-sm">{w.created_at || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </div>

      {editOpen && (
        <WorkspaceTemplateCreateModal
          existing={t}
          onClose={() => setEditOpen(false)}
          pushToast={pushToast}
        />
      )}

      {showDelete && (
        <Modal
          title={`Delete ${templateId}?`}
          danger
          onClose={() => { setShowDelete(false); setDeleteError(null); }}
          footer={
            <>
              <Btn kind="ghost" onClick={() => { setShowDelete(false); setDeleteError(null); }}>Cancel</Btn>
              <Btn
                kind="danger"
                icon="trash"
                disabled={referencingWorkspaces.length > 0}
                busy={del.loading}
                onClick={() => del.mutate().catch(() => { /* onError */ })}
              >
                {del.loading ? "Deleting…" : "Delete template"}
              </Btn>
            </>
          }
        >
          {deleteError ? (
            <><strong style={{ color: "var(--red)" }}>409 Conflict</strong> — {deleteError}</>
          ) : referencingWorkspaces.length > 0 ? (
            <>
              <strong style={{ color: "var(--red)" }}>409 Conflict</strong> — this template is referenced by{" "}
              <strong>{referencingWorkspaces.length}</strong> workspace{referencingWorkspaces.length === 1 ? "" : "s"}.
              Delete those workspaces first.
            </>
          ) : (
            <>No workspaces reference this template. Deletion is safe.</>
          )}
        </Modal>
      )}
    </div>
  );
}

window.WorkspaceTemplatesPage = WorkspaceTemplatesPage;
window.WorkspaceTemplateCreateModal = WorkspaceTemplateCreateModal;
window.WorkspaceTemplateDetail = WorkspaceTemplateDetail;
