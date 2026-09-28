"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");

// NVENC (P400, Pascal). cq sets the quality; maxrate/bufsize are only a safety valve.
// Values live in basicJelle/sharedEncodingConstants.
// - -b:v 0: a -b:v target is ignored in -rc vbr -cq mode (encodes just sat on maxrate).
// - Sources below the bucket's bits-per-pixel threshold get its degraded cq.
// - maxrate is also capped at a fraction of the source bitrate (SOURCE_MAXRATE_RATIO/FLOOR).
// - Keep the flags identical to the sweep below, or its numbers don't transfer.
//   Pascal HEVC NVENC has no B-frames or temporal AQ.
//
// Calibration sweep 2026-09-28: uncapped (-b:v 0, spatial AQ, lookahead 32, p7),
// 180s sample per file, video kb/s:
//   source                         cq26  cq28  cq30  cq32  cq34  cq36
//   1440p (from 4K WEBDL, 23.976)  9529  7192  5420  4056  3047  2286
//   1080p WEBDL (23.976)           6534  4995  3863  2998  2332  1821
//   720p WEBDL                     3030  2370  1855  1444  1135   885
//   720p WMV (29.97, src 1500k)    2693  2159  1746  1382  1069   822
//   720p WMV (25, src 1600k)       2741  2175  1714  1317  1003   759
// Visual check: 720p/1080p fine at cq32 (1080p cq36 not ok, 720p cq34 borderline);
// 1440p fine up to cq36; WMVs looked the same from cq30 to cq36.
// maxrate ~2.5x the cq32 average, bufsize 2x maxrate.
//
// Wire after "Set Video Encoder (Custom)" output 1. Assumes >1440p was already
// downscaled upstream ("Set Video Resolution 1440p").

var sharedConstants = require('../../../basicJelle/sharedEncodingConstants');
var BUCKETS = sharedConstants.BUCKETS;

var details = function () { return ({
    name: 'Set Encoding (GPU, Custom)',
    description: 'Sets NVENC cq (higher for low bits-per-pixel sources) with -b:v 0 and a per-resolution maxrate/bufsize, capped at '
        + (sharedConstants.SOURCE_MAXRATE_RATIO * 100) + '% of source bitrate; p7 + spatial AQ + lookahead.',
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

    // Prefer the video stream's own bit_rate; not all containers populate it
    // per-stream (e.g. some MKVs), so fall back to the overall format bitrate.
    var rawBitrate = Number(vStream.bit_rate || args.inputFileObj.ffProbeData.format.bit_rate || 0);
    var sourceBitrateKbps = Math.round(rawBitrate / 1000);
    var sourceBpp = (width > 0 && height > 0 && fps > 0 && rawBitrate > 0)
        ? rawBitrate / (width * height * fps)
        : bucket.bppThreshold; // unknown bitrate -> can't judge, default to healthy
    var enc = bucket.gpu;
    var cq = sourceBpp < bucket.bppThreshold ? enc.degraded : enc.healthy;

    var maxrate = enc.maxrate;
    if (sourceBitrateKbps > 0) {
        maxrate = Math.max(Math.min(maxrate, sourceBitrateKbps * sharedConstants.SOURCE_MAXRATE_RATIO),
            enc.maxrate * sharedConstants.SOURCE_MAXRATE_FLOOR);
    }
    maxrate = Math.round(maxrate);
    var bufsize = Math.round(maxrate * (enc.bufsize / enc.maxrate));

    args.jobLog('GPU encoding: source ' + sourceBitrateKbps + 'k, bpp ' + sourceBpp.toFixed(4) + ' @' + fps.toFixed(3) + 'fps'
        + ' (threshold ' + bucket.bppThreshold + ') -> cq' + cq + ', maxrate ' + maxrate + 'k, bufsize ' + bufsize + 'k');

    args.variables.ffmpegCommand.overallOuputArguments.push(
        '-rc', 'vbr',
        '-cq', String(cq),
        '-b:v', '0',
        '-maxrate', maxrate + 'k',
        '-bufsize', bufsize + 'k',
        '-preset', 'p7',
        '-spatial_aq', '1',
        '-rc-lookahead', '32',
    );

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: 1,
        variables: args.variables,
    };
};
exports.plugin = plugin;
