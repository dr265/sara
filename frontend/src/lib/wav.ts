/**
 * WAV encoding for browser microphone capture.
 *
 * Why this exists instead of `MediaRecorder`: the backend's transcriber
 * (`core/voice.py::transcribe`) decodes with Python's `wave` module, which
 * understands RIFF/WAVE and nothing else. `MediaRecorder` produces WebM/Opus on
 * every current browser, which `wave` cannot open. So the browser captures raw
 * PCM through an `AudioContext` and this module writes a real WAV container.
 *
 * Whisper wants 16 kHz mono 16-bit; that is exactly what this produces, so the
 * upload is also smaller than anything else we could send.
 *
 * No dependency: the container is a 44-byte header followed by the samples,
 * and getting that wrong would silently produce a file the backend refuses.
 */

/** Whisper's native rate. Anything else is resampled to this. */
export const TARGET_SAMPLE_RATE = 16_000;

/**
 * Resample by linear interpolation.
 *
 * Not a windowed-sinc filter, which is what a purist would use - but speech
 * recognition at 16 kHz with Whisper is robust to the difference, and this runs
 * on the main thread on every capture. Linear interpolation with a fractional
 * read index has no discontinuity artefacts of its own; it is a mild low-pass,
 * not a correcting one.
 */
export function resample(
  input: Float32Array,
  inputRate: number,
  outputRate: number = TARGET_SAMPLE_RATE,
): Float32Array {
  if (inputRate === outputRate || input.length === 0) return input;
  const ratio = inputRate / outputRate;
  const length = Math.round(input.length / ratio);
  const output = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const position = i * ratio;
    const index = Math.floor(position);
    const next = Math.min(index + 1, input.length - 1);
    const weight = position - index;
    output[i] = input[index] * (1 - weight) + input[next] * weight;
  }
  return output;
}

/** Downmix any channel count to one by averaging. */
export function toMono(channels: Float32Array[]): Float32Array {
  if (channels.length === 1) return channels[0];
  const frames = channels[0]?.length ?? 0;
  const output = new Float32Array(frames);
  for (const channel of channels) {
    for (let i = 0; i < frames; i += 1) output[i] += (channel[i] ?? 0) / channels.length;
  }
  return output;
}

/**
 * Encode mono float samples as a 16-bit PCM WAV file.
 *
 * Clamps rather than wraps: a sample outside [-1, 1] that wrapped would turn
 * one loud peak into a burst of noise, and a clamp is at worst audibly quieter.
 */
export function encodeWav(
  samples: Float32Array,
  sampleRate: number = TARGET_SAMPLE_RATE,
): ArrayBuffer {
  const bytesPerSample = 2;
  const dataSize = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  const writeAscii = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
  };

  writeAscii(0, 'RIFF');
  view.setUint32(4, 36 + dataSize, true); // chunk size
  writeAscii(8, 'WAVE');

  writeAscii(12, 'fmt ');
  view.setUint32(16, 16, true); // PCM fmt subchunk size
  view.setUint16(20, 1, true); // format 1 = uncompressed PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * bytesPerSample, true); // byte rate
  view.setUint16(32, bytesPerSample, true); // block align
  view.setUint16(34, 16, true); // bits per sample

  writeAscii(36, 'data');
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += bytesPerSample;
  }
  return buffer;
}

/** Convenience: mono float samples in, 16 kHz mono 16-bit WAV out. */
export function toWhisperWav(
  samples: Float32Array,
  sampleRate: number,
): ArrayBuffer {
  return encodeWav(resample(samples, sampleRate, TARGET_SAMPLE_RATE), TARGET_SAMPLE_RATE);
}

/** Root-mean-square amplitude of a buffer - the loudness measure for VAD. */
export function rms(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}
