"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;
var flowUtils_1 = require("../../../../FlowHelpers/1.0.0/interfaces/flowUtils");

// Writes the marker "Check Tags (Custom)" looks for on the next pass:
//   -metadata <tagName>=<tagVersion> -movflags +use_metadata_tags
// (mp4 drops custom metadata keys on remux without that movflag).
// Only pushes to overallOuputArguments, so it's independent of the
// "-tag:v hvc1" container-compat tag set elsewhere in the flow (Custom
// Arguments node) — wire it anywhere before Execute, order doesn't matter.

var details = function () { return ({
    name: 'Apply Tags (Custom)',
    description: 'Writes the container-level metadata tag (name + version) that Check Tags (Custom) checks for.',
    style: {
        borderColor: '#6efefc',
    },
    tags: 'video',
    isStartPlugin: false,
    pType: '',
    requiresVersion: '2.11.01',
    sidebarPosition: -1,
    icon: '',
    inputs: [
        {
            label: 'Tag Name',
            name: 'tagName',
            type: 'string',
            defaultValue: 'TDARR',
            inputUI: { type: 'text' },
            tooltip: 'Container metadata key to write. Must match the Tag Name on Check Tags (Custom). To vary per library, use e.g. {{{args.userVariables.library.tdarrTagName}}} instead of a literal.',
        },
        {
            label: 'Tag Version',
            name: 'tagVersion',
            type: 'string',
            defaultValue: '1',
            inputUI: { type: 'text' },
            tooltip: 'Bump this (and on Check Tags) to force files tagged by an older version to be reprocessed. To vary per library, use e.g. {{{args.userVariables.library.tdarrTagVersion}}} instead of a literal.',
        },
    ],
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

    args.variables.ffmpegCommand.overallOuputArguments.push(
        '-metadata', "".concat(args.inputs.tagName, "=").concat(args.inputs.tagVersion),
        '-movflags', '+use_metadata_tags',
    );

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: 1,
        variables: args.variables,
    };
};
exports.plugin = plugin;
