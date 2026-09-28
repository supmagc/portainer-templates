"use strict";
// Tuning values for the custom Tdarr nodes, in one place. Not a flow plugin - loads fine as a
// loose file next to the plugin folders. Mechanism and rationale stay in each node's header;
// this file only holds the numbers and lists you'd retune.

// ---- Video: which files get encoded, and how hard -------------------------------------------
// Used by Check Should Encode, Set Encoding (GPU, Custom) and Set Encoding (CPU, Custom).
// Calibration sweeps and visual checks: headers of Set Encoding (GPU, Custom) and (CPU, Custom).

// Taller sources are re-encoded; the stock "Set Video Resolution 1440p" node does the downscale.
var RESOLUTION_CAP = 1440;

// Picked by output height. healthy/degraded = cq for gpu (NVENC), crf for cpu (x265); degraded
// applies when the source's bits per pixel per frame is below bppThreshold (old WMVs etc.).
// maxrate/bufsize in kb/s - a safety valve, not a target.
// cpu maxrate/bufsize reuse the NVENC-derived caps. 576p was not in either sweep - estimates.
var BUCKETS = [
    {
        maxHeight: 576,
        bppThreshold: 0.1271,
        gpu: { healthy: 30, degraded: 34, maxrate: 2500, bufsize: 5000 },
        cpu: { healthy: 22, degraded: 26, maxrate: 2500, bufsize: 5000 },
    },
    {
        maxHeight: 720,
        bppThreshold: 0.1220,
        gpu: { healthy: 32, degraded: 34, maxrate: 3600, bufsize: 7200 },
        cpu: { healthy: 22, degraded: 26, maxrate: 3600, bufsize: 7200 },
    },
    {
        maxHeight: 1080,
        bppThreshold: 0.0844,
        gpu: { healthy: 32, degraded: 36, maxrate: 7500, bufsize: 15000 },
        cpu: { healthy: 24, degraded: 28, maxrate: 7500, bufsize: 15000 },
    },
    {
        maxHeight: Infinity, // 1440p
        bppThreshold: 0.0848,
        gpu: { healthy: 34, degraded: 38, maxrate: 10000, bufsize: 20000 },
        cpu: { healthy: 24, degraded: 28, maxrate: 10000, bufsize: 20000 },
    },
];

// maxrate is also capped at this fraction of the source bitrate, so a small source can't grow...
var SOURCE_MAXRATE_RATIO = 0.7;
// ...but never below this fraction of the bucket's own maxrate.
var SOURCE_MAXRATE_FLOOR = 0.3;

// An already-HEVC file is only re-encoded when its bitrate exceeds the bucket's maxrate (higher
// of gpu/cpu) times this. The margin covers audio/container overhead when only the overall
// bitrate is known; keep it >= 1 or the flow can flag its own output.
var REENCODE_BITRATE_TOLERANCE = 1.15;

// ---- Frame rate and cadence (Set Video Cadence) ----------------------------------------------

// Output rates above this are halved until they fit (59.94 -> 29.97, 50 -> 25).
var MAX_OUTPUT_FPS = 30;
// Source rates outside this range are treated as bogus (e.g. ASF's 1000/1) and left alone.
var MIN_SANE_FPS = 10;
var MAX_SANE_FPS = 120;

// Sources above this rate, or with one of these codecs, get an idet interlace/telecine probe.
var PROBE_FPS_THRESHOLD = 30;
var PROBE_CODECS = ['wmv3', 'vc1', 'msmpeg4', 'msmpeg4v2', 'msmpeg4v3', 'mpeg4', 'h263', 'mpeg1video', 'mpeg2video'];
// Seconds decoded for the probe, starting 1/3 into the file.
var PROBE_DURATION_SECONDS = 10;
var PROBE_TIMEOUT_MS = 60000;
// Share of TFF+BFF frames that counts as interlaced.
var INTERLACE_RATIO_THRESHOLD = 0.5;
// Share of repeated fields that counts as telecined. Unvalidated - check the job log first.
var TELECINE_RATIO_THRESHOLD = 0.3;

