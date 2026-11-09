"""Bundled-demo package data.

The developer launcher, viewer, and tests load the demo pack from source.
Production loads its explicit compiled plan directly, so this package never
needs to broaden its import search path to the checkout's ``demo/`` directory.
"""
