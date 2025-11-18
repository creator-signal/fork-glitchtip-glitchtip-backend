from pydantic import ConfigDict

from ninja import Schema


class LaxIngestSchema(Schema):
    """Schema configuration for all event ingest schemas"""

    model_config = ConfigDict(coerce_numbers_to_str=True)
