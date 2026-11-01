# G6 Control

Tools to control a **Zoom G6** multi-effects processor from a computer over USB MIDI:

- **`webapp/`** — a browser control panel (Chrome, Web MIDI): patch selection, drum machine start/stop,
  tempo, pattern, volume and count, SD card information, loading WAV files into the Looper, a gated Looper play/stop and
  the Looper settings (volume, STOP MODE, POSITION).
- **`mcp-server/`** — an MCP server that lets an AI assistant control the G6: stored patches (read, write with backup),
  live editing of the active patch, drum machine, tuner, SD card information, WAV loading, a gated Looper play/stop and
  the Looper settings. Ask the assistant for a patch in the style you want and it builds it and writes it to a free
  slot (see [`mcp-server/README.md`](mcp-server/README.md#using-it-to-generate-patches)).
- **`protocol/`** — the list of known commands and shared test frames.

> **Unofficial project.** Not affiliated with, endorsed by or supported by Zoom Corporation.
> "Zoom" and "G6" are used only to describe compatibility. Verified on firmware 2.00 only (see *Compatibility*).
> Use at your own risk: sending unknown messages can freeze the device (see `protocol/COMMANDS.md`, *Dangerous*).

## AI-generated code

The code, the tests and the documentation in this repository were written by AI assistants — mainly **Claude**
(Anthropic), with contributions from **OpenAI Codex** — following the maintainer's instructions. The maintainer did not
write the code by hand: he described what he wanted, decided what to build and what to leave out, and ran every test on
a real G6, checking the results on the device display and by ear. Every command the tools send is listed in
[`protocol/COMMANDS.md`](protocol/COMMANDS.md) together with the tests that verified it.

What this means for you:

- It works on the maintainer's G6 (firmware 2.00) for the tests described in [`protocol/COMMANDS.md`](protocol/COMMANDS.md),
  and its limits are documented there and in the READMEs.
- The code has not had a line-by-line human review. The safety rules (allowlist, single sends, confirmations) were
  specified by the maintainer and are covered by automated tests, but tests are not a guarantee.
- Read the safety notes before using it, and treat it as you would any unreviewed tool that talks to your hardware.

## Compatibility

Everything in this repository — the command list, the test frames, the web app and the MCP server — was verified on a
G6 running **firmware 2.00**, the version installed on the development unit (read from the device).
Other firmware versions have not been tested: message formats, the installed effects and their parameter ranges may
differ. If your G6 runs another version, check the version shown by the device or by Guitar Lab before using these tools, and treat
everything as unverified.

## Safety principles

- Only messages listed as verified in [`protocol/COMMANDS.md`](protocol/COMMANDS.md) are sent; both the web app and the
  MCP server enforce an allowlist.
- Setters are sent once: no automatic retries.
- Live changes are not saved to patches. Stored patches are written only by the MCP server, never over an occupied slot
  without explicit permission and a backup.
- The playback state of the drum machine cannot be read, and is shown as unknown.
- A WAV load is sent only after an explicit confirmation that the Looper is stopped and holds nothing to keep, by file
  name, once.
- Looper play is gated: only after a WAV load by the same client, and a manual confirmation on the display that the
  WAV is loaded and the Looper stopped; it is sent once and then disabled. Stop is available only after that play.
  Nothing records, overdubs or deletes Looper content.
- Changing the Looper POSITION stops and resets the Looper: it is sent only after an explicit confirmation that the
  Looper holds nothing to keep, never when the value already matches, and once.

## Web app quick start

```bash
cd webapp
docker compose up -d
```

Open <http://localhost:8616> in Chrome and click **Connect**. Details in [`webapp/README.md`](webapp/README.md).

## MCP server quick start

```bash
cd mcp-server
python3 -m venv .venv && .venv/bin/pip install -e .
```

Then register `.venv/bin/g6-mcp` in your assistant: see [`mcp-server/README.md`](mcp-server/README.md).

## Tests

```bash
cd webapp && npm test                                 # Node 20+, no dependencies
cd mcp-server && python3 -m unittest discover -s tests  # Python 3.10+
```

Both suites use the same frames in [`protocol/test-vectors.json`](protocol/test-vectors.json).

## Acknowledgements

Thanks to [zoom-zt2](https://github.com/mungewell/zoom-zt2) by mungewell, a related open-source project
documenting Zoom effect pedals and their files, for its work on Zoom effect identifiers and module files.

## License

[MIT](LICENSE)
