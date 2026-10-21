# Security

Please report vulnerabilities privately: use GitHub's "Report a vulnerability" (Security tab →
Advisories) on this repository rather than a public issue.

In scope: anything that lets maps-mcp reach a host other than www.google.com, lets the card
view load or connect anywhere beyond its declared hosts or run script from tool data, leaks
the bearer token or the state directory, makes a tool result carry exception text, or serves
`/mcp` without a valid bearer.

Google changing the format of its Maps pages is not a vulnerability; open a normal issue.

Only the latest release is supported.
