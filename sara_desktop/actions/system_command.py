import sys
import subprocess
import platform

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_IS_WINDOWS = platform.system() == "Windows"

def run_system_command(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    cmd = (
        params.get("command") or
        params.get("cmd") or
        params.get("exec") or
        params.get("terminal_command") or
        ""
    ).strip()

    if not cmd:
        return "No command provided to execute."

    shell_type = params.get("shell", "powershell" if _IS_WINDOWS else "bash").lower()
    timeout = int(params.get("timeout", 30))

    print(f"[system_command] Running ({shell_type}): {cmd}")

    if player:
        player.write_log(f"[cmd] {cmd}")

    try:
        if _IS_WINDOWS and shell_type == "powershell":
            full_cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd]
        else:
            full_cmd = cmd

        result = subprocess.run(
            full_cmd,
            shell=isinstance(full_cmd, str),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace"
        )

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode == 0:
            output = stdout if stdout else f"Command executed successfully (exit code 0)."
            return output
        else:
            err_msg = stderr if stderr else stdout
            return f"Command exited with code {result.returncode}:\n{err_msg}"

    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout} seconds."
    except Exception as ex:
        print(f"[system_command] Error: {ex}")
        return f"Execution error: {ex}"


TOOL = {
    "name": "system_command",
    "description": "Executes shell and terminal commands directly on the user's computer (PowerShell on Windows, Bash on Mac/Linux). Use this to run CLI commands, list directories, inspect processes, manage files, or run system utilities in Agent Mode.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "The exact shell or terminal command to run (e.g. 'Get-Process', 'dir', 'ipconfig', 'git status')"
            },
            "shell": {
                "type": "STRING",
                "description": "powershell | cmd | bash (defaults to powershell on Windows)"
            }
        },
        "required": [
            "command"
        ]
    },
    "handler": run_system_command,
}
