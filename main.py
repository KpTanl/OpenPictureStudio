import sys

if __name__ == "__main__":
    from src.utils.installation import guard_startup
    if not guard_startup():
        sys.exit(1)

from src.main import main

if __name__ == "__main__":
    sys.exit(main())
