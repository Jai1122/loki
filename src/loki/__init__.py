"""LOKI — LLM-Orchestrated Killer-test Intelligence.

A reusable framework that unleashes a swarm of LLM agents ("variants") to
generate high-quality, meaningful JUnit 5 unit tests for Java Spring Boot
repositories. Quality — not raw coverage — is the goal: generated tests must
carry meaningful assertions that kill mutants, and must never be trivial
``assertTrue(true)`` filler.

See DESIGN.md for the full architecture and contracts.
"""

__version__ = "0.1.0"
