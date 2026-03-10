from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def main() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path)
    from .bot import run_bot

    run_bot()


if __name__ == "__main__":
    main()
