"""Convenience launcher for the credential-free mock AWS server.

    python mock_aws.py            # serves on http://127.0.0.1:8788
    python mock_aws.py 9000       # custom port

Open the printed URL in a browser for a live console where you can stop / start
/ resize / terminate instances and watch the cost numbers change in real time.
"""

import sys

from src.mock_aws.server import main

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
    main(port)
