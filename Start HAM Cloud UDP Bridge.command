#!/bin/zsh
cd "${0:A:h}" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  print "Python 3.10 or newer is required. Install Python from https://www.python.org/downloads/macos/"
  read "?Press Return to close."
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  print "Please install Python 3.10 or newer from https://www.python.org/downloads/macos/"
  read "?Press Return to close."
  exit 1
fi
python3 -m ham_cloud_udp_bridge --open
if [[ $? -ne 0 ]]; then
  read "?The bridge could not start. Press Return to close."
fi
