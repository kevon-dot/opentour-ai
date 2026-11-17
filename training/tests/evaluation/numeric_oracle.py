"""Test-only independent Decimal/binomial oracle; no production or SciPy imports."""

from __future__ import annotations

from decimal import Decimal, localcontext
from math import comb, factorial


def binomial_probability(k: int, n: int, p: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        left = Decimal(1) if k == 0 else p**k
        right = Decimal(1) if n == k else (1 - p) ** (n - k)
        return Decimal(comb(n, k)) * left * right


def cdf(k: int, n: int, p: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        return sum((binomial_probability(i, n, p) for i in range(k + 1)), Decimal(0))


def upper(k: int, n: int, alpha: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        if k == n:
            return Decimal(1)
        if k == 0:
            return 1 - (alpha.ln() / n).exp()
        left, right = Decimal(0), Decimal(1)
        for _ in range(180):
            middle = (left + right) / 2
            if cdf(k, n, middle) > alpha:
                left = middle
            else:
                right = middle
        return right


def lower(k: int, n: int, alpha: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        return Decimal(0) if k == 0 else 1 - upper(n - k, n, alpha)


def family_lower(mean: Decimal, count: int, alpha: Decimal, sides: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        radius = (2 * ((Decimal(1) if sides == 1 else Decimal(2)) / alpha).ln() / count).sqrt()
        return max(Decimal(-1), mean - radius)


def multinomial_probability(cells: tuple[int, ...], probabilities: tuple[Decimal, ...]) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        coefficient = factorial(sum(cells))
        for count in cells:
            coefficient //= factorial(count)
        result = Decimal(coefficient)
        for count, probability in zip(cells, probabilities, strict=True):
            result *= Decimal(1) if count == 0 else probability**count
        return result
