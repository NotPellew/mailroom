"""EmailMan - local-first mail label suggestion, review, and export."""

from EmailMan.classifier import EmailClassifier
from EmailMan.classification import Proposal
from EmailMan.config import Config

__all__ = ["EmailClassifier", "Config", "Proposal", "__version__"]

__version__ = "0.1.0"
