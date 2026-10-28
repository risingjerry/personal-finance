'use strict';
// Error codes shared by every command. Exit code is 0 on success, 1 otherwise;
// the JSON error always goes to stderr so stdout stays machine-parseable.
const codes = {
  USAGE: 'usage',
  BAD_INPUT: 'bad_input',
  NEED_LOGIN: 'need_login',
  SECURITY_CHALLENGE: 'security_challenge',
  BROWSER_DOWN: 'browser_down',
  CHROME_MISSING: 'chrome_missing',
  API_FORBIDDEN: 'api_forbidden',
  RATE_LIMITED: 'rate_limited',
  NO_DATA: 'no_data',
  LOCKED: 'locked',
  INTERNAL: 'internal',
};

class CliError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = 'CliError';
    this.code = code;
    this.details = details;
  }
  toJSON() {
    return { code: this.code, message: this.message, details: this.details };
  }
}

module.exports = { CliError, codes };
