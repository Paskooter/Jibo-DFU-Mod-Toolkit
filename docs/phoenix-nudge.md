# Optional Phoenix connection path

The toolkit finishes mode and Wi-Fi setup on its own. It does **not** connect
Jibo to Phoenix or enroll him. After a successful int-developer mode and Wi-Fi
setup, an optional card explains the separate jibo.io console steps, without
another confirmation or popup.

- The card appears only after **Set mode and configure Wi-Fi** completes
  successfully (including when the settings are already current), with a
  reported `int-developer` mode.
- It does not appear for `developer`, normal or OOBE mode, a missing mode,
  a failed or incomplete result, cancellation at any prompt, or separate
  mode-only or Wi-Fi-only edits.
- **Not now** removes only the card, leaving the result and **Back to actions**
  available. Later automatic cards stay hidden, including after restarting the
  toolkit. Opening the guide in a browser also hides later automatic cards.
- A small **Connect to Phoenix (jibo.io)** link in the sidebar opens the same
  guidance at any time, even after dismissal and without a robot attached. It
  is disabled while a robot operation is running.
- USB FLOW, USB READINESS, BACKUPS and the Actions list keep their structure.
  There are no launch popups or repeated reminders. The TUI, CLI, device backend
  and write/confirmation flows are unchanged.

## Wording

Sidebar link:

> Connect to Phoenix (jibo.io)

Guidance page title:

> Bring your Jibo online with Phoenix

Card title and introduction:

> Next: connect him to Phoenix
>
> Optional. Phoenix setup happens at jibo.io, not in this toolkit.

The sidebar guidance page adds this reminder between the introduction and
the steps; the completion card does not:

> He needs int-developer mode and your Wi-Fi first. Use Set mode and configure Wi-Fi here if he isn't set up yet.

The same three steps appear on the card and the guidance page:

> 1. Restart Jibo normally, out of recovery mode.
> 2. Note the IP on his boot screen (or in your router's device list).
> 3. At jibo.io, sign in and choose Robots → Add a Jibo. Run the one-line command it gives you; it asks for his IP.

Card buttons: **Open jibo.io** and **Not now**. The page keeps a separate
**Back to actions** button. The browser opens exactly `https://jibo.io/guide`,
with no robot IP, identifier or tracking parameters.

Status messages, shown only if a local step fails:

> Could not open a browser. You can open https://jibo.io/guide yourself.
>
> Phoenix guidance hidden for this session; the local preference could not be saved.

## Offline use and privacy

Showing the card, opening the guidance page, checking USB, and running setup do
not open a browser or contact jibo.io. Only an **Open jibo.io** click opens the
system browser. There is no availability check, telemetry, enrollment request
or automatic command. The one-line enrollment command stays a separate step in
the jibo.io console.

The GUI stores only `{"phoenix_dismissed": true}` in
`Jibo-Backups/.phoenix-guidance.json`, inside the backup folder (normally
`~/Jibo-Backups`). Nothing is written on first launch. The file holds no Wi-Fi
name, password, robot identity, IP address or account information. A missing or
damaged file never blocks the toolkit; if the folder is read-only, the card stays
hidden for that session. Delete the file to show the automatic card again; the
sidebar link is always available.

## Screenshots

### Sidebar link

![Sidebar link to the optional Phoenix guide](images/gui/phoenix-entry.png)

### Guidance page

![Optional Phoenix connection guidance](images/gui/phoenix-guidance.png)

### After int-developer mode and Wi-Fi setup

![Phoenix next-step card after int-developer mode and Wi-Fi setup](images/gui/phoenix-complete.png)

See the [screenshot notes](images/gui/README.md) for example-data details and
capture instructions.
