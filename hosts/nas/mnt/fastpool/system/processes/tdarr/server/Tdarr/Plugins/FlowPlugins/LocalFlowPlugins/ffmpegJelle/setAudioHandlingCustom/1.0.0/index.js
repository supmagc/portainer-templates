"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");

// First audio stream only - multi-track files aren't handled per track. Codec lists and
// bitrates live in basicJelle/sharedEncodingConstants:
//   AUDIO_COPY_CODECS                          -> copy
//   AUDIO_HIGH_QUALITY_CODECS / pcm_*, >2 ch   -> eac3 (AC3 caps at 640k/5.1; EAC3 is better
//                                                 per bit and plays wherever AC3 does)
//   everything else                            -> aac at the source bitrate, clamped
// Matching the source bitrate stops lossy audio from growing: a 128k WMA used to become
// 192k AAC (+49%).

var C = require('../../../basicJelle/sharedEncodingConstants');

function isHighQuality(codec) {
    return C.AUDIO_HIGH_QUALITY_CODECS.indexOf(codec) !== -1 || codec.indexOf('pcm_') === 0;
}

// MKV often only carries the bitrate in a BPS tag, not in bit_rate.
function sourceKbps(aStream) {
    var tags = aStream.tags || {};
    var bps = Number(aStream.bit_rate || tags.BPS || tags['BPS-eng'] || 0);
    return Math.round(bps / 1000);
}

var details = function () { return ({
    name: 'Set Audio Handling (Custom)',
    description: 'Copies ' + C.AUDIO_COPY_CODECS.join('/') + '. Transcodes lossless/high-bitrate multichannel to eac3 '
        + C.AUDIO_MULTICHANNEL_KBPS + 'k, everything else to aac at the source bitrate (max ' + C.AUDIO_AAC_MAX_KBPS + 'k).',
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
    var output = args.variables.ffmpegCommand.overallOuputArguments;

    if (!aStream) {
        args.jobLog('Audio: no audio stream');
    } else {
        var codec = String(aStream.codec_name || '');
        var channels = Number(aStream.channels) || 0;
        var srcKbps = sourceKbps(aStream);
        var decision;
        if (C.AUDIO_COPY_CODECS.indexOf(codec) !== -1) {
            output.push('-c:a', 'copy');
            decision = 'copy';
        } else if (isHighQuality(codec) && channels > 2) {
            output.push('-c:a', 'eac3', '-b:a', C.AUDIO_MULTICHANNEL_KBPS + 'k');
            decision = 'eac3 ' + C.AUDIO_MULTICHANNEL_KBPS + 'k';
        } else {
            var aacKbps = srcKbps > 0
                ? Math.max(C.AUDIO_AAC_MIN_KBPS, Math.min(C.AUDIO_AAC_MAX_KBPS, srcKbps))
                : C.AUDIO_AAC_MAX_KBPS;
            output.push('-c:a', 'aac', '-b:a', aacKbps + 'k');
            decision = 'aac ' + aacKbps + 'k';
        }
        args.jobLog('Audio: ' + codec + ' ' + channels + 'ch ' + (srcKbps || '?') + 'k -> ' + decision);
    }

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: 1,
        variables: args.variables,
    };
};
exports.plugin = plugin;
