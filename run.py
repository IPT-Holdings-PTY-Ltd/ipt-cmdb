"""Entry point for running IPT-CMDB with `python run.py`."""

import os
from cmdb import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)
