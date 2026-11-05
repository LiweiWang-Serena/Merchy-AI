"""The handoff safety net: order problems go to a person; policy questions and what-ifs don't."""
import pytest

from escalation import reports_problem

ORDER_PROBLEMS = [
    "My carburetor arrived cracked, I want a refund.",
    "The spark plugs I ordered just arrived, but you sent me the completely wrong size.",
    "I ordered the silver part, but you guys sent me a gold one instead.",
    "Tracking says my part shipped out, but it never actually arrived.",
    "It has been days since I paid and my order still hasn't shipped out yet.",
    "When I opened the package, the CDI box plug was crushed.",
    "The part I ordered is way too wide for my bike and doesn't fit at all. I need to return this.",
    "The gas cap I bought broke the very second I tried putting it on my tank.",
    "The product I received looks nothing like the pictures and it's missing parts.",
    "I returned the item two weeks ago and still no refund.",
    "The part you sent me is completely defective.",
    "The hose I received is regular plastic instead of silicone.",
]

QUESTIONS = [
    "How fast do you ship after I pay?",
    "What's your return policy if I change my mind?",
    "If the part arrives damaged, what do I do?",
    "What happens if the item arrives damaged?",
    "Can I return the inner tube if I ordered the wrong size?",
    "How long does it take to get my refund after I send it back?",
    "Will this starter relay solenoid work on a 2001 Yamaha TTR225?",
    "Do you have a carburetor for a 2008 Yamaha Grizzly 450?",
    "My bike won't start, will this coil fix it?",
    "Is this used part tested before you sell it?",
    "",
]


@pytest.mark.parametrize("message", ORDER_PROBLEMS)
def test_order_problems_go_to_a_person(message):
    assert reports_problem(message)


@pytest.mark.parametrize("message", QUESTIONS)
def test_questions_and_what_ifs_stay_with_the_assistant(message):
    assert not reports_problem(message)
