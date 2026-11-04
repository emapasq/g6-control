# How to: use the MCP server with an AI assistant

A step-by-step guide to let Claude Desktop (or Codex) talk to your G6: read and change patches, run the drum machine,
and create new patches from a description such as *"a Gilmour-style lead for my Strat"*.
For the full list of tools and safety rules see [`mcp-server/README.md`](../mcp-server/README.md).

**Time needed:** about 15 minutes.

## What you need

- A Zoom G6 with **firmware 2.00** (the only version tested), connected over USB and switched on.
- **Python 3.10 or newer.** Check in a terminal with `python3 --version`.
  The `python3` that comes with macOS is often **3.9**, which is too old: install a newer one from
  [python.org](https://www.python.org/downloads/) or with `brew install python`, then open a new Terminal window.
- **Claude Desktop** (or Codex).

## 1. Download the project

On the GitHub page, click **Code → Download ZIP** and unzip it, or run
`git clone https://github.com/emapasq/g6-control.git`.

Open Terminal and go to the `mcp-server` folder: type `cd ` (with a space), drag the `mcp-server` folder into the
Terminal window, and press Enter.

## 2. Install the server

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

This creates a private Python environment in `.venv` and installs the server there. It does not change the rest of
your system. On Windows use `py -m venv .venv` and `.venv\Scripts\pip install -e .`.

## 3. Copy the path of the server

```bash
echo "$(pwd)/.venv/bin/g6-mcp"
```

Copy the line it prints (for example `/Users/you/Downloads/g6-control-main/mcp-server/.venv/bin/g6-mcp`).
You need the **full path**: a path starting with `~` or `./` does not work in the configuration.
On Windows the file is `.venv\Scripts\g6-mcp.exe`.

If you move or rename the project folder later, the path changes: repeat steps 2 and 3.

## 4. Register it in Claude Desktop

1. In Claude Desktop open **Settings → Developer → Edit Config**. This opens `claude_desktop_config.json`.
2. Add a `zoom-g6` entry under `mcpServers`, using the path from step 3:

```json
{
  "mcpServers": {
    "zoom-g6": {
      "command": "/Users/you/Downloads/g6-control-main/mcp-server/.venv/bin/g6-mcp"
    }
  }
}
```

If the file already contains other settings, do **not** replace them: add `"mcpServers"` next to them (separated by
a comma), or add `"zoom-g6"` inside the existing `"mcpServers"`. A missing comma or brace stops all servers from
loading.

3. Save the file and **quit Claude Desktop completely** (Claude → Quit, or `Cmd+Q`; closing the window is not
   enough), then open it again.

Optional: add `"env": { "G6_BACKUP_DIR": "/Users/you/g6-backups" }` next to `"command"` to choose where overwritten
patches are backed up (default `~/.g6-control/backups`).

**Codex** instead: add to `~/.codex/config.toml`

```toml
[mcp_servers.zoom-g6]
command = "/Users/you/Downloads/g6-control-main/mcp-server/.venv/bin/g6-mcp"
```

## 5. First test

In a new chat, ask:

> Which patch is active on my G6?

The assistant asks permission to use the `patch_status` tool: allow it. It answers with the slot, the patch name and
its effect blocks. Then try:

> Start the drum machine at 90 BPM, then stop it.

## 6. Generate a patch

Describe what you want, and say which guitar you play:

> Make a clean, shimmering ambient patch for my Les Paul, with delay on foot switch 2, in a free slot.

The assistant reads the patch format guide, picks a **free slot**, writes the patch and selects it so you can play.
Then refine it in plain words ("less reverb", "more gain") and ask it to save the final version.

- It never overwrites an occupied slot unless you ask, and it backs up the old patch first.
- Live tweaks are **not saved** until the assistant writes the patch again.
- If a block stays empty, the patch is over the G6's processing limit: ask for a lighter effect.

## Troubleshooting

| Problem | What to do |
|---|---|
| The assistant has no G6 tools | Check the path in the config (full path, file exists), check the JSON is valid, and quit/reopen Claude Desktop. In **Settings → Developer** the `zoom-g6` server should be listed as running; its log is in `~/Library/Logs/Claude/` (`mcp-server-zoom-g6.log`). |
| `pip install` fails, or *requires a different Python* | Your Python is older than 3.10: see *What you need*, delete the `.venv` folder and repeat step 2. |
| *G6 not found … Nothing sent* | The G6 is off or not connected over USB, or the cable carries power only. Reconnect and ask again. |
| *MIDI port busy* | Another request is still running; wait and ask again. |
| Odd or mismatched replies | Avoid sending from Guitar Lab, the web app and the assistant at the same moment. |

The server only sends commands listed as verified in [`protocol/COMMANDS.md`](../protocol/COMMANDS.md), and asks
for your confirmation before risky actions (WAV load, Looper play, Looper POSITION).
