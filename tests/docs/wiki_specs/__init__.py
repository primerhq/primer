"""The page specs of the platform wiki, one module per section (pivot P4).

``tests.docs._wiki_specs.all_specs`` imports every module of this package and
merges their ``SPECS``; a module may not define a path twice (an
``AssertionError``). Task 1 adds ``sections.py`` with the sixteen section
index rows; each later content task adds the module for its own specs.
"""
