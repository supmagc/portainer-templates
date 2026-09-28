"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");
var child_process_1 = require("child_process");

// Two jobs on the video stream (thresholds in basicJelle/sharedEncodingConstants):
// 1. Suspicious sources (high fps or an old codec) get an ffmpeg idet probe; interlaced
//    ones get bwdif, telecined ones fieldmatch+decimate (IVTC).
// 2. Every output is forced to constant frame rate at the source's nominal rate (4/5 of
//    it after IVTC, which is what decimate outputs), halved while above MAX_OUTPUT_FPS -
//    an exact 2:1 drop, no judder. WMV/ASF timestamps otherwise pass through as VFR: one
//    test output dropped to 0.48fps and ended 3s short of its audio.
//    The rate lives here, not in a separate node, because forcing the source rate after
//    IVTC would re-add the frames decimate just removed.
//
// The filter is prepended to any existing -vf: ffmpeg only honours the last -vf, and the
// stock "Set Video Resolution" pushes its own without checking. So wire this LAST among
// filter-touching nodes. Unparseable idet output leaves the file uncorrected.

var C = require('../../../basicJelle/sharedEncodingConstants');

function classifyCadence(stderrText) {
    var multi = stderrText.match(/Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)/);
    if (!multi) {
        return 'none';
    }
    var tff = Number(multi[1]);
    var bff = Number(multi[2]);
    var progressive = Number(multi[3]);
    var undetermined = Number(multi[4]);
    var total = tff + bff + progressive + undetermined;
    if (total === 0) {
        return 'none';
    }
    var interlacedRatio = (tff + bff) / total;

    var repeated = stderrText.match(/Repeated Fields:\s*Neither:\s*(\d+)\s*Top:\s*(\d+)\s*Bottom:\s*(\d+)/);
    if (repeated) {
        var neither = Number(repeated[1]);
        var top = Number(repeated[2]);
        var bottom = Number(repeated[3]);
        var repeatedTotal = neither + top + bottom;
        var telecineRatio = repeatedTotal > 0 ? (top + bottom) / repeatedTotal : 0;
        if (telecineRatio > C.TELECINE_RATIO_THRESHOLD) {
            return 'telecine';
        }
    }
    if (interlacedRatio > C.INTERLACE_RATIO_THRESHOLD) {
        return 'interlace';
    }
    return 'none';
}

function probeCadence(args) {
    var duration = Number(args.inputFileObj.ffProbeData.format.duration) || 0;
    var seek = duration > C.PROBE_DURATION_SECONDS * 3 ? Math.floor(duration / 3) : 0;
    try {
        var result = (0, child_process_1.spawnSync)(args.ffmpegPath, [
            '-ss', String(seek),
            '-i', args.inputFileObj._id,
            '-t', String(C.PROBE_DURATION_SECONDS),
            '-an',
            '-vf', 'idet',
            '-f', 'null',
            '-',
        ], { encoding: 'utf8', timeout: C.PROBE_TIMEOUT_MS });
        return classifyCadence(result.stderr || '');
    }
    catch (err) {
        args.jobLog('Cadence: idet probe failed (' + err.message + '), leaving uncorrected');
        return 'none';
    }
}

function mergeVideoFilter(stream, filter) {
    var vfIndex = stream.outputArgs.indexOf('-vf');
    if (vfIndex !== -1 && stream.outputArgs[vfIndex + 1]) {
        stream.outputArgs[vfIndex + 1] = filter + ',' + stream.outputArgs[vfIndex + 1];
    }
    else {
        stream.outputArgs.push('-vf', filter);
    }
}

var details = function () { return ({
    name: 'Set Video Cadence (Custom)',
    description: 'Forces constant frame rate on every output (rates above ' + C.MAX_OUTPUT_FPS + ' halved), and deinterlaces/IVTCs suspicious sources (idet probe), merging into any existing -vf. Wire LAST among filter-touching nodes.',
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

    var probeStream = args.inputFileObj.ffProbeData.streams.find(function (s) { return s.codec_type === 'video'; });
    var rate = C.sourceRate(probeStream);
    var codecName = String(probeStream.codec_name || '').toLowerCase();
    var shouldProbe = (rate && rate.value > C.PROBE_FPS_THRESHOLD) || C.PROBE_CODECS.indexOf(codecName) !== -1;
    var cadence = shouldProbe ? probeCadence(args) : 'none';

    var vStream = args.variables.ffmpegCommand.streams.find(function (s) { return s.codec_type === 'video'; });
    if (cadence === 'telecine') {
        mergeVideoFilter(vStream, 'fieldmatch,decimate');
        if (rate) {
            rate = { num: rate.num * 4, den: rate.den * 5, value: rate.value * 0.8 };
        }
    }
    else if (cadence === 'interlace') {
        mergeVideoFilter(vStream, 'bwdif=mode=0');
    }

    if (rate) {
        rate = C.capRate(rate);
        vStream.outputArgs.push('-fps_mode', 'cfr', '-r', rate.num + '/' + rate.den);
    }

    args.jobLog('Cadence: codec=' + codecName + ', probed=' + shouldProbe + ' -> ' + cadence
        + (rate ? ', output CFR ' + rate.value.toFixed(3) + 'fps' : ', no sane source frame rate - timing left as-is'));

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: 1,
        variables: args.variables,
    };
};
exports.plugin = plugin;
