/* global React, SH_useShell */
// Voice, binding S4's composer work (spec section 8).
//
// The input half lives in Composer already (micEnabled / onTranscribed,
// S4 Task 24): hold-to-talk with a double-tap latch, an unmissable
// recording border, and dictation that ALWAYS lands as editable text.
// This file is the output half plus the one rule that is easy to get
// wrong: what may auto-play.
//
// Auto-play is FINAL ANSWERS ONLY, in the FOREGROUND session only. Tool
// narration never speaks, a background session never speaks, and there
// nothing here listens for a pause and commits on its own: an utterance
// can be an approval, so voice never resolves a gate. Dictation fills the
// composer and the human presses send.

function SH_voiceRepliesKey(sid) {
  return "primer.shell.voice:" + String(sid);
}

function SH_shouldAutoPlay(input) {
  var opts = input || {};
  var row = opts.row || {};
  if (!opts.enabled) return false;
  if (!opts.isForeground) return false;
  if (row.kind !== "assistant_message") return false;
  return !!row.final;
}

function SH_SpeakerButton(props) {
  var shell = SH_useShell();
  if (!shell.speech.tts_configured) return null;
  return (
    <button type="button" className="sh-verb"
      data-testid={"shell-speak:" + props.row.seq}
      onClick={function () {
        var audio = window.CT_speakTurn(
          String(props.row.text || props.row.label || ""), props.agentId
        );
        shell.voiceRef.current = audio;
      }}>Speak Turn</button>
  );
}

function SH_VoiceReplies(props) {
  var shell = SH_useShell();
  var sid = props.sid;
  var enabledState = React.useState(function () {
    try {
      return window.localStorage.getItem(SH_voiceRepliesKey(sid)) === "1";
    } catch (_e) { return false; }
  });
  var enabled = enabledState[0];
  var setEnabled = enabledState[1];
  var spokenRef = React.useRef({});

  React.useEffect(function () {
    if (!shell.speech.tts_configured) return;
    var rows = props.rows || [];
    var last = rows.length ? rows[rows.length - 1] : null;
    if (!last) return;
    if (spokenRef.current[last.seq]) return;
    if (!SH_shouldAutoPlay({
      row: last, enabled: enabled, isForeground: !!props.isForeground,
    })) return;
    spokenRef.current[last.seq] = true;
    shell.voiceRef.current = window.CT_speakTurn(
      String(last.text || last.label || ""), props.agentId
    );
  }, [props.rows && props.rows.length, enabled, props.isForeground]);

  if (!shell.speech.tts_configured) return null;

  return (
    <span className="sh-voice">
      <button type="button" className="sh-verb"
        data-testid="shell-voice-toggle" data-on={enabled}
        onClick={function () {
          var next = !enabled;
          setEnabled(next);
          try {
            window.localStorage.setItem(
              SH_voiceRepliesKey(sid), next ? "1" : "0"
            );
          } catch (_e) { /* best effort */ }
        }}>Toggle Voice Replies</button>
      {/* Persistent, not conditional: WCAG 1.4.2 wants the stop control
          reachable whenever audio can be playing at all. */}
      <button type="button" className="sh-verb"
        data-testid="shell-voice-stop"
        onClick={function () {
          var audio = shell.voiceRef.current;
          if (audio && typeof audio.pause === "function") audio.pause();
        }}>Stop Speaking</button>
    </span>
  );
}

window.SH_voiceRepliesKey = SH_voiceRepliesKey;
window.SH_shouldAutoPlay = SH_shouldAutoPlay;
window.SH_SpeakerButton = SH_SpeakerButton;
window.SH_VoiceReplies = SH_VoiceReplies;
