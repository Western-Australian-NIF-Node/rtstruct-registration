# register-rt-collections

XNAT Container Service command that registers archived RTSTRUCT / SEG scans as
OHIF ROI Collections, triggered automatically by the Event Service.

## Why

The XNAT OHIF viewer only lists contours that exist as `icr:roiCollectionData`
assessors. An RTSTRUCT uploaded through the normal DICOM uploader is archived
as a scan and never becomes a collection, so the viewer shows nothing. This
command closes that gap using the *archived* file, which carries the
site-hashed UIDs that match the archived CT.

## Order independence

The subscription fires on every **Scan Created** event, with no payload filter.
Each run retries every unregistered RTSTRUCT/SEG in that scan's session:

* RTSTRUCT arrives after the CT: its own event registers it.
* RTSTRUCT arrives before the CT: its event fails with "Missing dependencies"
  (the referenced series is not there yet); the CT's event registers it.
* Already registered objects are skipped by SOP Instance UID, so nothing is
  ever duplicated.

## Build and publish

Images are built by GitHub Actions
([.github/workflows/publish-image.yml](.github/workflows/publish-image.yml))
and published to the organisation's container registry as
`ghcr.io/western-australian-nif-node/register-rt-collections`.

### One-time setup

1. Push this repository to `Western-Australian-NIF-Node/rtstruct-registration`.
2. After the first release (below), open the organisation's **Packages** tab →
   `register-rt-collections` → **Package settings** and set visibility to
   **Public**, so XNAT can pull without credentials. If the option is greyed
   out, an org owner must allow public packages under **Settings → Packages**.

### Releasing a version

1. Set `"version"` and the tag in `"image"` in `command.json` to the new
   version, e.g. `1.0.1`, and commit.
2. Tag and push:

   ```bash
   git tag v1.0.1
   git push origin v1.0.1
   ```

The workflow publishes the tags `1.0.1`, `1.0` and `latest`. Keep the git tag
and `command.json` in step: XNAT pulls exactly the image `command.json` names.

### Local test build

```bash
docker build -t register-rt-collections:dev .
```

## Install in XNAT (once per server)

1. **Administer → Plugin Settings → Container Service → Images & Commands**.
   Pull `ghcr.io/western-australian-nif-node/register-rt-collections:<version>`
   (no image host credentials needed, the package is public), then
   **Add New Command** and paste `command.json`.
2. Same page: **enable** the command site-wide.
3. **Project → Project Settings → Configure Commands**: enable
   `Register RT collections (scan event)` for each project, or rely on a
   site-wide subscription.
4. Create a service account with create rights on ROI collections in the
   projects. Collections are attributed to the action user.

## Event subscription

**Administer → Event Service → Add New Event Subscription**:

| Field | Value |
|---|---|
| Label | `register-rt-collections` |
| Select Event | Scan – Created |
| Select Project | tick *Apply to All Projects*, or pick projects |
| Select Action | `Register RT collections (scan event)` |
| Attributes | leave empty |
| Event Payload Filter | leave empty (every scan must trigger, see above) |
| Perform Action As | the service account (do not use the event initiator) |
| Status | Enabled |

The second wrapper, `Register RT collections (session)`, appears in a
session's **Run containers** menu for one-off manual runs and for sessions
archived before the subscription existed.

## Manual run

```
XNAT_HOST=https://xnat.example.org XNAT_USER=... XNAT_PASS=... \
  python register_rt_collections.py --project P --session XNAT_E00015 --dry-run -v
```

## Verifying

Administer → Event Service → History shows each firing. The container log
(Administer → Container Service → History) prints one line per RTSTRUCT:
`registered`, `already registered`, or `not registered yet` with the server's
reason.
