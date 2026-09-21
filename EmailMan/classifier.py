"""High-level local classification facade for embedding EmailMan."""

from typing import Any, Dict, List, Optional

from EmailMan import classification
from EmailMan.classification import ClassificationError, Proposal
from EmailMan.config import Config, is_loopback_url


class EmailClassifier:
    """Classify a single email/document with the configured local model."""

    def __init__(
        self,
        config: Optional[Config] = None,
        model_endpoint: Optional[str] = None,
        timeout: Optional[float] = None,
        model_id: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        self.config = config or Config()
        self.config.validate()

        endpoint = model_endpoint or self.config.model_endpoint
        if not is_loopback_url(endpoint):
            raise ClassificationError(
                "Model endpoint must be a loopback http(s) URL "
                "(email text is never sent to a remote model)"
            )
        self.model_endpoint = endpoint
        self.timeout = float(timeout if timeout is not None else self.config.timeout)
        self.model_id = model_id or self.config.model_id
        self.provider = (
            provider
            if provider is not None
            else (None if model_endpoint else self.config.model_provider)
        )

    def classify(
        self,
        email_text: Optional[str] = None,
        labels: Optional[List[Dict[str, Any]]] = None,
        *,
        subject: str = "",
        sender: str = "",
        sender_email: str = "",
        body: Optional[str] = None,
    ) -> Proposal:
        """Return a Proposal for a pre-built mail block or raw subject/body fields."""
        defs = self.config.labels if labels is None else labels
        if (
            not isinstance(defs, list)
            or not defs
            or not all(isinstance(item, dict) and item.get("id") for item in defs)
        ):
            raise ClassificationError(
                "labels must be a non-empty list of label definition objects with an 'id'"
            )
        label_ids = [str(item["id"]) for item in defs]

        if email_text is None:
            if body is None:
                raise ClassificationError("classify requires email_text or body")
            email_text = classification.build_mail_block(
                sender=sender,
                sender_email=sender_email,
                subject=subject,
                body=body,
            )

        return classification.classify_message(
            email_text=email_text,
            label_ids=label_ids,
            model_endpoint=self.model_endpoint,
            timeout=self.timeout,
            model_id=self.model_id,
            labels=defs,
            provider=self.provider,
        )
