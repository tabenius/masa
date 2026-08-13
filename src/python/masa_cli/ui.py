import sys

YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


def render_progress(
    seq_num: int,
    verb: str,
    current: int,
    total: int,
    state: str = "running",
    error_msg: str = "",
) -> None:
    if state == "running":
        color = YELLOW
    elif state == "done":
        color = GREEN
    else:
        color = RED

    seq_str = f"{color}[ {seq_num:04d} ]{RESET}"
    ratio = min(max(current / total, 0.0), 1.0) if total > 0 else 1.0
    filled = int(round(ratio * 20))
    bar = "=" * filled + "-" * (20 - filled)
    verb_str = f"{verb:<11}"

    line = f"{seq_str}\t{verb_str}\t[{bar}]"
    if state == "error" and error_msg:
        line += f" Error: {error_msg}"

    sys.stdout.write("\r" + line + ("\n" if state in ("done", "error") else ""))
    sys.stdout.flush()


def print_warning(msg: str) -> None:
    sys.stdout.write(f"\r{YELLOW}[ WARNING ] {msg}{RESET}\n")
    sys.stdout.flush()


def print_error(msg: str) -> None:
    sys.stdout.write(f"\r{RED}[ ERROR ] {msg}{RESET}\n")
    sys.stdout.flush()
