/* global React */
// useSessionControls — shared per-session signal controls (FD1b).
//
// The pause/resume/steer/cancel mutations used to be inlined twice: once in the
// (now-removed) full-page SessionDetail component, and again in the Studio's
// ST_SessionControls cluster (studio-center.jsx). Both hit the SAME
// workspace-scoped endpoints (POST /workspaces/{wid}/sessions/{sid}/{action}).
// This extracts them into ONE useMutation-backed hook so every caller stays in
// sync.
//
//   var controls = useSessionControls(wid, sid, {
//     pushToast,                        // toast enqueuer (optional)
//     invalidates: ["studio-session:"+sid],  // extra cache keys to refetch
//     onSteerSuccess: () => { ... },    // e.g. close/clear the steer popover
//   });
//   controls.pause / .resume / .steer / .cancel   // each a useMutation result
//                                                 // ({ mutate, loading, ... })
//
// No-build rules: top-level `function`/`var`; every hook call is unconditional
// and in a fixed order (Rules of Hooks); exported as window.useSessionControls.

function useSessionControls(wid, sid, opts) {
  opts = opts || {};
  var pushToast = opts.pushToast;
  var extraInvalidates = Array.isArray(opts.invalidates) ? opts.invalidates : [];
  var onSteerSuccess = opts.onSteerSuccess;

  var api = window.primerApi || {};
  var useMutation = api.useMutation;
  var apiFetch = api.apiFetch;

  function toastErr(title) {
    return function (err) {
      if (typeof pushToast !== "function") return;
      pushToast({
        kind: "error",
        title: (err && err.title) || title,
        detail: (err && err.detail) || (err && err.message),
        requestId: err && err.requestId,
      });
    };
  }

  // session-detail + sessions:list keep the detail row and the list fresh;
  // callers can append their own view-scoped keys (e.g. studio-session:{sid}).
  var invalidates = ["session-detail:" + sid, "sessions:list"].concat(extraInvalidates);

  function signal(action) {
    return apiFetch(
      "POST",
      "/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid) + "/" + action
    );
  }

  var pause = useMutation(
    function () { return signal("pause"); },
    {
      invalidates: invalidates,
      onSuccess: function () { pushToast && pushToast({ kind: "success", title: "Session paused" }); },
      onError: toastErr("Pause failed"),
    }
  );
  var resume = useMutation(
    function () { return signal("resume"); },
    {
      invalidates: invalidates,
      onSuccess: function () { pushToast && pushToast({ kind: "success", title: "Resume signal sent" }); },
      onError: toastErr("Resume failed"),
    }
  );
  var cancel = useMutation(
    function () { return signal("cancel"); },
    {
      invalidates: invalidates,
      onSuccess: function () { pushToast && pushToast({ kind: "warning", title: "Cancel signal sent" }); },
      onError: toastErr("Cancel failed"),
    }
  );
  var steer = useMutation(
    function (instruction) {
      return apiFetch(
        "POST",
        "/workspaces/" + encodeURIComponent(wid) + "/sessions/" + encodeURIComponent(sid) + "/steer",
        { instruction: instruction }
      );
    },
    {
      invalidates: invalidates,
      onSuccess: function () {
        pushToast && pushToast({ kind: "success", title: "Steer queued" });
        if (typeof onSteerSuccess === "function") onSteerSuccess();
      },
      onError: toastErr("Steer failed"),
    }
  );

  return { pause: pause, resume: resume, steer: steer, cancel: cancel };
}

window.useSessionControls = useSessionControls;