// ---- Audio (Set Audio Handling) --------------------------------------------------------------

// Copied as-is.
var AUDIO_COPY_CODECS = ['aac', 'ac3', 'eac3'];
// With more than 2 channels these become eac3 at AUDIO_MULTICHANNEL_KBPS. Any pcm_* also counts.
var AUDIO_HIGH_QUALITY_CODECS = ['dts', 'truehd', 'mlp', 'flac'];
var AUDIO_MULTICHANNEL_KBPS = 640;
// Everything else becomes aac at the source bitrate, clamped to this range
// (AUDIO_AAC_MAX_KBPS when the source bitrate is unknown).
var AUDIO_AAC_MIN_KBPS = 64;
var AUDIO_AAC_MAX_KBPS = 192;

// ---- Frame rate helpers (shared so every node agrees on the output rate) -------------------

// {num, den, value} for a sane "num/den" rate, else null (e.g. "0/0", or ASF's 1000/1 timebase).
function parseRate(raw) {
    var parts = String(raw || '').split('/');
    var num = Number(parts[0]);
    var den = parts.length > 1 ? Number(parts[1]) : 1;
    if (!(num > 0) || !(den > 0)) {
        return null;
    }
    var value = num / den;
    return value >= MIN_SANE_FPS && value <= MAX_SANE_FPS ? { num: num, den: den, value: value } : null;
}

// The video stream's nominal rate: r_frame_rate, falling back to avg_frame_rate. null if neither is sane.
function sourceRate(vStream) {
    return parseRate(vStream.r_frame_rate) || parseRate(vStream.avg_frame_rate);
}

// Halves the rate until it's at most MAX_OUTPUT_FPS (exact 2:1 steps, so no judder).
function capRate(rate) {
    while (rate.value > MAX_OUTPUT_FPS) {
        rate = { num: rate.num, den: rate.den * 2, value: rate.value / 2 };
    }
    return rate;
}

// Frames per second the encoder actually codes (capped source rate, 24 if unknown). The
// bits-per-pixel gate uses this: with the raw source rate, a 4K 59.94 web release looked
// "degraded" because half its frames are dropped before encoding.
function encodedFps(vStream) {
    var rate = sourceRate(vStream);
    return rate ? capRate(rate).value : 24;
}

module.exports = {
    RESOLUTION_CAP: RESOLUTION_CAP,
    BUCKETS: BUCKETS,
    SOURCE_MAXRATE_RATIO: SOURCE_MAXRATE_RATIO,
    SOURCE_MAXRATE_FLOOR: SOURCE_MAXRATE_FLOOR,
    REENCODE_BITRATE_TOLERANCE: REENCODE_BITRATE_TOLERANCE,
    MAX_OUTPUT_FPS: MAX_OUTPUT_FPS,
    MIN_SANE_FPS: MIN_SANE_FPS,
    MAX_SANE_FPS: MAX_SANE_FPS,
    PROBE_FPS_THRESHOLD: PROBE_FPS_THRESHOLD,
    PROBE_CODECS: PROBE_CODECS,
    PROBE_DURATION_SECONDS: PROBE_DURATION_SECONDS,
    PROBE_TIMEOUT_MS: PROBE_TIMEOUT_MS,
    INTERLACE_RATIO_THRESHOLD: INTERLACE_RATIO_THRESHOLD,
    TELECINE_RATIO_THRESHOLD: TELECINE_RATIO_THRESHOLD,
    AUDIO_COPY_CODECS: AUDIO_COPY_CODECS,
    AUDIO_HIGH_QUALITY_CODECS: AUDIO_HIGH_QUALITY_CODECS,
    AUDIO_MULTICHANNEL_KBPS: AUDIO_MULTICHANNEL_KBPS,
    AUDIO_AAC_MIN_KBPS: AUDIO_AAC_MIN_KBPS,
    AUDIO_AAC_MAX_KBPS: AUDIO_AAC_MAX_KBPS,
    sourceRate: sourceRate,
    capRate: capRate,
    encodedFps: encodedFps,
};
