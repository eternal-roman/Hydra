# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in HYDRA, please report it responsibly:

- **Email:** Open a [GitHub Issue](../../issues) with the label `security`
- **Do NOT** open pull requests containing exploit details or proof-of-concept code

We will acknowledge reports within 48 hours and work to resolve confirmed vulnerabilities promptly.

## Secrets and API Keys

This project connects to Kraken, Anthropic, and xAI APIs. API keys are loaded from a `.env` file that is **gitignored** and must never be committed.

If you fork this repo:
- Never commit `.env`, API keys, or credentials
- Enable [GitHub Secret Scanning](https://docs.github.com/en/code-security/secret-scanning) and **Push Protection** on your fork
- Rotate any key you suspect has been exposed

## Scope

The following are in scope for security reports:
- Secret leakage (API keys, credentials)
- Command injection via WSL/Kraken CLI calls
- WebSocket vulnerabilities in the dashboard connection
- Logic flaws that could cause unintended order execution

Out of scope:
- Trading strategy effectiveness or financial losses
- Issues in third-party dependencies (report upstream)
