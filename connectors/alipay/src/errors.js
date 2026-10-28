'use strict';

// One error type for everything the CLI reports to an Agent.
// code is the stable machine-readable contract (see docs/PLAYBOOK.md).
class CliError extends Error {
  constructor(code, message, details, exitCode = 1) {
    super(message);
    this.name = 'CliError';
    this.code = code;
    this.details = details;
    this.exitCode = exitCode;
  }

  toJSON() {
    const err = { code: this.code, message: this.message };
    if (this.details !== undefined) err.details = this.details;
    return err;
  }
}

const codes = {
  USAGE: 'usage',
  LOCKED: 'locked',
  NO_DATA: 'no_data',
  NOT_FOUND: 'not_found',
  BAD_INPUT: 'bad_input',
  BAD_ZIP: 'bad_zip',
  BAD_CSV: 'bad_csv',
  BROWSER_DOWN: 'browser_down',
  CHROME_MISSING: 'chrome_missing',
  NEED_LOGIN: 'need_login',
  SECURITY_CHALLENGE: 'security_challenge',
  RATE_LIMITED: 'rate_limited',
  EXPORT_TIMEOUT: 'export_timeout',
  EMPTY: 'empty',
  INTERNAL: 'internal',
};

function usage(message, details) {
  return new CliError(codes.USAGE, message, details);
}

module.exports = { CliError, codes, usage };
