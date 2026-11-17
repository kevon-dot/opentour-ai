# Registered numerical profile

The CPU package accepts exact registered schema bytes and already decoded plan
values. Alpha allocations use the exact rational value of each shortest decimal
representation. Family effects use the supplied binary64 values. Counts must
be integers. One calculation can use up to 1,000,000 independent units. An
admitted plan has up to 64 looks, 128 claims per look and 8,192 claim cells.
External alpha ranges from 1e-12 to 1 - 1e-12, inclusive. The smallest internal
tail is 2.5e-13 after a two-sided paired allocation.

## Precision

Clopper–Pearson root candidates use the SciPy inverse of the smaller regularized
beta tail. Complementation takes place on the exact allocation before conversion
to binary64. Closed-form binomial endpoints use 80-digit decimal log and
exponentiation before outward rounding. [Regularized inverse](https://docs.scipy.org/doc/scipy/reference/generated/scipy.special.betaincinv.html) · [Complemented inverse](https://docs.scipy.org/doc/scipy/reference/generated/scipy.special.betainccinv.html)

An independent binomial probability recurrence checks each interior inverse
root. It starts with unit weight at the mode and normalizes accumulated weights.
The weights decrease away from the mode, so omitting weights below 1e-40 leaves
less than 1.1e-34 unnormalized mass in this profile. The eight-ULP probability
guard exceeds that truncation and the final conversion error. The relative
residual tolerance is 1e-7 of the smaller tail. If an adjacent representable
value jumps across the guard, the result uses the outward neighbor. Repair
search is bounded to 2²⁰ floating-point steps from the proposed root. An invalid
inverse or a root that fails the residual check raises a numerical error.

The paired interval subtracts already outward-rounded marginal bounds before
rounding the subtraction outward. The union bound covers both marginal tail
errors for a lower-only interval and all four for a two-sided interval. The
family interval widens the summed mean and high-precision radius before adding
or subtracting them. This preserves direction even when the expression nearly
cancels. Family means give each supplied independent family equal weight.
[Python arithmetic](https://docs.python.org/3.12/library/math.html) · [Decimal arithmetic](https://docs.python.org/3.12/library/decimal.html)

## Qualification scope

The independent test oracle computes finite binomial sums and solves them by
Decimal bisection. Tests compare canonical and finite-grid results within an
absolute tolerance of 1e-11 and use direction checks independently of that
tolerance. Large-count checks cover analytic endpoints, tail boundaries and
near-endpoint interior roots. Finite binomial and paired multinomial grids
enumerate exact outcome probabilities. The family formula has separate
one- and two-sided Decimal checks, including cancellation near zero.

These checks qualify deterministic calculations on generated inputs. The
evaluation service owns verification of population, independence, frozen design,
eligible outcomes, customer criteria and release decisions.
