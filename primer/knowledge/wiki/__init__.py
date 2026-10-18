"""The platform wiki build: hand-written pages plus pages generated from the tool registry.

``pages`` parses the hand-written sources, ``reference`` renders one page per tool, and
``build`` assembles the table of contents and the content hash; those three read their
inputs and write nothing. ``stamp`` is the only module that touches storage, and only its
own row: it decides whether a build may replace the stored wiki.
"""
