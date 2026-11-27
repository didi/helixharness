#!/usr/bin/env node
'use strict'

// Explicit CLI only: OpenCode has no native Stop hook for this workflow.
const core = require('../../skills/d-d2c/scripts/lib/d2c-explicit-gate-core.cjs')
const options = { host: 'opencode', allowClaim: true, requireOwnership: true, requireVisualEvidence: true }
const parseArgs = (argv) => core.parseArgs(argv, options)
const check = (args) => core.check(args, options)
const main = (argv = process.argv.slice(2)) => core.main(argv, options)
if (require.main === module) process.exitCode = main()
module.exports = { check, main, parseArgs }
