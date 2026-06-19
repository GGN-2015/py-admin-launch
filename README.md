# py-admin-launch

A cross-platform Python command/helper to launch a specified program with
administrator privileges.

## Strategy

- Linux desktop: `pkexec` for polkit GUI authentication, falling back to `sudo`
  when `pkexec` is not available.
- Windows: `ShellExecuteW(..., "runas", ...)`.
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
py-admin-launch --wait --cwd /path/to/workdir -- your-command arg1 arg2
```

`--wait` makes `py-admin-launch` wait until the launched command exits and then
return that command's exit code when the platform launcher supports it. Without
`--wait`, `py-admin-launch` only starts the command and returns after the
elevation request is handed off.

On Windows, elevated launches use `ShellExecuteW(..., "runas", ...)`, which does
not provide a child process handle to this helper. That means `--wait` cannot
observe the elevated program's exit code on Windows.

## Python API

```python
from py_admin_launch import launch

launch(["python", "-m", "http.server", "80"])
```

`launch()` accepts a command list plus optional `cwd` and `wait` arguments:

```python
result = launch(["your-command", "arg1"], cwd="/tmp", wait=True)
print(result.elevated, result.returncode)
```

When `wait=True`, `returncode` is the launched command's exit code where the
platform exposes one. When `wait=False`, `returncode` is `None`.
