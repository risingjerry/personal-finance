# Security policy

pfin reads financial records and browser cookies on the user's own machine, so leaks matter.

Please report a vulnerability privately through GitHub's "Report a vulnerability" (Security → Advisories) rather than
a public issue. Include the affected command and how to reproduce it with made-up data. Never send real account data,
cookies or keys.

In scope: a secret (cookie, token, API key) reaching stdout, logs, files, error messages or command-line arguments; a
code path that can change something on a platform (pay, transfer, cancel, confirm); reading data outside what a
command documents.

Only the latest `main` is supported.
