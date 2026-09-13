# Security Policy

## Supported code

Security fixes target the current default branch (`master`) and the latest supported release line. Older snapshots may receive fixes only when explicitly announced.

## Reporting a vulnerability

Please do **not** publish exploitable security details in a public issue. Send a private report to **support@ailinux.me** with:

- affected repository and revision/version,
- impact and prerequisites,
- minimal reproduction steps,
- relevant logs with secrets removed,
- suggested mitigation if known.

AILinux will triage reports based on practical impact and reproducibility. Never include passwords, API keys, private keys, session tokens, wallet secrets, or personal data in a report.

## Security expectations

- Secrets belong in local environment/configuration stores, OS keyrings, or GitHub Secrets; never commit them.
- Destructive changes require backups and verification.
- Network services should default to the narrowest required bind address and permissions.
- Third-party dependency advisories remain subject to the upstream package's support window.
