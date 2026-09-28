"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");

// x265 CPU fallback: same bits-per-pixel gate and source-bitrate cap as the GPU node, with
// crf instead of cq. Values live in basicJelle/sharedEncodingConstants. No -preset is set,
// so x265 runs its default "medium".
//
// Calibration sweep 2026-09-28: uncapped, same 180s samples as the GPU sweep, video kb/s:
//   source                         crf22  crf25  crf28  crf31
//   1440p (from 4K WEBDL, 23.976)   3390   2207   1472    995
//   1080p WEBDL (23.976)            4106   2681   1793   1221
//   720p WEBDL                      1575   1082    751    523
//   720p WMV (29.97, src 1500k)     1708   1141    739    476
//   720p WMV (25, src 1600k)        2212   1413    850    525
// crf chosen by visual check, landing near the GPU path's bitrates. crf22 grows the WMVs
// past their source. Speed: 1440p runs 4-6x slower than realtime (hours per 4K file),
// 1080p ~2.5-3x, 720p ~1-3x - fine as a fallback, not as the main path.
//
// Wire after "Set Video Encoder (Custom)" output 2. Assumes >1440p was already
// downscaled upstream ("Set Video Resolution 1440p").

var sharedConstants = require('../../../basicJelle/sharedEncodingConstants');
var BUCKETS = sharedConstants.BUCKETS;

var details = function () { return ({
    name: 'Set Encoding (CPU, Custom)',
    description: 'Sets x265 crf (higher for low bits-per-pixel sources) with a per-resolution vbv-maxrate/bufsize, capped at '
        + (sharedConstants.SOURCE_MAXRATE_RATIO * 100) + '% of source bitrate.',
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
    var width = Number(vStream.width) || 0;
    var height = Number(vStream.height) || 0;
    var fps = sharedConstants.encodedFps(vStream);
    var bucket = BUCKETS.find(function (b) { return height <= b.maxHeight; });

    var rawBitrate = Number(vStream.bit_rate || args.inputFileObj.ffProbeData.format.bit_rate || 0);
    var sourceBitrateKbps = Math.round(rawBitrate / 1000);
    var sourceBpp = (width > 0 && height > 0 && fps > 0 && rawBitrate > 0)
        ? rawBitrate / (width * height * fps)
        : bucket.bppThreshold; // unknown bitrate -> can't judge, default to healthy

    var enc = bucket.cpu;
    var crf = sourceBpp < bucket.bppThreshold ? enc.degraded : enc.healthy;

    var maxrate = enc.maxrate;
    if (sourceBitrateKbps > 0) {
        maxrate = Math.max(Math.min(maxrate, sourceBitrateKbps * sharedConstants.SOURCE_MAXRATE_RATIO),
            enc.maxrate * sharedConstants.SOURCE_MAXRATE_FLOOR);
    }
    maxrate = Math.round(maxrate);
    var bufsize = Math.round(maxrate * (enc.bufsize / enc.maxrate));

    args.jobLog('CPU encoding: source ' + sourceBitrateKbps + 'k, bpp ' + sourceBpp.toFixed(4) + ' @' + fps.toFixed(3) + 'fps'
        + ' (threshold ' + bucket.bppThreshold + ') -> crf' + crf + ', vbv-maxrate ' + maxrate + 'k, vbv-bufsize ' + bufsize + 'k');

    args.variables.ffmpegCommand.overallOuputArguments.push(
        '-crf', String(crf),
        '-x265-params', 'vbv-maxrate=' + maxrate + ':vbv-bufsize=' + bufsize,
    );

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: 1,
        variables: args.variables,
    };
};
exports.plugin = plugin;
