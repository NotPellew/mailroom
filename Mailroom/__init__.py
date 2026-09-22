"""Mailroom - local-first mail label suggestion, review, and export."""

from Mailroom.classifier import EmailClassifier
from Mailroom.classification import Proposal
from Mailroom.config import Config

__all__ = ["EmailClassifier", "Config", "Proposal", "__version__"]

__version__ = "0.1.0"
