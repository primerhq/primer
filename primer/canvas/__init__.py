"""The agent canvas: Canvas Lang, the element catalog and the atomic batch.

Everything in this package so far is pure: no I/O, no storage, no clock. The
parser (``lang``), the element table (``catalog``), the state and batch
applier (``state``), JSON ops (``ops``), the decompiler (``decompile``) and
the Html script check (``html_check``) are plain functions over plain data.
"""
