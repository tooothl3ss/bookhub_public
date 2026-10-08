# MVP API

One configured user. JSON for position/inventory requests; raw file bodies for uploads.
API authentication: `Authorization: Bearer <SYNC_API_KEY>`. HTTP Basic with
`SYNC_USER:SYNC_PASSWORD` is accepted too; its writes need `X-Reader-Sync: 1`.
KOSync uses `x-auth-user: <SYNC_USER>` and `x-auth-key: <MD5(SYNC_PASSWORD)>`.
Errors are `{"error": "..."}` with the status given below.

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Public process health |
| GET | `/api/library` | Books, documents, jobs, devices, application acknowledgements |
| POST | `/api/kindle/inventory` | Register/update Kindle files |
| GET | `/api/books/{key}/manifest` | Upload requirement, map status, artifact version |
| PUT | `/api/books/{key}/bundle` | Raw tar of KFX/content containers |
| PUT | `/api/books/{key}/epub` | Raw EPUB for the selected Kindle book |
| POST | `/api/books/{key}/generate-epub` | Build the EPUB from the uploaded KFX |
| GET | `/api/books/{key}/map` | Validated compact Kindle map |
| GET | `/api/books/{key}/file/{kfx,epub,map}` | Original KFX, EPUB or full JSON map |
| GET | `/api/books/{key}/meta` | Title, authors, language from KFX metadata |
| GET | `/api/books/{key}/cover` | Cover image embedded in the KFX (404 if none) |
| POST | `/api/books/{key}/rebuild` | Requeue map generation |
| GET / PUT | `/api/books/{key}/position` | Read / advance canonical PID |
| POST | `/api/books/{key}/reset` | Forget the position and start a new reading cycle |
| POST | `/api/books/{key}/applied` | Confirm native Kindle readback |
| POST | `/api/documents/{document}/bind` | Attach an opaque KOSync document ID to a book |
| DELETE | `/api/documents/{document}` | Forget a document that is not attached to a book |
| GET | `/users/auth` | KOSync authentication check |
| POST | `/users/create` | Validate configured account; no public registration |
| PUT | `/syncs/progress` | Reader XPath upload |
| GET | `/syncs/progress/{document}` | Current EPUB position of the attached book |

Inventory example:

```json
{"device_id":"kindle-main","books":[{"key":"CDEKEY","title":"Book","path":"/mnt/us/documents/book.kfx","size":12345,"fingerprint":"64-lowercase-hex-SHA256"}]}
```

The list is the device's whole library: a registered book missing from it is marked as
removed from the Kindle (`present: 0`), its server data stays. An empty list changes nothing.
The answer holds the manifest of every listed book.

Fingerprint algorithm: sort included relative file paths, hash each file with SHA256,
join `hex + two spaces + relative filename + newline`, then SHA256 this UTF-8 string.
The bundle preserves the main file and sidecar paths relative to the main KFX parent.
Send `X-Source-Fingerprint` matching the inventory (409 otherwise). `.yjf`, symlinks and
traversal paths are rejected. Limits: 256 MiB upload, 512 MiB expanded, 10,000 archive entries.

EPUB upload answers `{"status":"queued","sha256":...}`: the file is now the book's EPUB and
a job marks it ready. Two checks run before that, and a refused file leaves the previous EPUB
in place: 400 `EPUB не читается: ...` for a file that is not a readable EPUB (broken archive,
over the expanded limits above, DRM), and 400 `EPUB не совпадает с книгой Kindle: ...` when
the map is ready and the file is another text: positions sampled over the whole map are
translated to the EPUB and more than half must map; the message says how many did. A book
without text (manga) needs a textless EPUB with one page per Kindle section instead.
Without a ready map nothing is compared.
`generate-epub` takes an optional `{"title":...,"author":...}` and answers
`{"status":"generating"}`; 409 without an uploaded KFX or when the book already has an EPUB.
The generated EPUB is not compared with the map.

Position, as GET returns it:

```json
{"key":"CDEKEY","pid":228605,"xpath":"/body/DocFragment[6]/body/p[23]/text()[1].3","percentage":0.41,"revision":2,"updated_at":1760000000,"source":"kindle","asset_version":1,"bridge_status":"mapped","epoch":0,"start_pid":57}
```

`epoch` counts resets. `start_pid` is the first position of the book's text according to the
map, `null` until the map is ready: where to open the book when `pid` is `null`.
PID write: `{"pid":228605,"asset_version":1,"epoch":0}`. The asset version is returned by
manifest; `epoch` is the one last read from GET and counts as 0 when omitted. The answer is
the position (without `start_pid`) plus `accepted`.
A lower/equal PID returns 200 with `accepted:false` and `reason:behind|unchanged`; a write
from another epoch returns `reason:reset`. (`stale` exists only between the server's own
EPUB and Kindle positions and is not returned to clients.)
A newer PID updates the single canonical position, revision and server timestamp atomically.
Artifact-version mismatch returns 409. There is no automatic force/backward route.
Acknowledgement: `{"device_id":"kindle-main","pid":228605,"revision":2}`; 409 when the pair
is no longer the current position.

Reset takes no body and answers like GET: `pid:null`, `epoch` one higher, `source:"reset"`.
It also clears the progress of the attached documents, the acknowledgements and, for a book
on the shelf, the position of its EPUB. A client that sees a new `epoch` drops what it had
queued and sends the new value with its next write.

Bind body: `{"key":"CDEKEY"}` for a Kindle book, or `{"book_id":12,"sha256":"..."}` for an
EPUB of the library (`sha256` may be omitted: the book's main EPUB). A document named by the
MD5 of the EPUB's file name or by KOReader's partial MD5 of its content is attached when the
EPUB is added. DELETE removes a document with the progress retained for it: 404 for an
unknown one, 409 for one attached to a Kindle book.
KOSync PUT body:

```json
{"document":"opaque-id","progress":"/body/DocFragment[6]/body/span[1]/p[23]/text()[1].3","percentage":0.002,"device":"CrossPoint","device_id":"x3"}
```

`percentage` is in 0..1. The answer is `{"status":"OK"}` plus the outcome: `accepted` and
`reason` as above, or `pending:true` with `error` for an unknown/unready document, whose
progress is retained. Rebinding or finishing an asset job retries the pending position.
GET returns `progress` (the XPath of the book's current position in that EPUB, wherever it
came from: this or another reader, or the Kindle through the map), `percentage`, `timestamp`
and `device`; 404 until the document is attached and a position exists. GET timestamps stay
stable across reads and duplicate/behind updates. The backend never uses a percentage
fallback to guess a PID.

SQLite keeps the canonical current point and the latest device input/acknowledgement,
not a position history. Automatic resolution is the furthest position within one epoch;
offline deliberate backward reading is not supported by this policy.

Jobs are persistent and processed by one separate worker. On worker restart, interrupted
`running` jobs are requeued. Uploaded books and EPUBs are immutable by content hash.
