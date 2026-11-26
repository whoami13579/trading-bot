"""Online execution shared by the scheduled DEMO/LIVE strategies.

``single_position`` runs every single-position strategy (at most one
strategy-owned position on one instrument). It imports broker modules, so it is
imported only by online entry points, never by backtesting.
"""
