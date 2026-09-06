"""Visibility shared by durable text logs and the live frontend."""


def visible_research_log_line(line: str) -> bool:
    message = line.partition("] ")[2] if line.startswith("[") and "] " in line else line
    if not message.strip():
        return False
    if message.startswith(("tool>", "usage>")):
        return False
    if message.startswith("status>"):
        return any(word in message.lower() for word in ("error", "failed", "cancel", "interrupt"))
    return not message.startswith(("max_idle=", "stall watchdog:", "heartbeat", "polling "))

