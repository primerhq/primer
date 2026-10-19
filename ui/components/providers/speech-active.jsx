/* global React, window */
// speech-active.jsx - the ASR and TTS active selectors shown above their
// provider cards (plan 02, Task 16; seam I-4). Each selector edits the one
// ActiveSpeechConfig row as a whole, so changing one default never drops the
// other two. Loaded after register-speech.jsx and before provider-catalog.jsx.

function PC_activeSpeechBody(row) {
  const source = row || {};
  return {
    stt_provider_id: source.stt_provider_id || null,
    tts_provider_id: source.tts_provider_id || null,
    tts_voice: source.tts_voice || null,
  };
}

function PC_activeSpeechRows(rows) {
  return (Array.isArray(rows) ? rows : []).filter((row) => row && row.id).map((row) => ({
    value: String(row.id),
    label: String(row.id),
  }));
}

function PC_useActiveSpeech(reload) {
  const api = window.primerApi;
  const active = api.useResource(
    "pc:speech-active",
    (signal) => api.apiFetch("GET", "/speech_active_config", null, { signal }),
    { pollMs: 0 },
  );
  const [local, setLocal] = React.useState(null);
  const [saving, setSaving] = React.useState(false);
  const servedRef = React.useRef(active.data);

  React.useEffect(() => {
    if (active.data === servedRef.current) return;
    servedRef.current = active.data;
    setLocal(null);
  }, [active.data]);

  const save = async (next) => {
    if (saving) return;
    const body = PC_activeSpeechBody(next);
    setLocal(body);
    setSaving(true);
    try {
      const answer = await api.apiFetch("PUT", "/speech_active_config", body);
      setLocal(PC_activeSpeechBody(answer || body));
      active.refetch();
      if (typeof reload === "function") reload();
      if (typeof api.toastPush === "function") {
        api.toastPush({ kind: "success", title: "Speech default saved" });
      }
    } catch (err) {
      setLocal(null);
      if (typeof api.toastPush === "function") {
        api.toastPush({
          kind: "error",
          title: "Speech default not saved",
          detail: api.readRefusal(err, String(err)).message,
        });
      }
    } finally {
      setSaving(false);
    }
  };

  return {
    active,
    row: local || (active.data ? PC_activeSpeechBody(active.data) : null),
    saving,
    save,
  };
}

function PC_ActiveSpeechLoad({ state }) {
  if (state.row) return null;
  if (state.active.error) {
    const words = window.primerApi.readRefusal(state.active.error, String(state.active.error)).message;
    return (
      <div className="nv-form-error" role="alert" data-testid="pc-active-error">
        <span>{words}</span>{" "}
        <button type="button" className="btn ghost" data-testid="pc-active-retry" onClick={() => state.active.refetch()}>Retry</button>
      </div>
    );
  }
  return <div className="muted" role="status" data-testid="pc-active-loading">Loading speech defaults…</div>;
}

function PC_SttActiveSelector({ rows, reload }) {
  const state = PC_useActiveSpeech(reload);
  if (!state.row) return <PC_ActiveSpeechLoad state={state} />;
  const options = PC_activeSpeechRows(rows);
  const value = state.row.stt_provider_id || "";
  return (
    <window.PK_Section title="DEFAULT TRANSCRIPTION" testid="pc-active-stt" cols={1}>
      <window.PK_Field label="Transcribe with">
        <select data-testid="pc-active-stt-provider" aria-label="Transcribe with" value={value} disabled={state.saving}
          onChange={(event) => state.save({ ...state.row, stt_provider_id: event.target.value || null })}>
          <option value="">(none)</option>
          {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </window.PK_Field>
    </window.PK_Section>
  );
}

function PC_TtsActiveSelector({ rows, reload }) {
  const api = window.primerApi;
  const state = PC_useActiveSpeech(reload);
  const providerId = state.row ? state.row.tts_provider_id : null;
  const voices = api.useResource(
    providerId ? "pc:speech-voices:" + providerId : null,
    (signal) => api.apiFetch("GET", "/audio/voices?provider_id=" + encodeURIComponent(providerId), null, { signal }),
    { pollMs: 0 },
  );
  if (!state.row) return <PC_ActiveSpeechLoad state={state} />;
  const options = PC_activeSpeechRows(rows);
  const voiceNames = voices.data && Array.isArray(voices.data.voices) ? voices.data.voices : [];
  return (
    <window.PK_Section title="DEFAULT VOICE OUTPUT" testid="pc-active-tts" cols={2}>
      <window.PK_Field label="Speak with">
        <select data-testid="pc-active-tts-provider" aria-label="Speak with" value={state.row.tts_provider_id || ""} disabled={state.saving}
          onChange={(event) => state.save({ ...state.row, tts_provider_id: event.target.value || null, tts_voice: null })}>
          <option value="">(none)</option>
          {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </window.PK_Field>
      <window.PK_Field label="Voice" help={providerId && voices.error ? "Could not list this provider's voices." : null}>
        <select data-testid="pc-active-tts-voice" aria-label="Voice" value={state.row.tts_voice || ""}
          disabled={state.saving || !providerId || voices.loading}
          onChange={(event) => state.save({ ...state.row, tts_voice: event.target.value || null })}>
          <option value="">(provider default voice)</option>
          {voiceNames.map((voice) => <option key={voice} value={voice}>{voice}</option>)}
        </select>
      </window.PK_Field>
    </window.PK_Section>
  );
}

window.PC_TYPE_EXTRAS = Object.assign(window.PC_TYPE_EXTRAS || {}, {
  tts: PC_TtsActiveSelector,
  stt: PC_SttActiveSelector,
});
