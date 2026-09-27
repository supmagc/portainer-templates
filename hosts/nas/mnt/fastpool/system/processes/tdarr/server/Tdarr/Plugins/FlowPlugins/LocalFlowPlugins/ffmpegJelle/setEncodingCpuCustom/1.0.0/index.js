"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");

// No high/low quality split here per your call — x265's crf is already
// content-adaptive, so it doesn't have the "cq scale != crf" bloat failure
// mode that justified the split on the GPU/NVENC path.
//
// Wire this after "Set Video Encoder (Custom)" output 2 (CPU fallback), on the
// CPU/no-hardware branch. Assumes content >1440p was already downscaled to
// 1440p upstream (keep the stock "Set Video Resolution 1440p" node) — this
// node only buckets by height, it does not resize.

var BUCKETS = [
    { maxHeight: 576, maxrate: 1800, bufsize: 2400 },
    { maxHeight: 720, maxrate: 2700, bufsize: 3600 },
    { maxHeight: 1080, maxrate: 4200, bufsize: 5600 },
    { maxHeight: Infinity, maxrate: 7500, bufsize: 10000 }, // 1440p (post-downscale ceiling)
];

var details = function () { return ({
    name: 'Set Encoding (CPU, Custom)',
    description: 'Sets x265 crf22 + vbv-maxrate/bufsize matched to the output resolution bucket.',
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

    var vStream = args.inputFileObj.ffProbeData.streams.find(function (s) { return s.codec_type === 'video'; });
    var height = Number(vStream.height) || 0;
    var bucket = BUCKETS.find(function (b) { return height <= b.maxHeight; });

    args.variables.ffmpegCommand.overallOuputArguments.push(
        '-crf', '22',
        '-x265-params', "vbv-maxrate=".concat(bucket.maxrate, ":vbv-bufsize=").concat(bucket.bufsize),
    );

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: 1,
        variables: args.variables,
    };
};
exports.plugin = plugin;
