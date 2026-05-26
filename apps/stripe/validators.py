"""Shared validators for Stripe-format identifiers."""

import re

SUBSCRIPTION_ID_PATTERN = re.compile(r"^sub_[A-Za-z0-9]+$")
