"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");

// Rules:
//   - aac / ac3 / eac3              -> copy (already efficient/compatible)
//   - "high quality" multichannel   -> transcode to eac3 640k
//     (dts, dts-hd, truehd, mlp, pcm_*, flac, with channels > 2)
//   - "high quality" but stereo     -> transcode to aac 192k
//     (no surround to preserve, ac3/eac3 buys nothing over aac here)
//   - anything else (mp3, wma, ...) -> transcode to aac 192k
//     (source was already lossy/low quality, no point spending bits on eac3)
//
// EAC3 chosen over AC3 for the high-quality multichannel case: AC3 caps at
// 640kbps/5.1, EAC3 does better quality-per-bit and is supported by
// everything that plays AC3 (Jellyfin/Plex/Emby, most AVRs since ~2010+).
//
// Known limitation: only the first audio stream is inspected. Multi-audio-
// track files (e.g. commentary tracks) aren't handled per-track.

var PASSTHROUGH_CODECS = ['aac', 'ac3', 'eac3'];
var HIGH_QUALITY_CODECS = ['dts', 'truehd', 'mlp', 'flac'];

function isHighQuality(codec) {
    return HIGH_QUALITY_CODECS.indexOf(codec) !== -1 || codec.indexOf('pcm_') === 0;
}

var details = function () { return ({
    name: 'Set Audio Handling (Custom)',
    description: 'Copies aac/ac3/eac3. Transcodes high-quality multichannel sources to eac3 640k, everything else (incl. high-quality stereo) to aac 192k.',
    style: {
        borderColor: '#6efefc',
    },
    tags: 'audio',
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

    var aStream = args.inputFileObj.ffProbeData.streams.find(function (s) { return s.codec_type === 'audio'; });
    var codec = aStream ? aStream.codec_name : '';
    var channels = aStream ? (Number(aStream.channels) || 0) : 0;

    if (PASSTHROUGH_CODECS.indexOf(codec) !== -1) {
        args.variables.ffmpegCommand.overallOuputArguments.push('-c:a', 'copy');
    } else if (isHighQuality(codec) && channels > 2) {
        args.variables.ffmpegCommand.overallOuputArguments.push('-c:a', 'eac3', '-b:a', '640k');
    } else {
        args.variables.ffmpegCommand.overallOuputArguments.push('-c:a', 'aac', '-b:a', '192k');
    }

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: 1,
        variables: args.variables,
    };
};
exports.plugin = plugin;
