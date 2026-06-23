# primer-runtime

The workspace runtime server for [Primer](https://github.com/primerhq/primer) -
a persistent in-container WebSocket process that executes agent and graph work
inside a sandboxed workspace and streams results back to the Primer control
plane.

This package is published independently so a remote host can run only the
runtime without the full Primer platform. For the platform, the operator
console, and full documentation, see the main repository:

https://github.com/primerhq/primer

## Install

```bash
pip install primer-runtime
primer-runtime   # starts the WS server (default port 5959)
```

## License

Apache-2.0. See the main repository for the full text.
