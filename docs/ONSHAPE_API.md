# Onshape API notes

> **Verify before trusting.** These notes were written from docs, not from tested calls. Confirm each call in the API Explorer (https://cad.onshape.com/glassworks/explorer/) or the Phase 0 spike, then update this file with what actually worked (status codes, redirect behavior, response fields).

Docs: https://onshape-public.github.io/docs/ (API intro, auth, Import & Export guide, extensions).

## Auth

- API keys from Onshape → My account → Developer → API keys. Give them read-only scope.
- Simplest: **HTTP Basic auth** with `access_key:secret_key`. Onshape also supports HMAC-signed requests; only switch if Basic fails.
- Base URL: `https://cad.onshape.com/api` (versioned paths like `/api/v10/...` also exist; pick one and keep it consistent).
- Rate limits: https://onshape-public.github.io/docs/auth/limits/. Each send is about 2–4 calls.

## Part name

```
GET /api/parts/d/{did}/{wvm}/{wvmid}/e/{eid}?configuration={enc}
→ [ { "partId": "JHD", "name": "Bracket", ... }, ... ]
```

Pick the entry whose `partId` matches. Fall back to the part ID if it isn't found.

Document name, for the export subfolder: `GET /api/documents/{did}` → `name`.

## STL export (synchronous)

Part Studio endpoint, filtered to one part:

```
GET /api/partstudios/d/{did}/{wvm}/{wvmid}/e/{eid}/stl
    ?partIds={pid}&mode=binary&units=millimeter&grouping=true&configuration={enc}
Accept: application/vnd.onshape.v1+octet-stream   (or application/octet-stream)
```

- Expect a **307 redirect** to a download URL. Follow it **with auth still attached** (httpx `follow_redirects=True`; check that the auth header survives the redirect, which it should if the host is still `*.onshape.com`).
- Useful params: `angleTolerance`, `chordTolerance`, `minFacetWidth` (resolution presets, Phase 4).
- Alternative per-part path: `/api/parts/d/{did}/{wvm}/{wvmid}/e/{eid}/partid/{pid}/stl`. URL-encode `pid`.

## 3MF / STEP (async translation, Phase 4)

```
POST /api/partstudios/d/{did}/{wv}/{wvid}/e/{eid}/translations
  { "formatName": "STEP", "partIds": "{pid}", "storeInDocument": false, "configuration": "..." }
→ { "id": "<translationId>", "requestState": "ACTIVE", ... }

GET /api/translations/{translationId}        # poll until requestState == DONE (or FAILED)
→ { "resultExternalDataIds": ["<xid>"], ... }

GET /api/documents/d/{did}/externaldata/{xid}  # the file bytes
```

Check the Explorer's list of valid `formatName` values and confirm 3MF is among them (the UI can export 3MF).

## Configuration strings

The extension's `{$configuration}` gives the encoded configuration of the active element, e.g. `Length%3D50+mm;Holes%3Dtrue`. Pass it through as the `configuration` query param. Be careful not to double-encode: log exactly what was sent and compare with the Explorer.

## Findings log

Add dated entries here as you learn things:

- **2026-09-30, Phase 0 spike (`scripts/spike_export.py`), a test document:**
  - **Basic auth works** (`access:secret`) on unversioned `/api/...` paths. No HMAC needed.
  - `GET /api/documents/{did}` → 200 JSON, `name` = `"test"`.
  - `GET /api/parts/d/{did}/w/{wid}/e/{eid}` → 200 JSON list; entries have `partId` (e.g. `"JHD"`) and `name` (`"Part 1"`).
  - `GET /api/partstudios/d/{did}/w/{wid}/e/{eid}/stl?partIds=JHD&mode=binary&units=millimeter&grouping=true` with `Accept: application/vnd.onshape.v1+octet-stream` → **307**, `Location` = `https://cad-usw2.onshape.com/modelexport?...` (**a different host**, pinned to a microversion).
  - **httpx strips `Authorization` on cross-host redirects** (by design), so `follow_redirects=True` gives **401** from `cad-usw2`. Fix: request with `follow_redirects=False`, check that `Location` is `https://<sub>.onshape.com`, then GET it with auth attached. Never re-send auth to any other host.
  - The redirected GET (`Accept: application/octet-stream`) → 200 `application/sla`, a valid binary STL (684 bytes, 12 triangles, 68.61 × 40.38 × 6.35 mm, so millimeters are honored).
- **2026-09-30, extension spike (`scripts/spike_listener.py`), Tree context menu / Selected part / Open in new window, Action URL `http://localhost:8765/open?slicer=orca&d={$documentId}&wv={$workspaceOrVersion}&wvid={$workspaceOrVersionId}&e={$elementId}&p={$partId}&c={$configuration}`:**
  - `{$documentId}`, `{$workspaceOrVersion}` (`w`), `{$workspaceOrVersionId}`, `{$elementId}` and `{$partId}` (`JHD`) all substitute correctly.
  - **`{$configuration}` came through literally** as `{$configuration}` for a Part Studio with no configurations. The handler must treat a literal `{$configuration}` as "default config", not reject it.
  - With a configured Part Studio, the browser sent `c=List_zrSB7lcyzQWXqq%3D_1`, which decodes to `List_zrSB7lcyzQWXqq=_1` (`<parameterId>=<optionId>`, not the display name). Passing that decoded string as the `configuration` query param (httpx encodes it back to `%3D`) exported the configured geometry: 25.4 × 25.4 mm versus 50.8 × 25.4 mm by default. So: **decode once when parsing, and let httpx encode once when sending.** The redirect `Location` already carries the resolved configuration.
  - **Onshape appends its own params** after ours: `companyId`, `sessionCompanyId`, `server` (`https://cad.onshape.com`), `userId`, `clientId`, `locale`, `theme`. Validation must allow (and ignore) exactly these. `server` must never be used as the API host.
  - Request headers on the navigation: `Sec-Fetch-Site: cross-site`, `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, `Sec-Fetch-User: ?1`. **No `Referer` and no `Origin`** (https → http downgrade), so we can't verify that the request came from Onshape.
  - The browser also requests `/favicon.ico`.
- **2026-09-30, error shapes and key check:**
  - `GET /api/users/sessioninfo` with valid keys → 200 JSON (`email`, `firstName`, `company`, ...). With **invalid keys → 204 No Content** (treated as anonymous, not 401). `doctor` uses this: only a 200 with JSON means the keys work.
  - `GET /api/documents/<nonexistent id>` → 404 JSON `{"message": "Not found.", "status": 404, ...}`.
  - STL export with a nonexistent `partIds` → 307 as usual, then **400** JSON from the `modelexport` host.
- **2026-09-30, face normals for "this face down" (D-14):**
  - `GET /api/partstudios/d/{did}/{wvm}/{wvmid}/e/{eid}/bodydetails[?configuration=]` → 200 `{bodies: [{id (= partId, e.g. "JHD"), type, faces, edges, vertices}], documentMicroversion}`.
  - Each face: `{id (e.g. "JHG"), orientation: bool, area (m²), loops, box {minCorner, maxCorner} (m), surface}`. Planar faces have `surface = {type: "plane", origin: [x,y,z] (m), normal: [x,y,z]}`. The test block's 6 faces were all `plane`.
  - **`surface.normal` is the plane's normal, not the face's.** Outward face normal = `normal` if `orientation` is true, else `−normal`. Checked on the test block: bottom face `JHG` at z = 0 has plane normal +Z with `orientation: false` → outward −Z; top face `JHK` at z = 6.35 mm has +Z with `orientation: true` → outward +Z; `JHO` at y = −12.7 mm has +Y with `false` → −Y.
  - Units are metres. Non-planar faces carry other `surface.type` values; "face down" only makes sense for planes.
- **2026-09-30, Element right panel spike (`scripts/spike_panel.py`)**, Context Part Studio, Action URL `http://localhost:8766/panel?d={$documentId}&wv={$workspaceOrVersion}&wvid={$workspaceOrVersionId}&e={$elementId}`:
  - Onshape accepts an `http://localhost` panel URL, loads it in an iframe (`Sec-Fetch-Dest: iframe`, `Sec-Fetch-Site: cross-site`), and appends the same extra params as for menu actions (`companyId`, `sessionCompanyId`, `server=https://cad.onshape.com`, `userId`, `clientId`, `locale`, `theme`).
  - After our `applicationInit` (`{documentId, workspaceId, elementId, messageName}` posted to `https://cad.onshape.com`), Onshape posts `{"messageName": "SELECTION", "selections": [...]}` from origin `https://cad.onshape.com` on every change. **Each message carries the whole current selection**; a deselect sends `[]`.
  - Selection items seen: a face `{"selectionType": "ENTITY", "entityType": "FACE", "selectionId": "JHK", "workspaceMicroversionId": "…"}` and a part `{"selectionType": "BODY", "selectionId": "JHD", "workspaceMicroversionId": "…"}`. `selectionId` of a BODY is the part ID; of a FACE, the face ID from `bodydetails`.
  - A face alone doesn't say which part it's on; look it up in `bodydetails` (the body whose `faces` contain it).
  - The panel URL must include `c={$configuration}` too (the spike's didn't), so configured parts export in the configuration shown.

## OAuth 2 (per-user sign-in, D-23)

From https://onshape-public.github.io/docs/auth/oauth/ (2026-10-01):

- Authorize: `GET https://oauth.onshape.com/oauth/authorize` with `response_type=code`,
  `client_id`, `redirect_uri` (must be registered on the OAuth app), `state`.
- Token: `POST https://oauth.onshape.com/oauth/token`, form body, client credentials in
  the body (`client_id`, `client_secret`). Code exchange: `grant_type=authorization_code`,
  `code`, and the same `redirect_uri`. Refresh: `grant_type=refresh_token`,
  `refresh_token`; the answer has a new access token **and a new refresh token** (store
  both). Access tokens last about 60 minutes; refresh tokens last as long as the grant.
- API calls: `Authorization: Bearer <access_token>`; a 401 means refresh and retry.
- Users can revoke an app's access at any time; the next refresh then fails.
- `oauth.onshape.com` sends `Content-Security-Policy: frame-ancestors 'self' *.onshape.com …`
  and no `Cross-Origin-Opener-Policy`; `cad.onshape.com` sends no COOP either (checked
  2026-10-01). So sign-in runs in a pop-up, which can message its opener.
- Extensions can't pass the user's identity: the Action URL placeholders are document,
  element, part, configuration and company ids only. os2slice learns who signed in from
  `GET /api/users/sessioninfo` (`id`: 24 hex characters; `name`).
- Docs: "3rd-party cookies must be enabled in the browser for Onshape apps to work
  correctly"; os2slice's panel cookie is `Partitioned` to work without them.
- **2026-10-01, live (Josh, Firefox):** sign-in from the panel worked end to end with the
  app's client ID and secret and redirect URI `https://print.example.duckdns.org:8443/auth/callback`:
  pop-up, authorize, panel reload as the user, preview, both Bambu Studio links, and a print
  queued with the user's own token (log: `print job … started by onshape:<user id>`). The code
  exchange as documented (form body, client credentials in the body) was accepted.

