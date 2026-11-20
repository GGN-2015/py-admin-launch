# py-admin-launch

A cross-platform Python command/helper to launch a specified program with
administrator privileges.

Python 3.6 and every later Python 3 release are supported. Python 3.6 installs
the small `dataclasses` backport automatically; newer interpreters use the
standard-library implementation.

## Strategy

- Windows: UAC starts a hidden helper. The command uses the caller's console
  for interactive streams and the original file or pipe handles for redirected
  streams. Standard output and standard error remain separate; binary data is
  not decoded or copied through a TCP server. Only the UAC consent dialog is
  needed, not a second command window.
- Linux: a controlling terminal uses `sudo`, which can prompt on `/dev/tty`
  even when command input is piped. A headless session uses `sudo -n`: arrange
  noninteractive authorization or the command fails on stderr. A graphical
  session without a controlling terminal prefers `pkexec` and a polkit agent.
  `pkexec` receives the display/session variables and keeps the working directory;
  `sudo` uses its configured environment policy and the requested working directory.
  Standard input, output and error are inherited in all three cases.
- macOS: `osascript` + `with administrator privileges`, falling back to `sudo`
  when `osascript` is not available.

If the current process is already administrator/root, the command is launched
directly as the current user.

## CLI

Run from source:

```bash
python -m py_admin_launch -- your-command arg1 arg2
```

After installing the package:

```bash
py-admin-launch -- your-command arg1 arg2
```

Useful options:

```bash
py-admin-launch --cwd /path/to/workdir -- your-command arg1 arg2
py-admin-launch --no-wait -- your-command arg1 arg2
```

The CLI waits by default and returns the command's exit code. On Windows and
Linux, use ordinary shell pipes and redirection. For example, in `cmd.exe` or
a POSIX shell:

```sh
py-admin-launch -- your-command < input.bin > output.bin 2> errors.log
```

Press Ctrl+C to interrupt a foreground command. On Windows the console delivers
Ctrl+C to an attached command. With all three streams redirected, the helper
forwards Ctrl+C to the command's private hidden console instead. On cancellation
the helper gives the command up to three seconds to exit, then terminates any
remaining processes in its job. The CLI reports an interrupt as exit code 130.

Use `--no-wait` only when you intentionally want to start the command in the
background. On Windows it returns after the helper confirms the command has
started. The child retains its stream handles, but the launcher no longer waits
for its exit or supervises Ctrl+C. Redirect streams before handing off if the
child should not share the terminal.

### `py-sudo`: sudo-style arguments

Install `py-admin-launch` to get both commands. With `py-sudo`, put options
before the command. Arguments after the command belong to the command:

```sh
py-sudo whoami
py-sudo -n -- your-command --help
```

On **Linux and macOS**, `py-sudo` replaces itself with the system `sudo` and
passes every argument unchanged, even when already root. For example,
`py-sudo -u nobody -- id -u` runs `id` as `nobody`. Options, `NAME=value`
assignments, password prompts, terminal handling, policy and exit status are
those of the installed sudo. This entry point requires system sudo; it never
falls back to `pkexec` or `osascript`.

On **Windows**, `py-sudo` uses the same UAC and standard-stream handling as
`py-admin-launch`. It waits and returns the command's exit code by default.
Use `-b` to return after startup, `-D DIR` to set the working directory, or
`-n` to fail without opening UAC if not already elevated. `-u root` and
`-u "#0"` request an administrator token, not a different account. `-s` alone
opens an interactive `cmd.exe`; for a shell built-in use, for example,
`py-sudo cmd.exe /d /c ver`. `py-sudo --help` lists the Windows options.
Unix-only operations such as `-u nobody`, `-S`, `-E`, `-v` and leading
`NAME=value` assignments are rejected, not silently ignored.

## Python API

```python
from py_admin_launch import launch

launch(["python", "-m", "http.server", "80"], wait=True)
```

`launch()` accepts a command list plus optional `cwd` and `wait` arguments:

```python
result = launch(["your-command", "arg1"], cwd="/tmp", wait=True)
print(result.elevated, result.returncode)
```

Use `wait=True` for foreground execution and interruption. With the API default
`wait=False`, `returncode` is `None`. On Windows, `pid` is the target command's
PID, not the helper's.

## Testing

Run `python -m unittest discover -s tests -v`. Windows integration tests use
the real helper and Win32 IPC with the `open` verb so CI needs no consent UI.
Set `PAL_TEST_UAC=1` from a **non-administrator** terminal to exercise `runas`
instead. Linux integration tests run as root or with noninteractive `sudo`
authorization for the test interpreter.
