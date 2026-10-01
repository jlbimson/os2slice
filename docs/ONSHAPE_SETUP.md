# Onshape setup

os2slice appears in Onshape through an OAuth application with two extensions. Replace `<host>` below with the add-on's `hosts` value, e.g. `print.example.duckdns.org:8443`.

## 1. OAuth application

Onshape → account menu → **My account** → **Developer** → **OAuth applications** → **Create new OAuth application**.

| Field | Value |
|---|---|
| Name | `os2slice` |
| Primary format | a reverse domain you own, e.g. `com.example.os2slice` |
| Summary | e.g. `Slice and queue Onshape parts on the office Bambu printers through BamBuddy.` |
| Redirect URLs | `https://<host>/auth/callback` |
| Type | Integrated Cloud App |
| Permissions | **Application can read your documents** only |

Copy the **client secret** from the window shown right after creation: Onshape shows it only once. If you lose it, regenerate it on the app's **Keys and secret** tab, which also shows the client ID.

In the os2slice add-on's Configuration tab:

1. Set `onshape_auth: oauth`.
2. Set `onshape_oauth_client_id` and `onshape_oauth_client_secret`.
3. Save and restart.

The log's pass/fail table should show `Onshape sign-in: per user; redirect URI https://<host>/auth/callback`.

## 2. Extensions

On the app: **Extensions** → **Add extension**, twice. Placeholders are case-sensitive and use the `{$name}` form. The icon is `assets/os2slice-icon.svg`.

| Name | Location | Context | Action URL |
|---|---|---|---|
| Print | Element right panel | Part Studio | `https://<host>/panel?d={$documentId}&wv={$workspaceOrVersion}&wvid={$workspaceOrVersionId}&e={$elementId}&c={$configuration}` |
| Print with BamBuddy | Tree context menu (action type *Open in new window*) | Selected part | `https://<host>/print?d={$documentId}&wv={$workspaceOrVersion}&wvid={$workspaceOrVersionId}&e={$elementId}&p={$partId}&c={$configuration}` |

Onshape only accepts `https://` Action URLs (or `http://localhost`).

## 3. Make it appear

**For yourself:** on the app's **Details** tab, choose **Create store entry**. Fill in the form; the entry is private to you. Then find the app at https://cad.onshape.com/appstore and **Subscribe**. Refresh Onshape.

**For a company** (Professional or Enterprise plan):

1. On the app's Details tab, transfer ownership to the company.
2. A company admin opens Company settings → Developer → **Applications** → os2slice, then adds users or a team under **Add users or teams**.
3. Assigned users see the extensions after a refresh; no store entry is needed.

If you were subscribed before the transfer and now see everything twice, unsubscribe from the private store entry.

## 4. First use

Users open a Part Studio, open the **Print** panel, and click **Sign in with Onshape**. A small Onshape window asks them to authorize os2slice once, then closes. Sign-ins last 30 days. **Sign out** in the panel, or revoking the app in Onshape's account settings, removes the access.
