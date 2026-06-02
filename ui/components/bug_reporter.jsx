/* global React, Icon, Btn, Modal, html2canvas */
// Floating bug-report button + screenshot capture + submit modal.
// POSTs {description, screenshot_b64, page_url, viewport, captured_at}
// to /v1/bugs (the backend writes one folder per report). Write-only:
// there is no GET surface — the operator reads bugs/ on disk.

function BG_BugButton({ pushToast }) {
  const { apiFetch } = window.primerApi;
  const [modalOpen, setModalOpen] = React.useState(false);
  const [screenshot, setScreenshot] = React.useState(null); // data URL
  const [capturing, setCapturing] = React.useState(false);
  const [description, setDescription] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState(null);

  const handleClick = async () => {
    if (typeof window.html2canvas !== "function") {
      pushToast?.({
        kind: "warning",
        title: "Screenshot library not loaded",
        detail: "Submit without an image, or reload.",
      });
      setScreenshot(null);
      setModalOpen(true);
      return;
    }
    setCapturing(true);
    setError(null);
    try {
      const canvas = await window.html2canvas(document.body, {
        useCORS: true,
        logging: false,
        scale: window.devicePixelRatio || 1,
      });
      setScreenshot(canvas.toDataURL("image/png"));
    } catch (err) {
      // html2canvas failures are usually CORS/CSP related; surface a
      // toast but still open the modal so the operator can submit a
      // text-only report.
      // eslint-disable-next-line no-console
      console.error("html2canvas failed", err);
      pushToast?.({
        kind: "warning",
        title: "Screenshot failed",
        detail: "Continuing with description only.",
      });
      setScreenshot(null);
    } finally {
      setCapturing(false);
      setModalOpen(true);
    }
  };

  const submit = async () => {
    if (!description.trim()) {
      setError("Description is required.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await apiFetch("POST", "/bugs", {
        description: description.trim(),
        screenshot_b64: screenshot, // may be null
        page_url: window.location.href,
        viewport: {
          width: window.innerWidth,
          height: window.innerHeight,
        },
        captured_at: new Date().toISOString(),
      });
      pushToast?.({
        kind: "success",
        title: "Bug reported",
        detail: "Saved to disk.",
      });
      setModalOpen(false);
      setDescription("");
      setScreenshot(null);
    } catch (err) {
      setError(err?.detail || err?.message || "Submit failed");
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = () => {
    setModalOpen(false);
    setDescription("");
    setScreenshot(null);
    setError(null);
  };

  return (
    <>
      <button
        data-testid="bug-report-btn"
        onClick={handleClick}
        disabled={capturing}
        title="Report a bug"
        aria-label="Report a bug"
        style={{
          position: "fixed",
          bottom: 20,
          left: 20,
          zIndex: 9999,
          width: 44,
          height: 44,
          borderRadius: "50%",
          border: "1px solid var(--border)",
          background: "var(--surface-2, var(--surface))",
          color: "var(--red)",
          cursor: capturing ? "wait" : "pointer",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          boxShadow: "0 2px 8px rgba(0,0,0,0.35)",
          padding: 0,
        }}
      >
        <Icon name="alert" size={18} />
      </button>
      {modalOpen && (
        <Modal onClose={cancel} title="Report a bug">
          <div
            data-testid="bug-report-modal"
            style={{ display: "flex", flexDirection: "column", gap: 12 }}
          >
            {screenshot ? (
              <img
                src={screenshot}
                alt="Page screenshot"
                style={{
                  maxWidth: "100%",
                  maxHeight: 240,
                  border: "1px solid var(--border)",
                  borderRadius: 4,
                  objectFit: "contain",
                }}
                data-testid="bug-screenshot-preview"
              />
            ) : (
              <div className="muted" style={{ fontSize: 12 }}>
                No screenshot captured.
              </div>
            )}
            <textarea
              data-testid="bug-description"
              className="textarea"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Describe what's wrong…"
              rows={6}
              autoFocus
              style={{ resize: "vertical" }}
            />
            {error && (
              <div style={{ color: "var(--red)", fontSize: 12 }}>
                {error}
              </div>
            )}
            <div
              style={{
                display: "flex",
                gap: 8,
                justifyContent: "flex-end",
              }}
            >
              <Btn kind="ghost" onClick={cancel} disabled={submitting}>
                Cancel
              </Btn>
              <Btn
                kind="primary"
                onClick={submit}
                disabled={submitting || !description.trim()}
                data-testid="bug-submit-btn"
              >
                {submitting ? "Sending…" : "Submit"}
              </Btn>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}

window.BG_BugButton = BG_BugButton;
