# greenmark-dmarc

A small, self-hosted DMARC aggregate (rua) report monitor that runs as a single Docker container.

It reads DMARC reports from an IMAP mailbox on a schedule, stores the results in SQLite, and shows a simple dashboard: pass and fail counts per domain, which sending servers are failing, and an optional client name for each domain. Built for an MSP that wants report emails out of a human inbox, and usable by anyone with the same need.

**Status:** early development. See [docs/approach.md](docs/approach.md) for the design.

Parsing is done by [parsedmarc](https://github.com/domainaware/parsedmarc) (Apache-2.0).

## Licence

Apache-2.0. See [LICENSE](LICENSE).
