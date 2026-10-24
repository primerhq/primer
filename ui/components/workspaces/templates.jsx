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
    form.command = (b.entrypoint || []).join("\n");
    form.args = (b.args || []).join("\n");
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
  form.init = (t.init_commands || []).join("\n");
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
  WT_formKeys(form.kind || "local").forEach(function (key) {
    if (initial && JSON.stringify(form[key]) === JSON.stringify(initial[key])) return;
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
// stores a template no workspace can be made from.
function WT_validateForm(form, opts) {
  var isAdmin = !!(opts && opts.isAdmin);
  var errs = {};
  var kind = form.kind;
  function size(key, max, tooMuch, bytes) {
    if (!WT_trim(form[key])) return;
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
    if (WT_trim(form.gpuCount)) {
      var gpus = /^[0-9]+$/.test(WT_trim(form.gpuCount)) ? Number(WT_trim(form.gpuCount)) : 0;
      if (gpus < 1) errs.gpuCount = "Enter a whole number of at least 1";
      else if (gpus > WT_MAX_GPUS) errs.gpuCount = "At most 64 GPUs";
      if (!isAdmin && WT_trim(form.gpuResource) && WT_trim(form.gpuResource) !== WT_DEFAULT_GPU_RESOURCE) {
        errs.gpuResource = "Only an admin may name another GPU resource";
      }
    }
    if (WT_jsonList(form.extraVolumesJson) === null) errs.extraVolumesJson = "Enter a JSON list";
    if (WT_jsonList(form.extraVolumeMountsJson) === null) errs.extraVolumeMountsJson = "Enter a JSON list";
    if (WT_trim(form.podGrace) && !/^[0-9]+$/.test(WT_trim(form.podGrace))) errs.podGrace = "Enter a whole number of seconds";
    if (WT_twice(form.nodeSelectorRows)) errs.nodeSelectorRows = "Each name once";
  }
  if ((form.files || []).some(function (f) { return f && WT_trim(f.mode) && !/^[0-7]{3,4}$/.test(WT_trim(f.mode)); })) {
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
function WT_splitErrors(err, keyForLoc, opts) {
  var fields = {};
  var rest = [];
  var entries = err && Array.isArray(err.fieldErrors) ? err.fieldErrors : [];
  entries.forEach(function (fe) {
    var msg = fe && typeof fe.msg === "string" ? fe.msg : "";
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

// Every template, a page of 200 at a time in id order, until a short page says the list has ended (or WT_MAX_PAGES pages were read).
function WT_fetchAllTemplates(apiFetch, signal) {
  var items = [];
  function page(n) {
    var path = "/workspace_templates?limit=200&offset=" + n * 200 + "&order_by=id:asc";
    return apiFetch("GET", path, null, { signal: signal }).then(function (answer) {
      var rows = (answer && answer.items) || [];
      items = items.concat(rows);
      if (rows.length < 200 || n + 1 >= WT_MAX_PAGES) return { items: items, total: items.length };
      return page(n + 1);
    });
  }
  return page(0);
}

Object.assign(window, {
  WT_LIST_KEY, WT_TEMPLATE_KEYS, WT_RESERVED_IDS, WT_isReserved, WT_KIND_LABELS, WT_KIND_ICONS, WT_K8S_ADMIN_FIELDS, WT_DEFAULT_GPU_RESOURCE,
  WT_K8S_DEFAULT_COMMAND, WT_GIB, WT_MAX_PAGES, WT_quantityToGiB, WT_formatGiB, WT_giBToBytes, WT_diskQuantity, WT_templateSize, WT_sizeText,
  WT_summaryLine, WT_cardFacts, WT_heldAdminFields, WT_emptyForm, WT_formForChoice, WT_formFromTemplate, WT_templateBody, WT_validateForm,
  WT_FORM_PATHS, WT_fieldKeyForLoc, WT_splitErrors, WT_fetchAllTemplates,
});
// ---- end WT pure helpers ----

// Security review 2026-10-08: host mounts, Kubernetes overlays and secret file sources reach the host, the cluster or
// operator secrets, so only an admin may set or change them; the server answers a non-admin with 403 forbidden_role.
const WT_ADMIN_ONLY_NOTE =
  "Admin only: only an admin can set or change these fields, and only an admin can edit or delete a template that has any of them set. Users can still create workspaces from such a template and run their own commands next to its mounts, secrets and service account, so do not put host-privileged mounts, secrets or a service account with real permissions in a template ordinary users may use. Kubernetes overlays must also stay on the server's allowlist.";

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
