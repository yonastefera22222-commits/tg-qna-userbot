"""Tests for the sub-millisecond AST-safe local math giveaway solver."""

import pytest

from qna_bot.ai.math_solver import try_solve_math


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("What is 47 * 83?", "3901"),
        ("Solve: (12 + 8) × 5", "100"),
        ("150 ÷ 6 = ?", "25"),
        ("Q1: 2^10?", "1024"),
        ("Calculate 15% of 240", "36"),
        ("What is sqrt(144)?", "12"),
        ("√81 + 9", "18"),
        ("1,000 + 2,500", "3500"),
        ("10 / 4", "2.5"),
        ("What's 9 x 9?", "81"),
    ],
)
def test_math_solver_valid_giveaway_prompts(prompt: str, expected: str) -> None:
    assert try_solve_math(prompt) == expected


@pytest.mark.parametrize(
    "prompt",
    [
        "2026",
        "100",
        "2026-11-22",
        "11/22/2026",
        "Capital of France?",
        "__import__('os').system('ls')",
        "9**9**9",
        "10 / 0",
        "Hello everyone!",
    ],
)
def test_math_solver_rejects_non_math_and_unsafe(prompt: str) -> None:
    assert try_solve_math(prompt) is None
