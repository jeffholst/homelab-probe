# JSON output schemas

Every `--json` output of the tool, the snapshot files and the webhook payload have a [JSON Schema](https://json-schema.org/) (draft 2020-12) in [`docs/schemas/`](schemas/), so a script or a dashboard can check what it receives instead of guessing. Each file is named `<document>.v<N>.schema.json` and its `$id` is its address on the `main` branch; the copy in the release you installed (the tag) is the authority for that release.

The web server (see [the web interface](web.md#the-api-apiv1unifi)) returns the same documents with two more keys, `generated_at` and `warnings`; the documents that are bare arrays come back as `{"items": [...], "generated_at", "warnings"}`. Those API responses are described in the server's `/api/v1/openapi.json` (built from the schemas below), and the schemas themselves are served at `/api/v1/schemas/{name}`.

| Command | Document | Schema |
| ------- | -------- | ------ |
| `diagnose --json` | findings, with `areas` and `summary` | [`diagnose.v1.schema.json`](schemas/diagnose.v1.schema.json) |
| `audit --json` | findings, same shape | [`audit.v1.schema.json`](schemas/audit.v1.schema.json) |
| `firewall --json` | policies, port forwards, zones, matrix, findings | [`firewall.v1.schema.json`](schemas/firewall.v1.schema.json) |
| `topology --json` | the uplink tree | [`topology.v1.schema.json`](schemas/topology.v1.schema.json) |
| `wifi --json` | radios, neighbors, channel plan | [`wifi.v1.schema.json`](schemas/wifi.v1.schema.json) |
| `wan --json` | internet state, monitoring, speedtests | [`wan.v1.schema.json`](schemas/wan.v1.schema.json) |
| `client NAME --json` | one client | [`client.v1.schema.json`](schemas/client.v1.schema.json) |
| `events --summary --json` | counts and the noisiest clients | [`events-summary.v1.schema.json`](schemas/events-summary.v1.schema.json) |
| `events --json` | the events (a bare array) | [`events.v1.schema.json`](schemas/events.v1.schema.json) |
| `query --json` | devices and clients (a bare array) | [`query-all.v1.schema.json`](schemas/query-all.v1.schema.json) |
| `query devices --json` | devices (a bare array) | [`query-devices.v1.schema.json`](schemas/query-devices.v1.schema.json) |
| `query clients --json` | clients (a bare array) | [`query-clients.v1.schema.json`](schemas/query-clients.v1.schema.json) |
| `query reservations --json` | reservations (a bare array) | [`query-reservations.v1.schema.json`](schemas/query-reservations.v1.schema.json) |
| `doctor --json` | the checks of the installation, the settings and the controller | [`doctor.v1.schema.json`](schemas/doctor.v1.schema.json) |
| `export --format json` | the file `unifi_inventory.json`: devices, clients and switches with their ports | [`export.v1.schema.json`](schemas/export.v1.schema.json) |
| `query ports --json` | switch ports (a bare array) | [`query-ports.v1.schema.json`](schemas/query-ports.v1.schema.json) |
| `query networks --json` | networks (a bare array) | [`query-networks.v1.schema.json`](schemas/query-networks.v1.schema.json) |
| `query wlans --json` | Wi-Fi networks (a bare array) | [`query-wlans.v1.schema.json`](schemas/query-wlans.v1.schema.json) |
| `new-clients --json` | clients in no group (a bare array) | [`new-clients.v1.schema.json`](schemas/new-clients.v1.schema.json) |
| `diff --json` | what changed between two inventories | [`diff.v1.schema.json`](schemas/diff.v1.schema.json) |
| `snapshot` | the saved file (`schema_version`) | [`snapshot.v1.schema.json`](schemas/snapshot.v1.schema.json) |
| `diagnose --notify` | the webhook payload | [`webhook-payload.v1.schema.json`](schemas/webhook-payload.v1.schema.json) |

## How they are versioned

- Every document that is a JSON object has a **`version`** as its first key (the snapshot file has `schema_version`). It changes only when a field is removed or renamed, or its meaning changes; fields are never renamed or reused within a version. The file name carries the same number, so version 2 would be a new file next to the old one.
- **New fields can appear without a new version**, so the schemas leave objects open (additional properties are allowed): the output of a newer tool still validates against the schema you saved. Do not write a consumer that fails on a key it does not know.
- A document that is a **bare array** (`query`, `new-clients`, `events`) cannot carry a version without changing its type, so it has none: its version is the schema file name, and a change that is not additive is listed in the [changelog](../CHANGELOG.md).
- `required` lists what every output contained across all the test variants (every command, every kind of client, filters, controllers with missing data). A value that can be unknown is typed with `null` (for example `["number", "null"]`); an object that only exists sometimes is not in `required`.
- Finding codes (`device.offline`, `audit.wifi_open`, ...) are never renamed or reused; the schemas only say a code is `area.name`, because new codes are added in minor versions. The list is in [Diagnose and audit](diagnose.md#json-output-and-finding-codes).

## Validating

```python
import json, subprocess
from jsonschema import validate   # pip install jsonschema

run = subprocess.run(["hlp", "diagnose", "--json"], capture_output=True, text=True)   # exit 1 or 2 means findings
validate(json.loads(run.stdout), json.load(open("docs/schemas/diagnose.v1.schema.json")))
```

## Keeping them true

The tests run every command against the synthetic fixture over many variants and validate the output against its schema **and against a strict copy** (`additionalProperties: false` wherever the schema does not say otherwise), so a key the code adds without declaring it fails. They also check that the files are valid draft 2020-12 schemas, that each `version` matches the constant in the code and the file name, that every command with a `--json` option has a schema, and that the schemas of the typed records (the snapshot, the diff, the topology) name exactly the keys of their `TypedDict`s. When you add a field, add it to the schema (and the `TypedDict`), and add a line to the changelog.
