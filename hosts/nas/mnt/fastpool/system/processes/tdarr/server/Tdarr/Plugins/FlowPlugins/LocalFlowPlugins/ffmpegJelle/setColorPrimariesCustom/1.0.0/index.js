"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");

// New node, not a replacement of anything in the original flow — the
// original flow doesn't tag output colorimetry at all, which is the likely
// cause of the desaturated-looking transcodes. Reads the source's actual
// color tags via ffprobe and stamps matching values on the output, instead
// of leaving NVENC/libx265 to default to "unspecified" (which is what
// causes players to guess wrong and render flat/desaturated).
//
// Wire this in once, after the video-encoder node, before Execute — it
// doesn't need to be duplicated per CPU/GPU branch, the tags are the same
// either way.

var details = function () { return ({
    name: 'Set Color Primaries (Custom)',
    description: 'Copies color_primaries/color_trc/colorspace/color_range from source to output.',
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

    // ffprobe's tag names aren't all valid ffmpeg option values (verify via `ffmpeg -h full`)
    var trcOptionName = { bt470m: 'gamma22', bt470bg: 'gamma28' };
    var spaceOptionName = { gbr: 'rgb' };
    var known = function (v) { return v && v !== 'unknown' && v !== 'reserved'; };

    var primaries = known(vStream.color_primaries) ? vStream.color_primaries : 'bt709';
    var transfer = known(vStream.color_transfer) ? (trcOptionName[vStream.color_transfer] || vStream.color_transfer) : 'bt709';
    var space = known(vStream.color_space) ? (spaceOptionName[vStream.color_space] || vStream.color_space) : 'bt709';
    var range = (vStream.color_range === 'pc') ? 'pc' : 'tv';

    args.variables.ffmpegCommand.overallOuputArguments.push(
        '-color_primaries', primaries,
        '-color_trc', transfer,
        '-colorspace', space,
        '-color_range', range,
    );

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: 1,
        variables: args.variables,
    };
};
exports.plugin = plugin;
