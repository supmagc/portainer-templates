"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.plugin = exports.details = void 0;

// Skips files this flow already produced, so renames/fresh scans don't re-encode them.
// The tag is written by "Apply Tags (Custom)" as:
//   -metadata <tagName>=<tagVersion> -movflags +use_metadata_tags   (mp4 drops custom keys without the movflag)
// Tag Name itself is what distinguishes flows (e.g. per-library via a
// {{{args.userVariables.library.*}}} template) - bump Tag Version to force
// every already-tagged file to be treated as unprocessed again.

var details = function () { return ({
    name: 'Check Tags (Custom)',
    description: 'Checks the container-level metadata tag written by Apply Tags (Custom) for this tag name/version.',
    style: {
        borderColor: 'orange',
    },
    tags: 'video',
    isStartPlugin: false,
    pType: '',
    requiresVersion: '2.11.01',
    sidebarPosition: -1,
    icon: 'faQuestion',
    inputs: [
        {
            label: 'Tag Name',
            name: 'tagName',
            type: 'string',
            defaultValue: 'TDARR',
            inputUI: { type: 'text' },
            tooltip: 'Container metadata key to look for (case-insensitive). Must match the Tag Name on Apply Tags (Custom). To vary per library, use e.g. {{{args.userVariables.library.tdarrTagName}}} instead of a literal.',
        },
        {
            label: 'Tag Version',
            name: 'tagVersion',
            type: 'string',
            defaultValue: '1',
            inputUI: { type: 'text' },
            tooltip: 'Bump this (and on Apply Tags) to force files tagged by an older version to be reprocessed. To vary per library, use e.g. {{{args.userVariables.library.tdarrTagVersion}}} instead of a literal.',
        },
    ],
    outputs: [
        {
            number: 1,
            tooltip: 'Tag present: already processed',
        },
        {
            number: 2,
            tooltip: 'Tag missing or different value',
        },
    ],
}); };
exports.details = details;

var plugin = function (args) {
    var lib = require('../../../../../methods/lib')();
    args.inputs = lib.loadDefaultValues(args.inputs, details);

    var wantName = String(args.inputs.tagName).toLowerCase();
    var wantValue = String(args.inputs.tagVersion);
    var tags = (args.inputFileObj.ffProbeData.format || {}).tags || {};
    var key = Object.keys(tags).find(function (k) { return k.toLowerCase() === wantName; });
    var found = key !== undefined && String(tags[key]) === wantValue;
    args.jobLog(args.inputs.tagName + "=" + (key !== undefined ? tags[key] : '<none>') + ", expected " + wantValue + ", processed: " + found);

    return {
        outputFileObj: args.inputFileObj,
        outputNumber: found ? 1 : 2,
        variables: args.variables,
    };
};
exports.plugin = plugin;
