"""HADCD node agent.

The software that runs on a heat-capable building's computer. It
enrolls with the central server (Phase 7a auth), heartbeats, reports
heat demand from a BMS data source, pulls offloaded tasks, runs them
in subprocesses against the shared handler registry
(`hadcd_workloads`), and returns the results.

The agent is the production counterpart to the Phase 5 simulator: the
simulator fakes execution with sleeps and invents heat data; the agent
really executes and reads heat from a real source (a file in 7b; a
pluggable adapter in 7c).
"""

__version__ = "0.7.0"
