"""Authority, policy, configuration, audit, and error boundaries.

Import concrete submodules explicitly so a pure ownership check does not load
provider clients, voice routing, or database dependencies as a side effect.
"""
