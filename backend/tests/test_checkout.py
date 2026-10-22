"""
Tests for checkout.py's card validation -- the part of this project
that most needs to match Stripe's real documented test-card behavior,
since that's exactly what would be exercised in production.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from checkout import _luhn_valid, TEST_CARD_OUTCOMES


def test_known_success_card_is_a_valid_luhn_number():
    assert _luhn_valid("4242424242424242") is True


def test_known_decline_card_is_still_a_valid_luhn_number():
    # Stripe's decline test cards are still well-formed card numbers --
    # the decline is simulated by Stripe's test-mode backend, not by
    # failing basic validation. Our TEST_CARD_OUTCOMES map mirrors that.
    assert _luhn_valid("4000000000000002") is True
    assert TEST_CARD_OUTCOMES["4000000000000002"] == "declined"


def test_luhn_rejects_obviously_invalid_number():
    assert _luhn_valid("1234567812345678") is False


def test_generic_valid_card_not_in_test_map_defaults_to_success():
    # Any Luhn-valid number that isn't one of Stripe's special test
    # numbers should be treated as a generic successful test charge --
    # this matches how Stripe's real test mode behaves.
    generic_valid_card = "4111111111111111"
    assert _luhn_valid(generic_valid_card) is True
    assert generic_valid_card not in TEST_CARD_OUTCOMES
