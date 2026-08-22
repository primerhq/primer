// primer UI - microphone capture maths.
//
// Pure functions only: no WebAudio, no React, no DOM. That is what lets
// tests/ui/test_audio_resample.py run this file in MiniRacer and assert
// on real numbers rather than on a mock.
//
// The rule that matters: DOWNMIX BEFORE RESAMPLE. Browsers capture at
// 44.1 or 48 kHz and speech services commonly hard-reject anything that
// is not 16 kHz mono, so both conversions have to happen; doing them in
// the other order resamples every channel and then throws half the work
// away.

var PRIMER_TARGET_RATE = 16000;
// RMS below this over a window counts as silence for segmentation.
var PRIMER_SILENCE_RMS = 0.01;

function PRIMER_downmixToMono(channels) {
  if (!channels || channels.length === 0) return new Float32Array(0);
  if (channels.length === 1) return channels[0];
  var length = channels[0].length;
  var out = new Float32Array(length);
  for (var i = 0; i < length; i++) {
    var sum = 0;
    for (var c = 0; c < channels.length; c++) sum += channels[c][i];
    out[i] = sum / channels.length;
  }
  return out;
}

function PRIMER_resampleLinear(samples, fromRate, toRate) {
  if (fromRate === toRate) return samples;
  var ratio = fromRate / toRate;
  var length = Math.floor(samples.length / ratio);
  var out = new Float32Array(length);
  for (var i = 0; i < length; i++) {
    var position = i * ratio;
    var lower = Math.floor(position);
    var upper = Math.min(lower + 1, samples.length - 1);
    var weight = position - lower;
    out[i] = samples[lower] * (1 - weight) + samples[upper] * weight;
  }
  return out;
}

function PRIMER_encodeWavPcm16(samples, sampleRate) {
  var dataBytes = samples.length * 2;
  var buffer = new ArrayBuffer(44 + dataBytes);
  var view = new DataView(buffer);

  function ascii(offset, text) {
    for (var i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  }

  ascii(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true);           // PCM fmt chunk size
  view.setUint16(20, 1, true);            // format: PCM
  view.setUint16(22, 1, true);            // channels: mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true);            // block align
  view.setUint16(34, 16, true);           // bits per sample
  ascii(36, "data");
  view.setUint32(40, dataBytes, true);

  for (var i = 0; i < samples.length; i++) {
    // Clamp rather than let the cast wrap: a wrapped peak is a loud
    // click, which reads to ASR as a consonant that was never spoken.
    var value = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  return new Uint8Array(buffer);
}

function PRIMER_toMono16kWav(channels, sourceRate) {
  var mono = PRIMER_downmixToMono(channels);
  var resampled = PRIMER_resampleLinear(mono, sourceRate, PRIMER_TARGET_RATE);
  return PRIMER_encodeWavPcm16(resampled, PRIMER_TARGET_RATE);
}

function PRIMER_findSilenceSplit(samples, sampleRate, minSeconds) {
  // Segment long recordings on silence instead of uploading one large
  // file: a failed forty-minute upload wastes the whole transfer and
  // shows no progress on the way.
  var minSamples = Math.floor(minSeconds * sampleRate);
  if (samples.length <= minSamples) return -1;
  var window = Math.floor(sampleRate * 0.3);
  for (var start = minSamples; start + window <= samples.length; start += window) {
    var sum = 0;
    for (var i = start; i < start + window; i++) sum += samples[i] * samples[i];
    if (Math.sqrt(sum / window) < PRIMER_SILENCE_RMS) return start;
  }
  return -1;
}

if (typeof window !== "undefined") {
  window.PRIMER_TARGET_RATE = PRIMER_TARGET_RATE;
  window.PRIMER_SILENCE_RMS = PRIMER_SILENCE_RMS;
  window.PRIMER_downmixToMono = PRIMER_downmixToMono;
  window.PRIMER_resampleLinear = PRIMER_resampleLinear;
  window.PRIMER_encodeWavPcm16 = PRIMER_encodeWavPcm16;
  window.PRIMER_toMono16kWav = PRIMER_toMono16kWav;
  window.PRIMER_findSilenceSplit = PRIMER_findSilenceSplit;
}
