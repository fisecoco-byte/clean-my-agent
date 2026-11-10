"""Release executable entry; never re-exec a checkout virtual environment."""
import multiprocessing
from clean_my_agent.cli import main

if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
