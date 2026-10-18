"""The platform wiki build: hand-written pages plus pages generated from the tool registry.

Pure modules, no storage writes of their own: ``pages`` parses the sources,
``reference`` renders one page per tool, ``build`` assembles the table of
contents and the content hash, ``stamp`` decides whether a build may replace
the stored wiki.
"""
