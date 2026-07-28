"""Python-runner toolset: register a python function as a callable tool.

See docs/dev/subsystems/python-runner-toolset.md. The source is untrusted
(agents can reach the toolset-management tools), so registration inspects it
by AST only and execution happens behind isolation in `runners`.
"""
