# arwRunTests

A plugin for [agentRW](https://github.com/linuxrebel/agentRW). Runs your pytest
suite, reports the result in a handful of tokens instead of thousands, and tells
you whether a change broke something that used to pass.

```
/runtests                    run the suite, show a summary
/runtests baseline           record the current results
/runtests verify             run again, report what broke since
```

---

## Why it exists

A model was asked to fix one line of a Python file. It rewrote `join(*lines)`
as `join(lines)` — a single dropped character. The result:

| check | verdict |
|---|---|
| pylint | **9.57 / 10** |
| `compile()` | passed |
| actually running it | `TypeError: sequence item 0: expected str instance, tuple found` |

A linter grades style. `compile()` proves the file parses. Neither knows what
the code *does*. Only executing it distinguished a working file from a broken
one, and nothing in the toolchain was executing anything.

That is the entire case for this plugin. It is not a better linter. It is the
one check that catches what a linter structurally cannot.

---

## What it does that `pytest` alone does not

**It aggregates.** Raw pytest output on a real suite is thousands of tokens —
more than a 2048-token context window holds. This returns counts and the first
few failures, around 150 tokens. On a 7B model running in 4 GB of VRAM that is
the difference between a usable check and one that eats the whole window.

**It knows what "broke" means.** `pytest` tells you six tests are failing. It
cannot tell you that five of them were failing yesterday. A baseline turns
"failing" into "newly failing", which is the fact you actually need before
deciding to revert a change. A test that was already red is the status quo, not
a regression — treating it as one would block every change until the suite is
green.

**It never guesses.** A run killed by the timeout is reported as a timeout, not
as a failure. A program killed mid-run loses its buffered output, and calling
that "the tests failed" would be a false alarm someone might act on by throwing
away good code.

---

## What this will do

**It will:**
- Run `pytest` as a subprocess, from inside the directory you point it at
- Read the `.py` files pytest collects — which means **executing your test
  suite and everything it imports**, with whatever side effects that has
- Write `baseline.json` inside its own plugin directory when you run
  `/runtests baseline`
- Return counts and up to five failure messages to the model when the model
  calls the tool. If agentRW is pointed at a cloud model, those test names and
  assertion messages leave the machine

**It will not:**
- Modify, revert, or delete any file of yours — it has no write path outside
  its own `baseline.json`
- Decide anything. It reports facts; you or the caller decide what to do
- Install packages, change configuration, or reach the network itself
- Send your source code anywhere. Only test nodeids and failure messages are
  ever returned

The one thing worth being clear about: **running tests runs code.** Point it at
a suite you trust, the same way you would with `pytest` directly.

---

## Requirements

`pytest` only.

| | |
|---|---|
| Fedora | `sudo dnf install python3-pytest` |
| Debian/Ubuntu | `sudo apt install python3-pytest` |
| pip | `python3 -m pip install --user pytest` |

Without it, `/runtests` is never registered — the command simply does not
exist, and `/plugins` says why. Nothing breaks and nothing is installed for you.

---

## Install

agentRW has no plugin installer yet, so a plugin is installed by copying two
files into place. Install agentRW first — see
[its README](https://github.com/linuxrebel/agentRW) — then:

```bash
git clone https://github.com/linuxrebel/arwRunTests
```

Or, without git: download the **Source code (zip)** from the
[Releases](https://github.com/linuxrebel/arwRunTests/releases) page and unzip it. It
contains everything the plugin needs. The folder it unpacks to is named for the
tag — `arwRunTests-0.1.0` rather than `arwRunTests` — so adjust the paths below to match.

> **Do not unzip it straight into `tools/`.** A plugin has to sit exactly two
> levels down, at `tools/<owner>/<name>/`. One level too shallow and it is
> skipped in silence — `/plugins` reports nothing registered and says nothing
> about why. Copy the two files as shown below instead.

### Linux and macOS

agentRW installs to `/opt/agentRW`, which is owned by root, so copying a plugin
in needs `sudo`:

```bash
sudo mkdir -p /opt/agentRW/tools/linuxrebel/runtests
sudo cp arwRunTests/plugin.py arwRunTests/install.md /opt/agentRW/tools/linuxrebel/runtests/
```

### Windows

agentRW installs per-user, so no admin is needed. In PowerShell:

```powershell
$dest = "$env:LOCALAPPDATA\Programs\agentRW\tools\linuxrebel\runtests"
New-Item -ItemType Directory -Force -Path $dest
Copy-Item arwRunTests\plugin.py, arwRunTests\install.md -Destination $dest
```

### Check it took

Start `cagent` and run `/plugins`. You should see the plugin listed as
registered, and `/runtests help` should print its usage.

If pytest is missing, `/plugins` lists `linuxrebel/runtests` as dormant and
names `pytest` as what it needs. That is the plugin working correctly, not a
failure — install pytest and restart.

---

## Uninstall

Remove the directory. The command and tool disappear with it.

```bash
sudo rm -rf /opt/agentRW/tools/linuxrebel/runtests
```

---

## Usage

```
/runtests [path] [k=expr]        run the suite, show a summary
/runtests baseline [path]        record the current results as the baseline
/runtests verify [path]          run, then report what broke since the baseline

  path     a file or directory. Default: the working directory
  k=expr   only tests whose name matches, e.g. k=parser
```

The intended cycle around a risky change:

```
/runtests baseline        before you touch anything
… make the change …
/runtests verify          did anything that used to pass stop passing?
```

`verify` reports three things: **regressions** (passed before, failing now),
**disappeared** (used to pass, no longer collected — usually an import error or
a renamed file), and **repaired** (was failing, now passes).

The model can also call `run_tests` directly as a tool. That costs about 40
tokens of context on every turn, which is the price of the model being able to
check its own work. `cagent --low-vram` stops advertising plugin tools; the
`/runtests` command keeps working either way, because it does not go through
the model at all.

---

## License

MIT
