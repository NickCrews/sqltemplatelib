import sys

collect_ignore = []
if sys.version_info < (3, 14):
    # t-string literals are a syntax error before Python 3.14.
    collect_ignore.append("test_tstrings.py")
