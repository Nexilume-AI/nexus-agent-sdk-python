"""Acquire a POSIX controlling terminal before replacing this process with a shell.

Executed in a fresh Python process: using preexec_fn in the threaded Runtime
could deadlock after fork. Popen already created our session with setsid().
"""

import fcntl
import os
import sys
import termios


def main() -> None:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(0, os.getpgrp())
    executable = sys.argv[1]
    os.execv(executable, [executable, "-i"])


if __name__ == "__main__":
    main()
