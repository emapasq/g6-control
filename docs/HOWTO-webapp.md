# How to: use the web app

A step-by-step guide for first-time users. You do not need to know Docker, MIDI or programming.
For the full feature list see [`webapp/README.md`](../webapp/README.md).

**Time needed:** about 10 minutes.

## What you need

- A Zoom G6 with **firmware 2.00** (the only version tested), and a USB cable that carries data (some charging-only
  cables do not).
- A Mac or PC with **Google Chrome** (or Microsoft Edge). Safari does not support Web MIDI.
- Nothing else is required. Docker is optional (see step 3).

## 1. Connect the G6

1. Plug the G6 into the computer with the USB cable and switch it on.
2. Close Zoom Guitar Lab if it is open (it can stay open, but for a first test it is easier without it).

## 2. Download the project

Either:

- on the GitHub page, click **Code → Download ZIP** and unzip it (you get a folder called `g6-control-main`), or
- in a terminal: `git clone https://github.com/emapasq/g6-control.git`

The commands below assume you are in the project folder. On a Mac: open **Terminal**, type `cd ` (with a space),
drag the project folder into the Terminal window, and press Enter.

## 3. Start the page

The web app is a set of static files that must be served from `localhost` (Chrome only allows MIDI on
`localhost` or HTTPS pages; opening `index.html` directly with a double click does **not** work).
Choose **one** option.

### Option A — Python (no install on most Macs)

```bash
cd webapp/public
python3 -m http.server 8616 --bind 127.0.0.1
```

Leave this Terminal window open while you use the page; press `Ctrl+C` to stop it.
If macOS asks to install the *command line developer tools*, accept and run the command again.
On Windows, use `py -m http.server 8616 --bind 127.0.0.1` (install Python from python.org if needed).

### Option B — Docker

If you already use Docker (Docker Desktop must be running):

```bash
cd webapp
docker compose up -d
```

It keeps running in the background and restarts with Docker. Stop it with `docker compose down`.

## 4. Open the page and connect

1. In Chrome, open <http://localhost:8616>.
2. Click **Connect**.
3. Chrome asks whether the page may *control and reprogram your MIDI devices*: click **Allow**.
   (This permission is needed for SysEx, the messages the G6 understands.)
4. The status changes to **Connected** and the active patch name appears.

From now on the page connects by itself when you open it.

## 5. First test

- Press **→** (or the next-patch button): the G6 changes patch.
- In **Rhythm**, type a BPM, press Enter, then **Start**: the drum machine plays. **Stop** stops it.
- **Load patch list** reads the 240 patch names. The G6 shows *PC Mode* for a few seconds: this is normal.

The page cannot read whether the drum machine is playing, so it shows the playback state as *unknown*.

## Troubleshooting

| Problem | What to do |
|---|---|
| The page is blank or nothing happens on **Connect** | Make sure the address is `http://localhost:8616` and not a `file://…` path. Use Chrome or Edge. |
| *G6 not found* | Is the G6 switched on? Try another USB cable or port. Unplug and replug, then click **Connect** again. On Windows, install the Zoom G6 driver from zoom.co.jp if the device does not appear. |
| No permission prompt, or you clicked **Block** | Click the icon left of the address bar → **Site settings** → **MIDI devices** → **Allow**, then reload. |
| *Address already in use* when starting | Another program uses port 8616. Use another number (e.g. `8617`) in the command and in the address. |
| Docker: *Cannot connect to the Docker daemon* | Start Docker Desktop and run the command again, or use Option A. |
| Commands seem to go to the wrong place | Avoid sending from Guitar Lab, the MCP server and the page at the same moment. |

Before using the Looper and WAV loading, read the safety notes in [`webapp/README.md`](../webapp/README.md#features):
those actions ask for explicit confirmations on purpose.
