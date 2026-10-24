"""tool-command-shell: run a shell command sent as an a8s message."""
from .shell import (  # noqa: F401
    DEFAULT_INLINE_MAX,
    DEFAULT_OUTPUT_MAX,
    DEFAULT_TIMEOUT,
    build_argv,
    compose_reply,
    main,
    run_command,
)
