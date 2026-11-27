#!/usr/bin/env node
'use strict'

// Keep Cursor's existing explicit checkpoint contract and error reason.
const core = require('../../skills/d-d2c/scripts/lib/d2c-explicit-gate-core.cjs')
if (require.main === module) process.exitCode = core.main()
module.exports = core
