"""Where os2slice runs, for the messages that say where to change something.

`OS2SLICE_ADDON=1` means "in a container" (no keyring; secrets from the file and the
environment) for both the Home Assistant add-on and the Docker image. Which of the two
is `OS2SLICE_RUNTIME`: the add-on's launcher sets "home-assistant", the Docker image
"docker". An add-on started by an older launcher has only OS2SLICE_ADDON=1.
"""

from __future__ import annotations

import os
from typing import Literal, cast

Runtime = Literal["home-assistant", "docker", "desktop"]
HOME_ASSISTANT: Runtime = "home-assistant"
DOCKER: Runtime = "docker"
DESKTOP: Runtime = "desktop"


def runtime() -> Runtime:
    value = os.environ.get("OS2SLICE_RUNTIME", "")
    if value in (HOME_ASSISTANT, DOCKER):
        return cast(Runtime, value)
    return HOME_ASSISTANT if os.environ.get("OS2SLICE_ADDON") == "1" else DESKTOP
