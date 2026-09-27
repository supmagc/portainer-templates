"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");

// Two-tier cq gate on source bitrate vs. the resolution bucket's threshold:
//   <  skipThreshold (legacy/already-compact) -> cq30, -b:v 0 (pure CQ mode)
//   >= skipThreshold (healthy/normal source)   -> cq24, -b:v targetBitrate
// NVENC's cq isn't equivalent to x265's crf: at a low cq a legacy low-bitrate
// source rides maxrate and comes out bigger than the source. cq30 + -b:v 0
// (NVENC's actual "Constant Quality" mode - target bitrate must be 0) fixes
// that. The healthy tier instead sets a real -b:v target so NVENC steers
// toward an average instead of only having a ceiling (maxrate) and a floor
// (cq) with nothing in between - that gap is what let very-high-bitrate
// sources just pin at maxrate regardless of cq. targetBitrate is 60% of the
// bucket's maxrate.
//
// A third >=2x-skipThreshold tier (cq26 + highMaxrate/highBufsize, 75% of
// the bucket's normal maxrate/bufsize) exists below but is DISABLED for now
// via HIGH_TIER_ENABLED - on the one sample tested, cq26 alone (same maxrate)
// made ~no size difference, so it's parked pending re-test now that
// targetBitrate covers the same problem from the healthy tier. Flip the flag
// to bring it back.
//
// skipThreshold = maxrate: above it, cq alone can't out-grow the source; lowering it lets legacy sources bloat.
//
// Wire this after "Set Video Encoder (Custom)" output 1 (GPU), on the
// hardware-available branch. Assumes content >1440p was already downscaled
// to 1440p upstream (keep the stock "Set Video Resolution 1440p" node).

var HIGH_TIER_ENABLED = false;

var BUCKETS = [
    { maxHeight: 576, maxrate: 1800, bufsize: 2400, skipThreshold: 1800, targetBitrate: 1200, highMaxrate: 1350, highBufsize: 1800 },
    { maxHeight: 720, maxrate: 2700, bufsize: 3600, skipThreshold: 2700, targetBitrate: 1700, highMaxrate: 2025, highBufsize: 2700 },
    { maxHeight: 1080, maxrate: 4200, bufsize: 5600, skipThreshold: 4200, targetBitrate: 2600, highMaxrate: 3150, highBufsize: 4200 },
    { maxHeight: Infinity, maxrate: 7500, bufsize: 10000, skipThreshold: 7500, targetBitrate: 4500, highMaxrate: 5625, highBufsize: 7500 }, // 1440p
];

var details = function () { return ({
    name: 'Set Encoding (GPU, Custom)',
    description: 'Sets NVENC cq24/cq30 (source-bitrate gated) + target/maxrate/bufsize matched to the output resolution bucket, preset p7.',
    style: {
        borderColor: '#6efefc',
    },
    tags: 'video',
    isStartPlugin: false,
    pType: '',
    requiresVersion: '2.11.01',
    sidebarPosition: -1,
    icon: '',
    inputs: [],
    outputs: [
        {
            number: 1,
            tooltip: 'Continue to next plugin',
        },
    ],
}); };
exports.details = details;

var plugin = function (args) {
    var lib = require('../../../../../methods/lib')();
    args.inputs = lib.loadDefaultValues(args.inputs, details);
    (0, flowUtils_1.checkFfmpegCommandInit)(args);

    var streams = args.inputFileObj.ffProbeData.streams;
    var vStream = streams.find(function (s) { return s.codec_type === 'video'; });
    var height = Number(vStream.height) || 0;
    var bucket = BUCKETS.find(function (b) { return height <= b.maxHeight; });

    // Prefer the video stream's own bit_rate; not all containers populate it
    // per-stream (e.g. some MKVs), so fall back to the overall format bitrate.
    var rawBitrate = vStream.bit_rate || args.inputFileObj.ffProbeData.format.bit_rate || 0;
    var sourceBitrateKbps = Math.round(Number(rawBitrate) / 1000);

    var cq = 24;
    var maxrate = bucket.maxrate;
    var bufsize = bucket.bufsize;
    var targetBitrate = bucket.targetBitrate;
    if (sourceBitrateKbps > 0 && sourceBitrateKbps < bucket.skipThreshold) {
        cq = 30;
        targetBitrate = 0; // pure CQ mode - a forced average here is what caused the original NVENC bloat-on-legacy-sources bug
    }
    else if (HIGH_TIER_ENABLED && sourceBitrateKbps >= bucket.skipThreshold * 2) {
        cq = 26;
        maxrate = bucket.highMaxrate;
        bufsize = bucket.highBufsize;
    }

    args.variables.ffmpegCommand.overallOuputArguments.push(
        '-rc', 'vbr',
        '-cq', String(cq),
        '-b:v', "".concat(targetBitrate, "k"),
        '-maxrate', "".concat(maxrate, "k"),
        '-bufsize', "".concat(bufsize, "k"),
        '-preset', 'p7',
        '-multipass', 'fullres',
    );

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: 1,
        variables: args.variables,
    };
};
exports.plugin = plugin;
