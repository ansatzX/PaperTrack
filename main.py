import logging

from papertrack.cli import main

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s[%(levelname)s] %(message)s",
)

for _name in ("httpcore", "urllib3", "httpx", "requests"):
    logging.getLogger(_name).setLevel(logging.WARNING)

if __name__ == "__main__":
    main()
