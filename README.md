**English** | [Русский](README.ru.md)

# BookHub

A personal EPUB library with a web shelf, reading-position sync and an OPDS catalog.
You can read the same EPUB on several KOReader or CrossPoint devices; Kindle connects through
a KFX ↔ EPUB adapter. The web interface is in Russian.

This is an early release for self-hosting. The public repository is updated with periodic
snapshots; main development happens elsewhere. There is no ongoing support and no fixed
update schedule. The version of this snapshot is recorded in [SNAPSHOT.md](SNAPSHOT.md)
(in Russian).

## Features

- EPUB upload, covers, catalog search, statuses and several versions of the same book.
- Exact EPUB position sync between readers over KOSync.
- Kindle: KFX import, EPUB extraction from DRM-free files and KFX ↔ EPUB position translation.
- OPDS: browse library sections and download the main EPUB straight from the reader.
- CrossPoint X3: manual EPUB transfer, file presence info and position retrieval.
- AI reviews, recommendations and description translation — only when you press the button.

AI and X3 are optional. A plain EPUB library doesn't need a Kindle either.

## Before you start

**The web shelf has no authentication.** Anyone who can reach its port can read, download and
delete books, change settings and trigger paid requests to the configured AI. Use a trusted
home network. For outside access you need a VPN or an HTTPS proxy with authentication. CORS
protection is not a substitute for a password. The sync and OPDS password does not protect
the web shelf.

By default the ports are reachable only from the server itself (`127.0.0.1`). Books and
secrets are stored locally and are not part of the repository. Details about your data are
in [SECURITY.md](SECURITY.md) (in Russian).

## Quick start

You need Docker Engine with Compose v2, Python 3 to generate the settings, and internet
access during the build. Run the commands below from the downloaded or cloned project
directory.

```sh
python3 scripts/configure.py
docker compose up -d --build --wait
```

The first step creates `.env` with a separate random sync password and Kindle token. Running
it again does not overwrite an existing file. Alternatively, copy `.env.example` to `.env`
and fill in `SYNC_PASSWORD` and `SYNC_API_KEY` yourself; empty values are rejected.

The shelf is at **http://127.0.0.1:8088**. Click «Добавить книгу» (Add book) → «Загрузить
EPUB» (Upload EPUB).

To connect readers on your home network, set `BOOKHUB_BIND` in `.env` to the server's local IP
(for example, `192.168.1.10`). `0.0.0.0` opens the ports on all interfaces — use it only with
suitable network rules. Then run `docker compose up -d` again. The shelf will be available at
`http://<server-IP>:8088`.

```sh
docker compose ps
docker compose logs --tail=100 web sync worker
docker compose stop
docker compose start
```

The application creates `data/` next to `compose.yaml`. The three containers share one SQLite
database and the files; no separate DBMS is needed. If the ports are taken, change `WEB_PORT`
and `SYNC_PORT` in `.env`.

## Connecting readers

| Reader / feature | Default address | Credentials from `.env` |
|---|---|---|
| KOReader / CrossPoint KOSync | `http://<server-IP>:8089` | `SYNC_USER`, `SYNC_PASSWORD` |
| OPDS | `http://<server-IP>:8089/opds` | same login and password, HTTP Basic |
| Kindle | `http://<server-IP>:8088` | `SYNC_API_KEY` in the Kindle client |

The readers must have **the very same EPUB**. A different edition of the book may have
different markup and an independent position. The percentage is shown for convenience but is
not what position translation is based on. An unknown document can be linked to a book
manually in the «Устройства» (Devices) section.

Sync automatically moves reading forward only. To re-read a book, reset its position. A
KOSync client that was offline may push the old position again after a reset, so sync your
devices first. An ambiguous Kindle position is never applied by guesswork.

OPDS lists books that have an EPUB, except finished and dropped ones. Catalog sections are
configured on the shelf. OPDS downloads are initiated by the reader; files are never pushed
automatically.

Kindle client, requirements and installation: [kindle-client/README.md](kindle-client/README.md)
(in Russian). Technical protocol: [reader-sync/API.md](reader-sync/API.md).

## AI and X3

For AI, fill in `AI_URL` and `AI_MODEL`, plus `AI_API_KEY` if required. The URL is the service
base to which the application appends `/v1/chat/completions`; any compatible server works,
including Ollama. The address must be reachable from Docker: `localhost` inside a container
is the container itself. `AI_SUMMARY_MODEL` and `AI_HINT_MODEL` let you pick separate models.
After changing `.env`, run `docker compose up -d`.

Reviews, recommendations, description translation and AI hints run only on demand. A
translation is stored separately from the original description. Until AI is configured the
library works normally and the corresponding buttons are disabled.

To transfer files to a CrossPoint X3, set `X3_URL` — the address of its Wi-Fi web service —
and, if needed, `X3_BOOKS_DIR`. Open the file transfer screen on the reader and use
«Отправить на X3» (Send to X3). None of this is needed for plain KOSync or OPDS. For WebSocket
transfer the server also needs access to the device port one above its HTTP service port.

When you manually connect to the X3, the shelf refreshes the file list, fetches the saved
reading position and can remove files of previously deleted cards from the X3. It tracks
which file belongs to which book; the current state is shown on the book card and in the
«Устройства» (Devices) section.

## Updating and backups

Before updating, stop all three processes and copy the **whole** `data/` directory, including
the SQLite WAL if present. Don't copy just the main database file while the application is
running.

```sh
docker compose stop
tar -czf "../bookhub-data-$(date +%Y%m%d-%H%M%S).tar.gz" data
```

Keep `.env` separately in a safe place. Get the new published version of the code with
`git pull --ff-only` or by replacing the files from an archive, keeping your `.env` and
`data/`, then:

```sh
docker compose up -d --build --wait
```

The database schema is upgraded on startup. Rolling back requires a backup of the data **and**
the previous version of the application. Deleting a card also deletes the files that belong
to it if nothing else uses them; there is no recycle bin, so it is no substitute for backups.

## Limitations

- A single library owner; the shelf has no separate users or access control.
- Kindle requires a prepared device with shell access; DRM is not circumvented.
- Compatibility with every Kindle model and every CrossPoint version is not guaranteed.
- Large EPUB and KFX files take memory and time. HTTP uploads are limited to 256 MiB;
  the total unpacked size of an EPUB is limited to 512 MiB and 10,000 files.
- Metadata lookup relies on external catalogs; their availability and completeness are not
  guaranteed.
- Passwords are not encrypted over HTTP. Outside a trusted network use VPN/HTTPS.

## How it works and how to test it

`bookhub/` is the application and the reader adapters. `myshelf/static/` is the web interface.
Web, sync and worker are three processes of a single image sharing one database. KFX
processing and waiting for AI are moved to the worker so they don't hold up regular sync
requests. `reader-sync/app/` contains compatibility imports for the legacy protocol tests;
you don't need to run a separate server from that directory.

```sh
docker build --target tests -t bookhub-public:tests .
docker run --rm --network none -v "$PWD:/src:ro" bookhub-public:tests
```

The tests use temporary data and never touch a live library. Two checks need your own
KFX/YJF files and are skipped without them. GitHub Actions runs the tests and a check that
the three processes work together. Automated tests don't replace verifying firmware on a
real reader.

## License

BookHub code is distributed under **GPL-3.0-only** unless a file states another license.
Full text: [LICENSE](LICENSE). Explanation: [LICENSE-INFO.md](LICENSE-INFO.md) (in Russian).
Attribution and licenses of third-party components: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The application license does not cover books uploaded by the user.
