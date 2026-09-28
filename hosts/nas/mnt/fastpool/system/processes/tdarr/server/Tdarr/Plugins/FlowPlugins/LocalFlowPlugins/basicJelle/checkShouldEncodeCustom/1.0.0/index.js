"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;

// Decides whether a file is worth encoding at all, so an already-good file
// (right codec, right resolution, right bitrate for its resolution) doesn't
// get pushed through a lossy re-encode for no gain. Complements Check Tags
// (Custom): that one only catches files THIS flow already tagged; this one
// also catches files that were already fine to begin with.
//
// Encode is needed if ANY of:
//   - not already HEVC (codec upgrade is the point either way)
//   - height > 1440 (resolution cap)
//   - already HEVC at <=1440p, but bitrate is above the higher of the GPU/CPU maxrate
//     for this resolution times REENCODE_BITRATE_TOLERANCE. Our own output can't
//     exceed its encoder's cap, so this never flags a file this flow produced.
//   Otherwise: skip, nothing to gain.
//
// Wire this before "Set Video Encoder (Custom)": output 1 -> continue to
// encoding, output 2 -> skip (route to end/no-op).

var sharedConstants = require('../../sharedEncodingConstants');
var BUCKETS = sharedConstants.BUCKETS;
var RESOLUTION_CAP = sharedConstants.RESOLUTION_CAP;

var details = function () { return ({
    name: 'Check Should Encode (Custom)',
    description: 'Skips files already HEVC, already <=1440p, and already within the resolution bucket encoder cap - nothing to gain from re-encoding them.',
    style: {
        borderColor: 'orange',
    },
    tags: 'video',
    isStartPlugin: false,
    pType: '',
    requiresVersion: '2.11.01',
    sidebarPosition: -1,
    icon: 'faQuestion',
    inputs: [],
    outputs: [
        {
            number: 1,
            tooltip: 'Needs encoding',
        },
        {
            number: 2,
            tooltip: 'Already good, skip',
        },
    ],
}); };
exports.details = details;

var plugin = function (args) {
    var lib = require('../../../../../methods/lib')();
    args.inputs = lib.loadDefaultValues(args.inputs, details);

    var vStream = args.inputFileObj.ffProbeData.streams.find(function (s) { return s.codec_type === 'video'; });
    var height = Number(vStream.height) || 0;
    var codecName = String(vStream.codec_name || '').toLowerCase();
    var bucket = BUCKETS.find(function (b) { return height <= b.maxHeight; });

    var rawBitrate = Number(vStream.bit_rate || args.inputFileObj.ffProbeData.format.bit_rate || 0);
    var sourceBitrateKbps = Math.round(rawBitrate / 1000);

    var wrongCodec = codecName !== 'hevc';
    var tooHighRes = height > RESOLUTION_CAP;
    var cap = Math.max(bucket.gpu.maxrate, bucket.cpu.maxrate);
    var tooHighBitrate = !wrongCodec && !tooHighRes && sourceBitrateKbps > cap * sharedConstants.REENCODE_BITRATE_TOLERANCE;

    var needsEncode = wrongCodec || tooHighRes || tooHighBitrate;

    args.jobLog("Should encode: codec=".concat(codecName, " height=").concat(height, " bitrateKbps=").concat(sourceBitrateKbps, " -> wrongCodec=").concat(wrongCodec, " tooHighRes=").concat(tooHighRes, " tooHighBitrate=").concat(tooHighBitrate));

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: needsEncode ? 1 : 2,
        variables: args.variables,
    };
};
exports.plugin = plugin;
