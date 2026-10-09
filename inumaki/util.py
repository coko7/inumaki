import re
import time


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def normalize(text: str) -> str:
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^\w\s%]", "", text)
    return " ".join(text.split())
