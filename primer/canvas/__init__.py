"""The agent canvas: Canvas Lang, the element catalog, the atomic batch and the service that keeps a session's canvas.

The core is pure: no I/O, no storage, no clock. The parser (``lang``), the element table (``catalog``), the state and batch applier
(``state``), JSON ops (``ops``), the decompiler (``decompile``) and the Html script check (``html_check``) are plain functions over plain
data.

Around it sit the parts that touch storage, the event log and the session log: the settings row (``settings``), the service that writes
the canvas and announces it (``service``, with ``records`` for the transcript lines) and the rule of ``canvas_wait`` (``wait``). The
agent's tools are in ``primer.toolset.canvas``.
"""
