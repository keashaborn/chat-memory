"""Conversation capability contracts and application services.

Import concrete services from their defining modules. Keeping this package
initializer side-effect free prevents the contracts module from importing the
composition root through the inactive provider during package initialization.
"""
