# greenmark-dmarc

A small, self-hosted DMARC aggregate (rua) report monitor that runs as a single Docker container.

It reads DMARC report emails from an IMAP mailbox on a schedule, stores the results in SQLite, and shows a simple dashboard:

- every domain with its message count, DMARC pass rate and failures for the last 7, 30, 90 or 365 days;
- for each domain, the servers sending as it, and whether each passes or fails SPF, DKIM and DMARC;
- messages that passed DMARC only through SPF because DKIM failed, which is easy to miss;
- an optional client name for each domain, and a row for every expected domain even before its first report arrives.

It is built for an IT provider that wants report emails out of a human inbox, and is usable by anyone with the same need. Parsing is done by [parsedmarc](https://github.com/domainaware/parsedmarc).

## Quick start (Docker Compose)

```sh
cp docker-compose.example.yml docker-compose.yml   # then edit the values
docker compose up -d
```

Open `http://<host>:8080` and log in with `ADMIN_USER` and `ADMIN_PASSWORD`.

## Unraid

Copy `unraid/greenmark-dmarc.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the Unraid server, then go to **Docker → Add Container** and pick `greenmark-dmarc` from the template list. Fill in the admin password and mailbox details. The image is published to `ghcr.io/e2346437/greenmark-dmarc:latest` by GitHub Actions.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_USER` | `admin` | Dashboard login name |
| `ADMIN_PASSWORD` | (required) | Dashboard password. The container will not start without it. |
| `IMAP_HOST` | | Mail server that receives the reports. Leave empty to use uploads and the drop folder only. |
| `IMAP_PORT` | `993` | IMAP port |
| `IMAP_USER` / `IMAP_PASSWORD` | | Mailbox login |
| `IMAP_SSL` / `IMAP_VERIFY` | `true` | Use TLS, and verify the server certificate |
| `IMAP_FOLDER` | `INBOX` | Folder to read reports from |
| `ARCHIVE_FOLDER` | `Archive` | Processed emails are moved here |
| `POLL_MINUTES` | `30` | How often to check the mailbox |
| `OFFLINE` | `false` | Skip reverse DNS lookups |
| `DATA_DIR` | `/data` | Where the database lives. Mount a volume here. |
| `PORT` | `8080` | Web port inside the container |

### Client names (optional)

Put a `domains.yaml` in the data folder. See [domains.example.yaml](domains.example.yaml).

### Importing old reports

Drop `.xml`, `.xml.gz` or `.zip` report files into `<data folder>/import/`. They are imported on the next check and moved to `import/done/` (or `import/failed/`). You can also upload files from the dashboard.

## Pointing domains at the mailbox

In each domain's DMARC record, set `rua=mailto:<your report mailbox>`. If the mailbox is on a different domain from the one being reported on, the mailbox's domain must also publish an authorization record, or some senders will not deliver the reports:

```
example.com._report._dmarc.reports-domain.net.  TXT  "v=DMARC1"
```

A single wildcard record, `*._report._dmarc.reports-domain.net`, covers all domains.

## Security

The dashboard has one admin login and no HTTPS of its own. Keep it on your local network or put it behind a reverse proxy with TLS.

## Development

```sh
pip install -r requirements-dev.txt
pytest
```

The test reports in `tests/fixtures/` are synthetic and use reserved example domains and documentation IP ranges.

See [docs/approach.md](docs/approach.md) for the design.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
