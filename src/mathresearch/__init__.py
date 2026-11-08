"""MathResearch: an autonomous mathematical research environment.

The system runs one loop over one agenda of research questions. Every worker
run is assembled from the persistent state in a single SQLite database and
writes its results back there. Learning, exploration and investigation are
kinds of questions, not separate subsystems.
"""

__version__ = "0.1.0"
