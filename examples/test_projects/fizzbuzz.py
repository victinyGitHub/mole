"""FizzBuzz — Python test file for mole.
Tests: hole discovery, type extraction via pyright, fill, verify.
"""
from mole import hole


# @mole:behavior return "Fizz" for multiples of 3, "Buzz" for 5,
#   "FizzBuzz" for both, str(n) otherwise
# @mole:ensures always returns a string, never None
def fizzbuzz(n: int) -> str:
    result: str = hole("implement fizzbuzz logic for single number")
    return result


# @mole:behavior generate fizzbuzz strings for range 1..n inclusive
# @mole:ensures length of output list equals n
def fizzbuzz_range(n: int) -> list[str]:
    output: list[str] = hole("generate fizzbuzz for 1 to n")
    return output
