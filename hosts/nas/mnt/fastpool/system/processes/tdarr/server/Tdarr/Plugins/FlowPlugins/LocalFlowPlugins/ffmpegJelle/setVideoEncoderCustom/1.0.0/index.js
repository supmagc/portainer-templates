"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var hardwareUtils_1 = require("../../../../FlowHelpers/1.0.0/hardwareUtils");
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");

// Replaces the stock "Check Node Hardware Encoder" + per-branch encoder nodes:
// detects hevc_nvenc on the node the same way the stock check does (getEncoder
// enabledDevices), sets -c:v accordingly and routes:
//   output 1 -> hevc_nvenc available -> wire to "Set Encoding (GPU, Custom)"
//   output 2 -> CPU fallback (libx265) -> wire to "Set Encoding (CPU, Custom)"

var details = function () { return ({
    name: 'Set Video Encoder (Custom)',
    description: 'Sets -c:v to hevc_nvenc if the node has it (output 1), otherwise libx265 (output 2).',
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
            tooltip: 'GPU: hevc_nvenc set',
        },
        {
            number: 2,
            tooltip: 'CPU fallback: libx265 set',
        },
    ],
}); };
exports.details = details;

var plugin = async function (args) {
    var lib = require('../../../../../methods/lib')();
    args.inputs = lib.loadDefaultValues(args.inputs, details);
    (0, flowUtils_1.checkFfmpegCommandInit)(args);

    var encoderProperties = await (0, hardwareUtils_1.getEncoder)({
        targetCodec: 'hevc',
        hardwareEncoding: true,
        hardwareType: 'auto',
        args: args,
    });
    var hasNvenc = encoderProperties.enabledDevices.some(function (row) { return row.encoder === 'hevc_nvenc'; });
    var encoder = hasNvenc ? 'hevc_nvenc' : 'libx265';
    args.jobLog("Node has hevc_nvenc: " + hasNvenc + ", using " + encoder);

    args.variables.ffmpegCommand.overallOuputArguments.push('-c:v', encoder);

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: hasNvenc ? 1 : 2,
        variables: args.variables,
    };
};
exports.plugin = plugin;
