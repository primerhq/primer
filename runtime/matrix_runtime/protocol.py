# Re-export from the authoritative worker-side copy so the two stay in sync.
# When the runtime container is built (Task 6) the matrix package is installed
# into the image, making this import available inside the container.
from matrix.workspace.runtime.protocol import *  # noqa: F401, F403
from matrix.workspace.runtime.protocol import (  # noqa: F401 — explicit for type checkers
    ErrorCode,
    Event,
    OpName,
    Request,
    Response,
    deserialize,
    serialize,
)
